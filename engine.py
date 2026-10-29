"""
engine.py - every calculation and every met / not-met decision lives here.

Design rule for the whole app:  AI READS AND EXPLAINS - THIS FILE CALCULATES AND DECIDES.
Nothing in this file calls the AI. Each rule is plain if-then logic (Lab 1 style), with the
regulation it comes from written next to it so a lawyer can check it.

Money: the Fed's FR Y-9C figures are in THOUSANDS of US dollars, and this file keeps thousands.
`bn()` turns a value into "$12.3bn" for display. Canadian (OSFI) figures are in C$ millions.

Simplifications (also stated on screen):
  * the combined bank is the two banks ADDED TOGETHER - no purchase premium or goodwill;
  * size categories use asset size only.
"""

import csv
import datetime
import io
import re

# ======================================================================================
# 1. READING FR Y-9C REPORTS
#    Each figure is identified by its MDRM code - the Fed's ID for one line of the report.
# ======================================================================================
FIELDS = {
    # our name          (MDRM code(s),        plain-English label,                   Y-9C location)
    "total_assets":      (["BHCK2170"],        "Total assets",                         "Sch. HC, item 12"),
    "avg_assets_lev":    (["BHCAA224"],        "Average assets for leverage ratio",    "Sch. HC-R Pt I"),
    "cet1":              (["BHCAP859"],        "Common equity tier 1 (CET1) capital",  "Sch. HC-R Pt I"),
    "tier1":             (["BHCA8274"],        "Tier 1 capital",                       "Sch. HC-R Pt I"),
    "tier2":             (["BHCA5311"],        "Tier 2 capital",                       "Sch. HC-R Pt I"),
    "total_capital":     (["BHCA3792"],        "Total capital",                        "Sch. HC-R Pt I"),
    "rwa":               (["BHCAA223"],        "Risk-weighted assets",                 "Sch. HC-R Pt II"),
    "cet1_ratio":        (["BHCAP793"],        "CET1 ratio (reported, %)",             "Sch. HC-R Pt I"),
    "tier1_ratio":       (["BHCA7206"],        "Tier 1 ratio (reported, %)",           "Sch. HC-R Pt I"),
    "total_ratio":       (["BHCA7205"],        "Total capital ratio (reported, %)",    "Sch. HC-R Pt I"),
    "leverage_ratio":    (["BHCA7204"],        "Leverage ratio (reported, %)",         "Sch. HC-R Pt I"),
    "slr":               (["BHCAH036"],        "Supplementary leverage ratio (reported, %)", "Sch. HC-R Pt I"),
    "total_liabilities": (["BHCK2948"],        "Total liabilities",                    "Sch. HC, item 21"),
    "deposits":          (["BHDM6631", "BHDM6636", "BHFN6631", "BHFN6636"], "Total deposits", "Sch. HC, item 13"),
}
RATIO_FIELDS = {"cet1_ratio", "tier1_ratio", "total_ratio", "leverage_ratio", "slr"}


def _num(value):
    try:
        return float(str(value).replace(",", "")) if value not in ("", None) else None
    except ValueError:
        return None


def _bank_from(get, rssd, name, city, state, quarter):
    bank = {"rssd": rssd, "name": name, "city": city, "state": state, "quarter": quarter}
    for key, (codes, _label, _loc) in FIELDS.items():
        present = [v for v in (_num(get(c)) for c in codes) if v is not None]
        bank[key] = sum(present) if present else None
    return bank


def parse_y9c_bulk(file_bytes):
    """The Fed's bulk download (caret '^' separated .txt). Returns (quarter, {rssd: bank})."""
    reader = csv.reader(io.StringIO(file_bytes.decode("latin-1")), delimiter="^")
    header = next(reader)
    banks, quarter = {}, None
    for row in reader:
        if len(row) < 10:
            continue
        rec = dict(zip(header, row))
        quarter = rec.get("RSSD9999", quarter)
        bank = _bank_from(rec.get, rec.get("RSSD9001"), (rec.get("RSSD9017") or "").strip(),
                          (rec.get("RSSD9130") or "").strip().title(), (rec.get("RSSD9200") or "").strip(), rec.get("RSSD9999"))
        if bank["total_assets"]:          # skip mid-tier entities with no balance sheet
            banks[bank["rssd"]] = bank
    return quarter, banks


