"""
ai.py - every AI call in the app goes through this file.

Design rule:  AI READS AND EXPLAINS - engine.py CALCULATES AND DECIDES.
So the AI is never asked to do arithmetic or decide pass/fail. It is given:
  * facts the app already computed (numbers, categories, test results), and
  * passages retrieved from the closed-world regulation library (library.py),
and asked to explain, flag or draft - citing the passages by their ID, e.g. [C41].

"Closed world" is enforced in four layers:
  1. Retrieve first: only library passages are ever sent to the AI.
  2. Instructions: answer ONLY from those passages, otherwise say so.
  3. Code check: every [C#] citation must be one of the passages actually sent.
  4. Relevance floor: if the library has nothing relevant, the AI is not called at all.
Plus (Lab 2) a REFLECTION step: a second AI call checks the answer against its passages,
and if it fails, the app LOOPS - rewrites the search, retrieves again and retries.

Providers: Anthropic (Claude Haiku - cheap, for testing) or OpenAI (for submission).
No web access is ever switched on for either provider.
"""

import json
import os
import re
import time

import streamlit as st

DEFAULT_MODELS = {"Anthropic (Claude)": "claude-haiku-5-5", "OpenAI": "gpt-4o-mini"}
RELEVANCE_FLOOR = 4.0     # minimum BM25 score for the best passage before the AI is called
MAX_LOOPS = 2             # reflection retries for the question box

CLOSED_WORLD_RULES = (
    "You are an assistant to a U.S./Canadian banking M&A lawyer. Rules you must follow:\n"
    "1. Use ONLY the numbered passages and the APP FACTS provided in this message. Do not use outside "
    "knowledge, do not mention cases, rules or numbers that are not in them.\n"
    "2. After every sentence that relies on a passage, cite it in square brackets with its ID, e.g. [C41].\n"
    "3. Never do arithmetic and never decide pass/fail yourself - the APP FACTS already contain the "
    "computed numbers and results; repeat them, do not change them.\n"
    "4. If the passages do not answer something, say plainly: 'The loaded regulations do not cover this.'\n"
    "5. Plain English for a busy senior lawyer. No preamble."
)


# --------------------------------------------------------------------------------------
# Session bookkeeping: call counter and audit trail (shown in the sidebar and exported)
# --------------------------------------------------------------------------------------
def _log(step, purpose, prompt, output, passages=None, seconds=0.0):
    st.session_state.setdefault("audit", []).append({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"), "step": step, "purpose": purpose,
        "passages_sent": [p["id"] + " " + p["cite"] for p in (passages or [])],
        "prompt_chars": len(prompt), "output": output, "seconds": round(seconds, 1),
    })
    st.session_state["ai_calls"] = st.session_state.get("ai_calls", 0) + 1


def ready():
    return bool(st.session_state.get("api_key")) or os.environ.get("APP_DEV_MOCK") == "1"


# --------------------------------------------------------------------------------------
# The one function that actually talks to an AI provider
# --------------------------------------------------------------------------------------
def chat(system, user, step, purpose, passages=None, max_tokens=900, want_json=False):
    provider = st.session_state.get("provider", "Anthropic (Claude)")
    model = st.session_state.get("model") or DEFAULT_MODELS[provider]
    key = st.session_state.get("api_key")
    started = time.time()

    if os.environ.get("APP_DEV_MOCK") == "1" and not key:
        text = _mock(user, passages, want_json)
    elif provider == "OpenAI":
        from openai import OpenAI
        client = OpenAI(api_key=key)
        kwargs = {"response_format": {"type": "json_object"}} if want_json else {}
        resp = client.chat.completions.create(
            model=model, max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kwargs)
        text = resp.choices[0].message.content or ""
    else:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        # Low effort: extended "thinking" is switched off. Otherwise the model can spend the whole
        # token allowance thinking and return no text at all (found in live testing).
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system, thinking={"type": "disabled"},
            messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    if not text.strip():
        text = "The AI returned an empty answer. Press the step again or try a shorter question."

    _log(step, purpose, system + user, text, passages, time.time() - started)
    return text


def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def _passages_block(passages):
    return "\n\n".join(f"[{p['id']}] {p['cite']} - {p['title']} (status: {p['status']})\n{p['text']}" for p in passages)


def check_citations(text, passages):
    """Layer 3 of closed world: every [C#] must be a passage we actually sent."""
    sent = {p["id"] for p in passages}
    cited = set(re.findall(r"\[(C\d+)\]", text))
    return {"cited": sorted(cited), "invalid": sorted(cited - sent), "has_citations": bool(cited)}


