# AI Usage Transcript (session summary)

Tool: Claude, used as an agentic coding assistant with shell/file access in
one continuous session. This is a summary of the actual working session
(prompts I gave it and what it did), not a polished script.

---

**1.** Unzipped the case study package and had Claude read `BRIEF.md`,
`DATA_DICTIONARY.md`, and `SUBMISSION_CHECKLIST.md` in full before writing
any code.

**2.** Asked it to inspect `reference.db` via SQL directly (`sqlite3` in
Python) to see the actual `categories`, `region_pic`, and
`existing_merchants` tables/rows, rather than assuming their shape from the
data dictionary alone.

**3.** Asked it to profile all three `submissions_partner*.csv` files:
- collect every distinct `business_category_freetext` value (found 43
  unique values) — this drove the design of the keyword classifier,
- check for blank required fields, invalid regions, malformed emails,
  short/garbage phone numbers, and out-of-range/unparseable dates,
- check for duplicate merchant names across/within files (found 28 groups),
- check for `+60`/`60`-prefixed phone numbers to validate the normalisation
  rule.

**4.** Had it prototype the free-text category classifier in isolation
(`test_classify.py` scratch file) and print its output against all 43
distinct values, so the keyword lists could be checked/corrected by eye
before being wired into the main pipeline. Adjusted a couple of keyword
lists after seeing the output (e.g. confirming "shoe store" should land in
Fashion & Apparel, not a separate footwear bucket, since the canonical list
has no footwear category).

**5.** Had it draft the full `consolidate.py`: reference loading via SQL,
per-row validation against all seven rules (collecting *all* applicable
failure reasons rather than stopping at the first), normalisation, category
classification, de-duplication, and CSV output — following the specific
column layout suggested in `DATA_DICTIONARY.md`.

**6.** Ran the script against the real data and reviewed the output:
195 rows in → 112 clean / 59 errors / 24 duplicates collapsed; spot-checked
the category distribution and the rejection-reason breakdown for
plausibility (e.g. confirming the 3 future-dated rows and the 10
already-onboarded rows matched what was seen during profiling in step 3).

**7.** Had it write `test_consolidate.py` covering normalisation helpers,
the classifier, each of the seven validation rules individually and in
combination (a row failing multiple rules at once), and both branches of
de-duplication (collapsing vs. pass-through). All 24 tests pass.

**8.** Discussed the category-classification design trade-off explicitly:
initial instinct was an LLM-per-row approach; after profiling the data
(step 3) it was clear a keyword rule set covered every value cleanly, so
that replaced the LLM idea for the hot path — see `AI_REFLECTION.md` for
the full reasoning and the scaling plan for genuinely novel free text.

**9.** Asked for the README's design-decisions section and the scaling /
recurring-pipeline stretch notes, then edited/tightened the wording and
confirmed each claim against what the code actually does.