def parse_y9c_pdf(file_bytes):
    """One FR Y-9C report saved as a PDF from the Fed's National Information Center.
    These PDFs are fillable forms: each number sits in a field named after its MDRM code
    (e.g. 'BHCK2170'), so figures are read exactly - no AI, no guessing.
    Returns (quarter, {rssd: bank}), or (None, {}) if the PDF is not an FR Y-9C."""
    from pypdf import PdfReader
    vals = {}
    for full, f in (PdfReader(io.BytesIO(file_bytes)).get_fields() or {}).items():
        v = f.get("/V")
        if v in (None, "") or str(v).strip() in ("", "/Off"):
            continue
        vals.setdefault(re.sub(r"\[\d+\]$", "", full.split(".")[-1]), str(v).strip())
    if "BHCK2170" not in vals:
        return None, {}
    try:
        quarter = datetime.datetime.strptime(vals.get("TEXT9999", ""), "%B %d, %Y").strftime("%Y%m%d")
    except ValueError:
        quarter = None
    bank = _bank_from(vals.get, vals.get("ID_RSSD", "pdf"), vals.get("NM_LGL", "Unnamed holding company").strip(),
                      vals.get("CITY", "").title(), vals.get("STATE_ABBR_NM", ""), quarter)
    return quarter, {bank["rssd"]: bank}


def merge_quarters(parsed):
    """[(quarter, {rssd: bank}), ...] -> {rssd: {"latest", "history", "avg_assets", "quarters"}}"""
    merged = {}
    for quarter, banks in sorted(parsed, key=lambda x: x[0] or ""):
        for rssd, bank in banks.items():
            entry = merged.setdefault(rssd, {"history": []})
            if not any(b["quarter"] == bank["quarter"] for b in entry["history"]):
                entry["history"].append(bank)
    for entry in merged.values():
        entry["latest"] = entry["history"][-1]
        assets = [b["total_assets"] for b in entry["history"][-4:] if b["total_assets"]]
        entry["avg_assets"] = sum(assets) / len(assets)
        entry["quarters"] = len(assets)
    return merged


def cross_checks(bank):
    """Data checks: recompute each reported ratio from its parts."""
    checks = []
    for label, num, den, reported in (("CET1 ratio", "cet1", "rwa", "cet1_ratio"), ("Tier 1 ratio", "tier1", "rwa", "tier1_ratio"),
                                      ("Total capital ratio", "total_capital", "rwa", "total_ratio"),
                                      ("Leverage ratio", "tier1", "avg_assets_lev", "leverage_ratio")):
        if None in (bank.get(num), bank.get(den), bank.get(reported)) or not bank.get(den):
            checks.append({"check": label, "ok": None, "result": "missing data"})
            continue
        calc = 100 * bank[num] / bank[den]
        checks.append({"check": label, "ok": abs(calc - bank[reported]) < 0.05,
                       "result": f"{calc:.2f}% recomputed vs {bank[reported]:.2f}% reported"})
    return checks


def bn(thousands, dp=1):
    """Thousands of dollars -> '$12.3bn' (or '$1.25tn')."""
    if thousands is None:
        return "n/a"
    if abs(thousands) >= 1e9:
        return f"${thousands / 1e9:,.2f}tn"
    return f"${thousands / 1e6:,.{dp}f}bn"


# ======================================================================================
# 2. SIZE CATEGORY  (12 CFR 252.5; 12 CFR 217.2) - by size only, plus a GSIB flag
# ======================================================================================
LINES = {"IV": 100e6, "III": 250e6, "II": 700e6}
CATEGORY_ORDER = ["None", "IV", "III", "II", "I"]
CATEGORY_CITE = "12 CFR 252.5; 12 CFR 217.2"


