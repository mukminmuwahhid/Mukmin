# AI Reflection

## Which AI tools I used, and for what

I built this end-to-end in a single agentic session with **Claude** (used as a
coding assistant with shell access to inspect the data, write/run code, and
iterate). I used it for:

- Unzipping and exploring the case study package, and profiling the raw CSVs
  and `reference.db` (unique category strings, region values, phone/date
  formats, duplicate merchant names, existing-merchant overlaps) before
  writing any pipeline code.
- Drafting the first version of `consolidate.py`, `test_consolidate.py`, and
  this documentation.
- Prototyping and testing the free-text category classifier against the
  actual set of ~43 distinct values before wiring it into the pipeline.

## What I delegated to AI vs. decided myself

**Delegated to AI:** the mechanical exploration (writing quick `python3 -c`
snippets to profile the CSVs/DB), first-draft boilerplate (CSV reading,
`argparse` setup, dataclasses), and generating the unit test scaffolding
once the validation rules were settled.

**Decided myself (the judgement calls the brief asks for):**
- The de-duplication key (name-only, not name+region) and winner rule
  (most-recent `registration_date`, tie-broken by `submission_id`).
- Rule evaluation order — specifically, evaluating *all* seven rules per row
  rather than stopping at the first failure, and suppressing a couple of
  redundant downstream messages when a required field is already blank.
- The final call on the category-classification approach (see below) — I
  didn't just accept the AI's first suggestion here.
- The phone-normalisation heuristic for distinguishing a genuine `+60`/`60`
  country code from a coincidental local number.

## A place the AI was wrong or misleading, and how I caught it

My first instinct (and the AI's default suggestion) for the free-text
category mapping was to reach for an LLM call per row — it's the "obvious"
answer to "classify this messy text into a category." But once I actually
profiled the data, I found the ~43 distinct free-text values cluster very
cleanly around a small set of keywords per canonical category (e.g. every
food-related value contains one of `restaurant/cafe/kopitiam/mamak/
bakery/...`). An LLM call per row would have been slower, non-free, harder
to unit-test deterministically, and introduces a failure mode (hallucinated
or inconsistent category) that a keyword rule simply can't have. I tested
the keyword classifier against every distinct value in the sample data
before committing to it (see `test_classify.py`-style exploration in the
transcript) and only the genuinely unmappable ones (`n/a`, `other`,
`general trading sdn bhd`) failed to match — exactly the intended
behaviour under rule 5. So I overrode the "reach for an LLM" instinct with a
rule-based classifier, and kept the LLM option in reserve only for
genuinely novel free text the keyword rules don't cover (see below).

## Making the category step reliable and affordable at ~50k rows/week

Sticking with rule-based keyword matching as the primary path is the
biggest lever: it's O(rows × keywords), runs in milliseconds for 50k rows,
costs nothing, and is fully deterministic and unit-testable — a value either
matches or it doesn't, and that mapping is captured in test cases, not in an
opaque model call.

If/when the keyword rules stop covering the incoming free text well enough
(new slang, new business types, non-English text), I'd extend the pipeline
rather than fall back to a per-row LLM call:

1. **Route only genuine misses to an LLM, and cache the result.** Any
   free-text value the keyword rules can't classify gets logged. A cheap
   batch job (not the live pipeline) periodically sends the *distinct*
   unclassified strings — not every row — to an LLM with the fixed list of
   canonical categories, asking for exactly one category or "none." At 50k
   rows/week the number of *distinct* unmapped strings is almost certainly a
   tiny fraction of total rows (partners reuse the same phrasing), so this
   keeps LLM calls to maybe dozens per week, not tens of thousands.
2. **Persist the LLM's answers back into the keyword/lookup table** (e.g. an
   exact-string lookup table in `reference.db`) so the same phrase is never
   sent to the LLM twice — the system gets cheaper and faster over time
   instead of paying a per-row cost forever.
3. **Force structured, constrained output** (respond with exactly one of the
   canonical category names, or `NONE`) and validate the response against
   the real `categories` table before accepting it, so a malformed or
   hallucinated response never silently corrupts `clean.csv` — it just falls
   back to rejection, same as an unmappable keyword value today.
4. **Keep a human-reviewable spot-check log** of LLM-classified values (a
   sample, not all of them) so the ops team can periodically confirm the
   cache isn't drifting.

This keeps the hot path — the actual 50k rows/week — cheap, fast, and
deterministic, while still handling genuinely novel free text without
needing a code change every time.
