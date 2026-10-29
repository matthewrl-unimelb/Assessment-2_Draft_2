# Bank Merger Regulatory Check

An AI-assisted Streamlit app for a banking M&A lawyer. Choose a U.S. bank to be bought and its buyer, which is either another U.S. bank or one of the four Canadian banks that buy U.S. banks (TD, RBC, BMO, CIBC). The app shows:
- which U.S. capital rules each bank meets;
- what the merger changes;
- for a Canadian buyer, what U.S. regulation it takes on, whether it can keep its businesses at home under Reg K, and which holdings it must review.

**Deployed app:** _add the Streamlit Cloud URL here_

## Workflow
| Page | What it does |
|---|---|
| Home | Choose the deal: the bank being bought, and the buyer. Tick **"The buyer is a Canadian bank"** to choose TD, RBC, BMO or CIBC and load its Pillar 3 report |
| 1 At a glance | Every requirement in one table: met / not met / newly applies / can't tell |
| 2 Capital ratios | CET1, Tier 1 and Total capital for the buyer, the bank bought, and the two combined |
| 3 Leverage & liabilities | The 4% leverage ratio (plus the 3% SLR from Category III) and the 10% liabilities concentration limit |
| 4 Canadian buyer | (A) U.S. presence before and after; (B) the U.S. rules it is now subject to, both as a foreign bank and for its U.S. holding company; (C) the Reg K "qualifying foreign banking organization" tests; (D) holdings to review, outside the U.S. and in it; (E) its Canadian (OSFI) capital ratios |
| 5 Summary & next steps | The full table, next steps, a draft memo checked by a second AI call, and a Word download |
| Size examples | Fictitious deals landing in each category, plus a "try your own" box |

## Design: code decides, AI explains
| File | Role | Class technique |
|---|---|---|
| `engine.py` | Every number and every result, as if-then rules with the regulation cited next to each | Lab 1: if-then logic |
| `library.py` | Closed-world library: 14 regulation documents (incl. Reg K) in ~1,160 section-level chunks, keyword search (BM25 + phrase bonus) | Lab 2: retrieval (RAG) |
| `ai.py` | Explains, drafts, reads Pillar 3 tables and sorts holdings, **only** from what it is given. Code verifies every `[C#]` citation. A reflection call checks Q&A answers and loops with a better search if they fail | Lab 2: RAG + looping + reflection |
| `build_structure.py` | Run once: turns the Fed's NIC ownership files (~1GB of XML, not in this repository) into `library/us_structure.json` | Data preparation |

The AI never browses the web, never does arithmetic and never decides a result. A full run makes **7+ AI calls automatically**:
- 2 data reviews
- the headline on Step 1
- explanations on Steps 2 and 3
- the memo draft, plus its reflection check

With a Canadian buyer, three more are added: reading the Pillar 3 report, sorting the holdings list, and the Step 4 explanation. Every question asked adds 2–4.

## Data sources
- **FR Y-9C** (Fed National Information Center), either as PDF reports or the bulk .txt download. Read **exactly** by code from the forms' MDRM-coded fields.
- **Pillar 3 / supplementary capital disclosure** of the Canadian parent. Code finds the KM1 and LR1 pages, the AI reads them, and code checks the figures against the bank's own reported ratios.
- **NIC ownership data** (`library/us_structure.json`): each Canadian bank's U.S. intermediate holding company, its U.S. companies inside and outside it, its U.S. branches, and the foreign companies it reports to the Fed.
- **Regulations** in `library/chunks.json`:
  - 12 CFR Parts 211 (Reg K), 217, 225, 249, 251 and 252
  - 12 U.S.C. 1828, 1842 and 1852
  - OSFI CAR (2027) Ch. 1–2 and the Leverage Requirements Guideline
  - the Bank Act (Parts IX–X)
  - the Fed's March 2026 capital-proposals memo

## Test cases
_All files are in `test_data/`._

1. **Canadian buyer crosses into Category III (works well).**
   - Home → **Or try an example: Canadian buyer** (RBC buying Associated Banc-Corp) → **Use example** (Pillar 3). Expected: the AI reads RBC's Q3 2026 Pillar 3 (CET1 C$104,956m), and code confirms it (13.49% vs the reported 13.50%).
   - Steps 1–3: RBC's U.S. arm ($204.0bn) plus Associated ($51.4bn) = $255.4bn, so **Category IV → III**. Annual stress tests, the SLR, the countercyclical buffer and fuller liquidity rules newly apply, and all capital ratios are met.
   - Step 4:
     - the IHC and the foreign-bank rules "already apply";
     - the IHC's category moves up;
     - Reg K assets measure: U.S. share 13.9%, met. Enter revenue and net-income shares (e.g. 25% and 30%) and Test 2 becomes met;
     - paste a list of subsidiaries (e.g. an insurer and a real-estate company) and they are flagged for review;
     - one U.S. company outside the IHC is flagged;
     - the OSFI ratios stay above target.
2. **A smaller Canadian bank crosses $100bn (works well).** Same example, but choose **CIBC** as the Canadian bank. Its U.S. arm ($83.7bn) plus Associated = $135.1bn, so the IHC moves from **below $100bn to Category IV**, and the foreign-bank rules (U.S. risk committee and CRO, liquidity buffer, capital stress testing, home-capital certification) all **newly apply**.
3. **Where it struggles.**
   - **Older report layout.** Choose **TD** and click **Use example**: TD's older 2023 disclosure doesn't use the standard KM1 table, so the AI returns several figures as missing, the code check shows "?", and the user must type them in.
   - **Limited Fed records.** BMO lists no foreign holdings, so a full Reg K review needs the bank's own subsidiaries list.
   - **Strict checker.** The Q&A checker sometimes marks correct answers "not fully confirmed".
   - **Off-topic question.** Ask "Who won the World Series?": the app declines, because the answer isn't in its regulations.

## Run
```
pip install -r requirements.txt
python3 -m streamlit run app.py
```
Paste an API key into the left-hand panel. To rebuild the data:
- `python3 library.py build <folder of regulation PDFs>`
- `python3 build_structure.py <folder of NIC XML files>`

_Screening tool, not legal advice. The combined bank is a simple sum of the two banks (no purchase premium); the Reg K test uses assets from the filings and user-entered revenue and income shares. Verify against the filings and the rules cited._