def category(avg_assets, gsib=False):
    if gsib:
        return "I", "It is a U.S. global systemically important bank (GSIB). Category I is only for GSIBs."
    if avg_assets >= LINES["II"]:
        return "II", f"Average total assets of {bn(avg_assets)} are $700bn or more."
    if avg_assets >= LINES["III"]:
        return "III", f"Average total assets of {bn(avg_assets)} are between $250bn and $700bn."
    if avg_assets >= LINES["IV"]:
        return "IV", f"Average total assets of {bn(avg_assets)} are between $100bn and $250bn."
    return "None", f"Average total assets of {bn(avg_assets)} are below $100bn, so no category applies."


def cat_label(c):
    return "Below $100bn" if c == "None" else f"Category {c}"


def rank(c):
    return CATEGORY_ORDER.index(c)


REQUIREMENTS = [  # what each category switches on, tied to a provision in the library
    ("Annual capital plan (CCAR)", {"None": "No", "IV": "Yes", "III": "Yes", "II": "Yes", "I": "Yes"}, "12 CFR 225.8"),
    ("Stress capital buffer (min 2.5%) replaces the 2.5% conservation buffer",
     {"None": "No", "IV": "Yes", "III": "Yes", "II": "Yes", "I": "Yes"}, "12 CFR 217.11(a); 12 CFR 225.8"),
    ("Supervisory stress test", {"None": "No", "IV": "Every other year", "III": "Annual", "II": "Annual", "I": "Annual"}, "12 CFR 252.44"),
    ("Supplementary leverage ratio (3%)", {"None": "No", "IV": "No", "III": "Yes", "II": "Yes", "I": "Yes (enhanced)"}, "12 CFR 217.10(a)(1)(v)"),
    ("Countercyclical capital buffer", {"None": "No", "IV": "No", "III": "Yes", "II": "Yes", "I": "Yes"}, "12 CFR 217.11(b)"),
    ("Unrealised securities gains/losses (AOCI) count in CET1",
     {"None": "Optional opt-out", "IV": "Optional opt-out", "III": "Optional opt-out", "II": "Yes", "I": "Yes"}, "12 CFR 217.22(b)(2)"),
    ("Liquidity coverage ratio / NSFR", {"None": "No", "IV": "Possibly (reduced)", "III": "Yes (reduced or full)", "II": "Yes (full)", "I": "Yes (full)"},
     "12 CFR 249.30(c)"),
]


def newly_applying(before, after):
    return [{"Requirement": r, "Before": m[before], "After": m[after], "Source": c}
            for r, m, c in REQUIREMENTS if m[before] != m[after]]


# ======================================================================================
# 3. THE COMBINED BANK (simple addition)
# ======================================================================================
def combine(a, t):
    keys = ("total_assets", "avg_assets_lev", "cet1", "tier1", "tier2", "total_capital", "rwa", "total_liabilities", "deposits")
    c = {k: (a[k] or 0) + (t[k] or 0) for k in keys}
    c["name"] = "Combined bank"
    c["cet1_ratio"] = 100 * c["cet1"] / c["rwa"]
    c["tier1_ratio"] = 100 * c["tier1"] / c["rwa"]
    c["total_ratio"] = 100 * c["total_capital"] / c["rwa"]
    c["leverage_ratio"] = 100 * c["tier1"] / c["avg_assets_lev"]
    if a.get("slr") and t.get("slr"):   # back out each bank's leverage exposure from its reported SLR
        c["slr"] = 100 * c["tier1"] / (a["tier1"] / (a["slr"] / 100) + t["tier1"] / (t["slr"] / 100))
    else:
        c["slr"] = None
    return c


# ======================================================================================
# 4. CAPITAL, LEVERAGE AND LIABILITIES TESTS
# ======================================================================================
CAPITAL_RATIOS = [("cet1_ratio", "CET1 ratio", 4.5), ("tier1_ratio", "Tier 1 ratio", 6.0), ("total_ratio", "Total capital ratio", 8.0)]
BUFFER = 2.5   # capital conservation buffer, or the minimum stress capital buffer for Category IV+


def judge(value, minimum, with_buffer):
    if value is None:
        return "unknown"
    if value < minimum:
        return "not_met"
    if value < with_buffer:
        return "warning"
    return "met"