# --------------------------------------------------------------------------------------
# 1. Grounded explanation for a page (one AI call + code citation check)
# --------------------------------------------------------------------------------------
def explain(step, task, facts, query, lib, k=6):
    passages = lib.search(query, k=k)
    if not passages or passages[0]["score"] < RELEVANCE_FLOOR:
        return {"text": "The loaded regulations do not contain material relevant to this step.", "passages": [], "check": None}
    user = (f"TASK: {task}\n\nAPP FACTS (computed by the app - do not change):\n{facts}\n\n"
            f"PASSAGES:\n{_passages_block(passages)}")
    text = chat(CLOSED_WORLD_RULES, user, step, task[:80], passages)
    check = check_citations(text, passages)
    if check["invalid"]:
        text = _strip_invalid(text, check["invalid"])
    return {"text": text, "passages": passages, "check": check}


def _strip_invalid(text, invalid):
    for cid in invalid:
        text = text.replace(f"[{cid}]", "[citation removed - not in sources]")
    return text


# --------------------------------------------------------------------------------------
# 2. Reflection (Lab 2): does each sentence follow from the cited passages?
# --------------------------------------------------------------------------------------
def reflect(step, answer, passages, question):
    user = (
        "You are a strict checker. Compare the ANSWER with the PASSAGES it cites.\n"
        "Return JSON only: {\"supported\": true/false, \"answered\": true/false, \"unsupported\": [\"sentence\", ...], "
        "\"better_search\": \"keywords likely to appear in the regulation text that would answer the question, "
        "e.g. section topics and defined terms\"}.\n"
        "supported = every statement is backed by its cited passage (saying the regulations do not cover something "
        "counts as supported). answered = the passages actually answered the question asked.\n\n"
        f"QUESTION: {question}\n\nANSWER:\n{answer}\n\nPASSAGES:\n{_passages_block(passages)}")
    verdict = _parse_json(chat("Return only valid JSON.", user, step, "Reflection check", passages, max_tokens=400, want_json=True))
    verdict.setdefault("supported", False)
    verdict.setdefault("answered", True)
    verdict.setdefault("unsupported", [])
    verdict.setdefault("better_search", "")
    return verdict


# --------------------------------------------------------------------------------------
# 3. The question box: retrieve -> answer -> reflect -> loop (Lab 2 RAG + looping + reflection)
# --------------------------------------------------------------------------------------
def ask(step, question, facts, lib):
    query, trail, best = question, [], None
    for attempt in range(1, MAX_LOOPS + 1):
        passages = lib.search(query, k=6)
        if not passages or passages[0]["score"] < RELEVANCE_FLOOR:
            trail.append(f"Attempt {attempt}: nothing relevant found for '{query}' - AI not called.")
            return {"text": "I couldn't find that in the regulations loaded into this app.", "passages": [], "trail": trail, "verified": True}
        user = (f"QUESTION: {question}\n\nAPP FACTS about the deal on screen (computed by the app):\n{facts}\n\n"
                f"PASSAGES:\n{_passages_block(passages)}")
        answer = chat(CLOSED_WORLD_RULES, user, step, f"Q&A attempt {attempt}", passages)
        cites = check_citations(answer, passages)
        if cites["invalid"]:
            answer = _strip_invalid(answer, cites["invalid"])
        verdict = reflect(step, answer, passages, question)
        trail.append(f"Attempt {attempt}: searched '{query}', cited {', '.join(cites['cited']) or 'nothing'}; "
                     f"reflection says {'SUPPORTED' if verdict['supported'] else 'NOT SUPPORTED'}"
                     f"{'' if verdict['answered'] else ' but the passages did NOT answer the question'}.")
        ok = verdict["supported"] and not cites["invalid"]
        if ok:
            best = {"text": answer, "passages": passages, "trail": trail, "verified": True}
            if verdict["answered"]:
                return best
        # LOOP: search again with the checker's better keywords
        query = verdict["better_search"] or question + " definition requirement"
    if best:
        return best
    return {"text": answer + "\n\n_⚠️ The checker could not confirm every sentence of this answer against the regulations. "
            "Treat it as a lead, not a conclusion._", "passages": passages, "trail": trail, "verified": False,
            "unsupported": verdict.get("unsupported", [])}


