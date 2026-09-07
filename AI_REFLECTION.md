# AI Reflection

## Which AI tools I used, and for what

I built this end-to-end in one agentic session with **Claude**, used as a
coding assistant with shell/file access (could unzip the package, run
Python directly, and read/write files). See `AI_TRANSCRIPT.md` for the full
session log. I used it for:

- Unzipping and exploring the case study package, and profiling the raw
  CSVs and `reference.db` (unique category strings, region values,
  phone/date formats, duplicate merchant names, existing-merchant overlaps)
  before writing any pipeline code.
- Drafting `consolidate.py`, `test_consolidate.py`, and this documentation.
- Prototyping and testing the free-text category classifier against the
  actual set of 43 distinct values before wiring it into the pipeline.

## What I delegated to AI vs. decided myself

**Delegated to AI:** the mechanical data profiling (writing the throwaway
scripts to count blanks, check regions/emails/phone lengths, find
duplicates), first-draft boilerplate (CSV reading, `argparse`, dataclasses),
and generating the unit test scaffolding once the rules were settled.

**Decided myself (the judgement calls the brief asks for):**
- The de-duplication key (name-only, not name+region) and winner rule
  (most-recent `registration_date`, tie-broken by `submission_id`).
- Rule evaluation order — evaluating *all* seven rules per row rather than
  stopping at the first failure, and suppressing redundant downstream
  messages when a required field is already blank.
- The final call to use a keyword classifier instead of an LLM for the
  category step (see below) — I pushed back on the AI's first suggestion
  here rather than taking it at face value.
- Catching and rejecting the AI's first phone-normalisation logic before it
  ever made it into the pipeline (see below).

## A place the AI was wrong or misleading, and how I caught it

Two, actually — both are in the transcript in full:

1. **The category step.** My first instinct, and the AI's default
   suggestion, was to call an LLM per row to classify the free text — the
   "obvious" answer to messy text classification. Before accepting that, I
   asked it to pull every distinct value from the data first. That showed
   only 43 distinct strings clustering cleanly around a handful of
   keywords per canonical category. An LLM call per row would have been
   slower, non-free, and harder to test deterministically for no real
   benefit here, so I overrode the "reach for an LLM" instinct in favour of
   a keyword rule set — verified against all 43 values before it went into
   the pipeline (only the genuinely unmappable ones, `n/a` / `other` /
   `general trading sdn bhd`, correctly fell through).

2. **The phone normalisation bug.** The AI's first draft of
   `normalise_phone()` stripped a leading `"60"` off *any* digit string
   that started with it, regardless of length. I caught this by asking what
   would happen to a plain local number that coincidentally starts with
   `60` (e.g. a mistyped number missing its leading `0`) — that number
   would get silently mangled into something else entirely, which is worse
   than leaving it alone. The fix: only treat `60` as a country code if the
   raw value explicitly has a `+60` prefix, or the digit string is long
   enough (10+ digits) to plausibly be a country code plus a full local
   number. Added a regression test (`test_normalise_phone_bare_60_prefix`)
   so this can't silently come back.

## Making the category step reliable and affordable at ~50k rows/week

Sticking with rule-based keyword matching as the primary path is the
biggest lever: it's O(rows × keywords), runs in milliseconds for 50k rows,
costs nothing, and is fully deterministic and unit-testable — a value
either matches or it doesn't, and that mapping is captured in test cases,
not in an opaque model call.

If/when the keyword rules stop covering the incoming free text well enough
(new slang, new business types, non-English text), I'd extend the pipeline
rather than fall back to a per-row LLM call:

1. **Route only genuine misses to an LLM, and cache the result.** Any
   free-text value the keyword rules can't classify gets logged. A cheap
   batch job (not the live pipeline) periodically sends the *distinct*
   unclassified strings — not every row — to an LLM with the fixed list of
   canonical categories, asking for exactly one category or "none." At 50k
   rows/week the number of *distinct* unmapped strings is almost certainly
   a tiny fraction of total rows (partners reuse the same phrasing), so
   this keeps LLM calls to maybe dozens per week, not tens of thousands.
2. **Persist the LLM's answers back into the keyword/lookup table** (e.g.
   an exact-string lookup table in `reference.db`) so the same phrase is
   never sent to the LLM twice — the system gets cheaper and faster over
   time instead of paying a per-row cost forever.
3. **Force structured, constrained output** (respond with exactly one of
   the canonical category names, or `NONE`) and validate the response
   against the real `categories` table before accepting it, so a malformed
   or hallucinated response never silently corrupts `clean.csv` — it just
   falls back to rejection, same as an unmappable keyword value today.
4. **Keep a human-reviewable spot-check log** of LLM-classified values (a
   sample, not all of them) so the ops team can periodically confirm the
   cache isn't drifting.

This keeps the hot path — the actual 50k rows/week — cheap, fast, and
deterministic, while still handling genuinely novel free text without
needing a code change every time.