def capital_tests(bank):
    return [{"key": k, "test": name, "value": bank.get(k), "minimum": m, "with_buffer": m + BUFFER,
             "status": judge(bank.get(k), m, m + BUFFER), "cite": "12 CFR 217.10(a); 12 CFR 217.11(a)"}
            for k, name, m in CAPITAL_RATIOS]


def leverage_tests(bank, cat):
    rows = [{"key": "leverage_ratio", "test": "Tier 1 leverage ratio", "value": bank.get("leverage_ratio"), "minimum": 4.0,
             "with_buffer": 4.0, "status": judge(bank.get("leverage_ratio"), 4.0, 4.0), "cite": "12 CFR 217.10(a)(1)(iv)"}]
    if cat in ("III", "II", "I"):
        rows.append({"key": "slr", "test": "Supplementary leverage ratio", "value": bank.get("slr"), "minimum": 3.0,
                     "with_buffer": 3.0, "status": judge(bank.get("slr"), 3.0, 3.0), "cite": "12 CFR 217.10(a)(1)(v)"})
    return rows


FIN_SECTOR_LIABILITIES = 23_223_259_570   # thousands of $ = $23.22 trillion
FIN_SECTOR_SOURCE = ("Federal Reserve notice, FR Doc. 2025-09560 — in effect 1 Jul 2025 to 30 Jun 2026. "
                     "Check for the figure published for 1 Jul 2026 onward.")


def liabilities_test(combined_liabilities, total=FIN_SECTOR_LIABILITIES):
    """12 U.S.C. 1852(b); 12 CFR 251: the combined firm may not exceed 10% of financial-sector liabilities."""
    limit = 0.10 * total
    share = 100 * combined_liabilities / total
    return {"status": "met" if combined_liabilities <= limit else "not_met", "share": share, "limit": limit,
            "detail": f"{bn(combined_liabilities)} = {share:.2f}% of the financial sector's liabilities (limit 10% = {bn(limit)})."}


# ======================================================================================
# 5. A CANADIAN BANK BUYING A U.S. BANK - the U.S. rules it becomes subject to
#    All amounts in thousands of US$.
# ======================================================================================
IHC_TRIGGER = 50e6          # U.S. non-branch assets (12 CFR 252.153)
SUBPART_O_TRIGGER = 100e6   # combined U.S. assets incl. branches (12 CFR 252.150 - Subpart O)

SUBPART_O_RULES = [
    ("U.S. risk committee and U.S. chief risk officer for the combined U.S. operations", "12 CFR 252.155"),
    ("Liquidity stress tests and a U.S. liquidity buffer (branches and IHC)", "12 CFR 252.156; 12 CFR 252.157"),
    ("Capital stress testing", "12 CFR 252.158"),
    ("Certify the parent meets home-country capital standards consistent with Basel", "12 CFR 252.154"),
]
SUBPART_O_NOTE = "Applies to foreign banks with $100bn+ total assets and $100bn+ combined U.S. assets (12 CFR Part 252, Subpart O)."


def _transition(before_on, after_on):
    return "applies" if before_on else ("newly_applies" if after_on else "met")


def us_footprint(ihc_assets, branch_assets, target_assets):
    """U.S. presence before and after the deal. Branches are part of the parent, not subsidiaries."""
    nb_before = ihc_assets
    nb_after = ihc_assets + target_assets
    return {"nonbranch_before": nb_before, "nonbranch_after": nb_after,
            "combined_before": nb_before + branch_assets, "combined_after": nb_after + branch_assets}