# --------------------------------------------------------------------------------------
# 4. Data review of an uploaded bank (no passages - the AI only looks at the figures)
# --------------------------------------------------------------------------------------
def review_bank(step, bank_name, figures_text, checks_text):
    user = (
        "Below are figures the app read from a bank holding company's FR Y-9C filing, and the app's own "
        "arithmetic cross-checks. In at most 5 short bullet points: (1) describe the bank's size and capital "
        "position in plain English, (2) flag anything unusual a lawyer should double-check (e.g. a negative or "
        "missing figure, a large AOCI loss, heavy reliance on goodwill). Do not recompute anything, do not cite "
        "law, and do not speculate beyond the figures.\n\n"
        f"BANK: {bank_name}\nFIGURES:\n{figures_text}\n\nAPP CROSS-CHECKS:\n{checks_text}")
    return chat("You are a careful bank-regulatory analyst.", user, step, f"Data review: {bank_name}", max_tokens=450)


# --------------------------------------------------------------------------------------
# 5. Extracting figures from a PDF (e.g. a Canadian parent's Pillar 3 / capital disclosure)
# --------------------------------------------------------------------------------------
PARENT_FIELDS = {
    "bank_name": "Name of the bank",
    "period": "Quarter-end date of the most recent column",
    "total_assets": "Total consolidated assets (balance sheet / LR1 'total consolidated assets as per published financial statements')",
    "cet1": "Common Equity Tier 1 (CET1) capital",
    "tier1": "Tier 1 capital",
    "total_capital": "Total capital",
    "rwa": "Total risk-weighted assets (RWA)",
    "leverage_exposure": "Total leverage ratio exposure measure",
    "cet1_ratio": "CET1 ratio (%)",
    "leverage_ratio": "Leverage ratio (%)",
}
AMOUNT_FIELDS = ("total_assets", "cet1", "tier1", "total_capital", "rwa", "leverage_exposure")


def extract_parent_figures(step, pages_text):
    """AI reads the KM1 'key metrics' and LR1 tables. Code then checks the result (verify_parent)."""
    user = (
        "Below are pages from a Canadian bank's Pillar 3 / supplementary regulatory capital disclosure. "
        "Tables have several quarter columns: use ONLY the FIRST (most recent) column. "
        "Amounts are in millions of Canadian dollars - copy them exactly as numbers (no commas). "
        "Ratios as percentages (e.g. 13.5). If a figure is not shown, use null. Never calculate or guess. "
        "For each figure give a short quote of the row you used.\n"
        "Return JSON only, in this shape: {"
        + ", ".join(f'"{k}": {{"value": ..., "found_as": "..."}}' for k in PARENT_FIELDS)
        + "}\n\nFIGURES WANTED:\n" + "\n".join(f"- {k}: {v}" for k, v in PARENT_FIELDS.items())
        + f"\n\nPAGES:\n{pages_text[:40000]}")
    raw = _parse_json(chat("Return only valid JSON.", user, step, "Read parent's Pillar 3 report", max_tokens=1500, want_json=True))
    out = {}
    for k in PARENT_FIELDS:
        item = raw.get(k) if isinstance(raw.get(k), dict) else {"value": raw.get(k), "found_as": ""}
        v = item.get("value")
        if k in AMOUNT_FIELDS or k in ("cet1_ratio", "leverage_ratio"):
            try:
                v = float(str(v).replace(",", "").replace("%", "")) if v not in (None, "", "null") else None
            except ValueError:
                v = None
        out[k] = {"value": v, "found_as": item.get("found_as", "")}
    return out


def verify_parent(fig):
    """CODE check of the AI's extraction: do the amounts reproduce the bank's own reported ratios?"""
    v = {k: fig.get(k, {}).get("value") for k in fig}
    checks = []
    for label, num, den, reported in (("CET1 ratio", "cet1", "rwa", "cet1_ratio"), ("Leverage ratio", "tier1", "leverage_exposure", "leverage_ratio")):
        if v.get(num) and v.get(den) and v.get(reported):
            calc = 100 * v[num] / v[den]
            checks.append({"check": label, "ok": abs(calc - v[reported]) < 0.15,
                           "result": f"{calc:.2f}% recomputed vs {v[reported]:.2f}% reported"})
        else:
            checks.append({"check": label, "ok": None, "result": "a figure is missing"})
    if v.get("tier1") and v.get("cet1"):
        checks.append({"check": "Tier 1 ≥ CET1", "ok": v["tier1"] >= v["cet1"], "result": "consistent" if v["tier1"] >= v["cet1"] else "inconsistent"})
    return checks