def us_rules_for_foreign_buyer(fp):
    """The U.S. thresholds a foreign bank crosses, before vs after the deal."""
    rows = []
    rows.append({"status": _transition(fp["nonbranch_before"] >= IHC_TRIGGER, fp["nonbranch_after"] >= IHC_TRIGGER),
                 "check": "Hold all U.S. subsidiaries through one U.S. intermediate holding company (IHC)",
                 "detail": f"U.S. non-branch assets {bn(fp['nonbranch_before'])} → {bn(fp['nonbranch_after'])} (trigger $50bn).",
                 "src": "12 CFR 252.153"})
    c_before, c_after = category(fp["nonbranch_before"])[0], category(fp["nonbranch_after"])[0]
    rows.append({"status": "newly_applies" if rank(c_after) > rank(c_before) else ("applies" if c_after != "None" else "met"),
                 "check": "The IHC's own U.S. category (treated like a U.S. bank holding company)",
                 "detail": f"{cat_label(c_before)} → {cat_label(c_after)}", "src": CATEGORY_CITE})
    o_before, o_after = fp["combined_before"] >= SUBPART_O_TRIGGER, fp["combined_after"] >= SUBPART_O_TRIGGER
    for rule, cite in SUBPART_O_RULES:
        rows.append({"status": _transition(o_before, o_after), "check": rule,
                     "detail": f"Combined U.S. assets incl. branches {bn(fp['combined_before'])} → {bn(fp['combined_after'])} (trigger $100bn).",
                     "src": cite})
    return rows, (c_before, c_after)


# ======================================================================================
# 6. REG K - can the Canadian bank keep its businesses outside the U.S.?  (12 CFR 211.23)
#    A "qualifying foreign banking organization" (QFBO) may do ANY kind of business outside
#    the U.S. and own foreign companies that do some U.S. business (211.23(f)). To qualify,
#    disregarding its U.S. banking, it must pass BOTH tests (211.23(a)), each on 2 of 3 measures:
#      Test 1 - mostly a bank:        banking outside the U.S. > worldwide non-banking
#      Test 2 - mostly outside U.S.:  banking outside the U.S. > banking in the U.S.
#    U.S. territories (Puerto Rico, Guam...) count as the U.S. (211.23, footnote 10).
# ======================================================================================
def qfbo_test(parent_total_assets, us_banking_assets, us_revenue_share=None, us_income_share=None, mostly_banking=None):
    """parent_total_assets and us_banking_assets in thousands of US$ (after the deal).
    us_*_share: % of the parent's banking revenue / net income earned in the U.S. after the deal (None = not given).
    mostly_banking: the user's confirmation for Test 1 (True / False / None)."""
    legs = []
    if parent_total_assets and us_banking_assets is not None:
        outside = parent_total_assets - us_banking_assets
        legs.append({"measure": "Assets", "us_share": 100 * us_banking_assets / parent_total_assets, "pass": outside > us_banking_assets,
                     "detail": f"Outside U.S. {bn(outside)} vs in U.S. {bn(us_banking_assets)}"})
    else:
        legs.append({"measure": "Assets", "us_share": None, "pass": None, "detail": "Need the parent's total assets (Pillar 3)."})
    for name, share in (("Revenue", us_revenue_share), ("Net income", us_income_share)):
        legs.append({"measure": name, "us_share": share, "pass": None if share is None else share < 50,
                     "detail": "Not entered" if share is None else f"{share:.0f}% earned in the U.S."})
    passes = sum(1 for l in legs if l["pass"] is True)
    fails = sum(1 for l in legs if l["pass"] is False)
    test2 = "met" if passes >= 2 else ("not_met" if fails >= 2 else "unknown")
    test1 = {True: "met", False: "not_met", None: "unknown"}[mostly_banking]
    overall = "not_met" if "not_met" in (test1, test2) else ("met" if test1 == test2 == "met" else "unknown")
    headroom = (parent_total_assets - 2 * us_banking_assets) if parent_total_assets else None
    return {"test1": test1, "test2": test2, "overall": overall, "legs": legs, "headroom": headroom}


REG_K_CONSEQUENCES = [
    ("What qualifying allows", "It may do business of any kind outside the U.S., and own foreign companies that also do some U.S. "
     "business — provided more than half of each such company's assets and revenue are outside the U.S., it doesn't underwrite U.S. "
     "securities beyond what U.S. bank holding companies may, and its U.S. financial activities have Fed approval.", "12 CFR 211.23(f)"),
    ("Failing the tests", "Failing for two consecutive years (as shown in its annual FR Y-7 report) ends the exemptions.", "12 CFR 211.23(d)(1)"),
    ("What must then be unwound", "Activities and investments started after the first failing year must be ended or sold within three "
     "months of the second annual report, unless the Fed consents.", "12 CFR 211.23(d)(2)"),
    ("The way out", "The bank can ask the Fed for a specific determination that it remains eligible — even before it fails.", "12 CFR 211.23(e)"),
]