def find_capital_pages(pdf_reader):
    """CODE (not AI) finds the KM1 key-metrics and LR1 leverage pages, skipping the table of contents,
    so the AI reads 2-4 pages instead of 100."""
    import re
    keep = []
    for i, page in enumerate(pdf_reader.pages):
        t = page.extract_text() or ""
        head = t[:400].upper()
        if "TABLE OF CONTENTS" in head or "INDEX" in head:
            continue
        is_km1 = re.search(r"KM1", t) and re.search(r"Common Equity Tier 1", t, re.I) and re.search(r"leverage", t, re.I)
        is_lr1 = re.search(r"LR1", t) and re.search(r"[Tt]otal consolidated assets", t)
        if is_km1 or is_lr1:
            keep.append((i, t))
        if len(keep) >= 4:
            break
    if not keep:  # fall back: pages that mention the key words most
        scored = sorted(((sum(t.lower().count(w) for w in ("cet1", "risk-weighted", "leverage")), i, t)
                         for i, t in ((i, p.extract_text() or "") for i, p in enumerate(pdf_reader.pages))), reverse=True)[:4]
        keep = sorted((i, t) for _, i, t in scored)
    return "\n\n".join(f"--- page {i + 1} ---\n{t}" for i, t in keep), [i + 1 for i, _ in keep]


# --------------------------------------------------------------------------------------
# 6. Reading the parent's list of subsidiaries (annual report) for the Reg K holdings review.
#    The AI only READS and CLASSIFIES each holding into a fixed list; engine.holdings_flags decides.
# --------------------------------------------------------------------------------------
ACTIVITIES = ["Banking", "Holding company", "Lending / finance", "Securities & investment", "Insurance", "Funds & trusts",
              "Real estate", "Other financial", "Non-financial (commercial)", "Not classified"]


def find_subsidiary_pages(pdf_reader, max_pages=6):
    """CODE picks the pages that list subsidiaries, so the AI reads a few pages, not the whole annual report."""
    scored = []
    for i, page in enumerate(pdf_reader.pages):
        t = page.extract_text() or ""
        low = t.lower()
        score = low.count("subsidiar") * 3 + low.count("limited") + low.count("inc.") + low.count("llc")
        if "subsidiar" in low and score >= 8:
            scored.append((score, i, t))
    keep = sorted(sorted(scored, reverse=True)[:max_pages], key=lambda x: x[1])
    return "\n\n".join(f"--- page {i + 1} ---\n{t}" for _, i, t in keep), [i + 1 for _, i, _ in keep]


def classify_holdings(step, text, parent_name):
    user = (
        f"Below is a list of companies owned by {parent_name} (from its annual report or a list the user pasted). "
        "For EACH company give: name; country where it is incorporated or mainly operates; its main business, chosen ONLY from "
        f"this list: {ACTIVITIES}; and us_business = true only if the text says it operates or has offices in the United States. "
        "Do not invent companies. Skip U.S. companies (those are reviewed separately). Return JSON only: "
        '{"holdings": [{"name": "...", "country": "...", "activity": "...", "us_business": false, "found_as": "short quote"}]}'
        f"\n\nTEXT:\n{text[:40000]}")
    raw = _parse_json(chat("Return only valid JSON.", user, step, "Classify the parent's holdings", max_tokens=3000, want_json=True))
    out = []
    for h in raw.get("holdings", []) if isinstance(raw.get("holdings"), list) else []:
        if not isinstance(h, dict) or not h.get("name"):
            continue
        country = str(h.get("country") or "Unknown")
        if country.strip().lower() in ("united states", "usa", "u.s.", "us"):
            continue
        out.append({"name": str(h["name"]).strip(), "country": country,
                    "activity": h.get("activity") if h.get("activity") in ACTIVITIES else "Not classified",
                    "us_business": bool(h.get("us_business")), "found_as": str(h.get("found_as") or "")})
    return out


# --------------------------------------------------------------------------------------
# Offline test mode (developer only: set APP_DEV_MOCK=1). Lets the plumbing be tested without a key.
# --------------------------------------------------------------------------------------
def _mock(user, passages, want_json):
    if want_json and "strict checker" in user:
        return json.dumps({"supported": True, "unsupported": [], "better_search": ""})
    if want_json and "companies owned by" in user:
        return json.dumps({"holdings": [{"name": "Mock Insurance Co.", "country": "Canada", "activity": "Insurance", "us_business": True}]})
    if want_json:
        return json.dumps({"bank_name": {"value": "Mock Bank"}, "period": {"value": "mock"}})
    cite = f" [{passages[0]['id']}]" if passages else ""
    return f"(Offline test mode - no AI was called.) This is placeholder text standing in for the AI's explanation{cite}."