def holdings_flags(holdings):
    """Reg K review of the parent's holdings OUTSIDE the U.S.
    holdings: list of {name, country, activity, us_business}. Returns rows with a flag and the reason."""
    rows = []
    for h in holdings:
        nonbank = h["activity"] in ("Insurance", "Real estate", "Non-financial (commercial)")
        if h.get("us_business") and nonbank:
            flag, why = "not_met", "Non-banking AND does business in the U.S. — needs the 211.23(f)(5) limits to hold, and QFBO status."
        elif h.get("us_business"):
            flag, why = "review", "Does business in the U.S. — check the 211.23(f)(5) limits (more than half of its assets and revenue outside the U.S.)."
        elif nonbank:
            flag, why = "review", "Non-banking business — allowed outside the U.S. only while the parent keeps QFBO status (211.23(f)(1), (3))."
        else:
            flag, why = "met", "Banking or financial business outside the U.S. — no Reg K issue on its own."
        rows.append(dict(h, flag=flag, why=why))
    order = {"not_met": 0, "review": 1, "met": 2}
    return sorted(rows, key=lambda r: (order[r["flag"]], r["name"]))


# ======================================================================================
# 7. THE CANADIAN PARENT UNDER OSFI (C$ millions)
# ======================================================================================
OSFI_DSIB = {"cet1": 8.0, "tier1": 9.5, "total": 11.5, "leverage": 3.5}    # CAR (2027) Ch.1 s.10 (+ DSB); LR Guideline
OSFI_OTHER = {"cet1": 7.0, "tier1": 8.5, "total": 10.5, "leverage": 3.0}


def parent_tests(parent, target_rwa_cad, target_assets_cad, dsib=True, dsb=0.0):
    """Parent's OSFI ratios before/after buying the target for cash at book value
    (capital unchanged; the target's risk-weighted assets and exposure are added)."""
    base = OSFI_DSIB if dsib else OSFI_OTHER
    extra = dsb if dsib else 0
    rows = []
    for name, num, den, add, target, cite in [
        ("CET1 ratio", "cet1", "rwa", target_rwa_cad, base["cet1"] + extra, "OSFI CAR (2027) Ch.1 s.10"),
        ("Tier 1 ratio", "tier1", "rwa", target_rwa_cad, base["tier1"] + extra, "OSFI CAR (2027) Ch.1 s.10"),
        ("Total capital ratio", "total_capital", "rwa", target_rwa_cad, base["total"] + extra, "OSFI CAR (2027) Ch.1 s.10"),
        ("Leverage ratio", "tier1", "leverage_exposure", target_assets_cad, base["leverage"], "OSFI Leverage Requirements Guideline"),
    ]:
        if not parent.get(num) or not parent.get(den):
            rows.append({"test": name, "before": None, "after": None, "target": target, "status": "unknown", "cite": cite})
            continue
        before = 100 * parent[num] / parent[den]
        after = 100 * parent[num] / (parent[den] + add)
        rows.append({"test": name, "before": before, "after": after, "target": target,
                     "status": "met" if after >= target else "not_met", "cite": cite})
    return rows


# ======================================================================================
# 8. STATUS LABELS
# ======================================================================================
STATUS = {
    "met": ("✓", "Met", "#1e7d4f"),
    "warning": ("!", "Inside buffer", "#b26a00"),
    "review": ("!", "Review", "#b26a00"),
    "not_met": ("✕", "Not met", "#b42318"),
    "newly_applies": ("↑", "Newly applies", "#1f3a93"),
    "applies": ("•", "Already applies", "#5b6474"),
    "unknown": ("?", "Can't tell", "#6b7280"),
}
