# AI Usage Transcript

Tool used: **Claude**, as an agentic coding assistant with terminal/file
access (could unzip files, run Python, read/write files directly). This is
a session log — cleaned up for readability but kept close to how the actual
prompts went, including the messy/debugging bits, not a polished script.

---

### Getting oriented

**Me:** hey so i got this zip file for a case study, can you unzip it and show me whats inside

**AI:** unzipped it. There's `BRIEF.md`, `DATA_DICTIONARY.md`,
`SUBMISSION_CHECKLIST.md`, a `reference.db` sqlite file, and a `data/`
folder with `submissions_partnerA.csv`, `partnerB.csv`, `partnerC.csv`.

**Me:** ok read me the brief and the data dictionary, i want the actual rules before we write anything

**AI:** [summarized both files] — basically: build `consolidate.py`, read
`reference.db` with SQL (don't hardcode categories/regions/existing
merchants), validate each submission against 7 rules, normalise the valid
ones, map the free-text category to a canonical one, dedupe, and split into
`clean.csv` / `errors.csv`. Reference date for "today" is fixed at
2026-06-01 so results are reproducible.

**Me:** got it. before touching the csvs, how do we even read reference.db, its sqlite right, what do i need to import

**AI:** just the standard library — `sqlite3`, nothing to pip install.
Something like:
```python
import sqlite3
con = sqlite3.connect("reference.db")
for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    print(row)
```

**Me:** run that and show me whats actually in the three tables, dont want to guess the schema

**AI:** ran it —
- `categories`: 7 rows, e.g. `Food & Beverage`, `Grocery & Convenience`,
  `Electronics & Repair`, `Health & Beauty`, `Fashion & Apparel`,
  `Home & Living`, `Professional Services`
- `region_pic`: 6 regions (Central, Northern, Southern, East Coast, Sabah,
  Sarawak) each with a pic_name + pic_email
- `existing_merchants`: 20 rows, `merchant_id` + `merchant_name`

**Me:** ok cool, now go through the actual csvs before we write rules, i wanna know how messy this really is. check blanks, weird regions, bad emails, phone number lengths, and the date formats

**AI:** wrote a quick profiling script (csv + re, nothing fancy) and ran it
across all three files (195 rows total). Findings:
- blank `merchant_name` in a few rows, blank `region` in 2, blank
  `contact_email` in 2
- regions include invalid ones that aren't in `region_pic`: `Penang`,
  `Klang Valley`, `West`, `KL`
- some obviously broken emails: `@nodomain.com`, `not-an-email`,
  `two@@at.com`, and a couple just blank
- phone formats are all over: `03-1122`, `07-4781139`, `0881985722`,
  `+60-99`, even literal `phone n/a`
- dates come in two formats only: `YYYY-MM-DD` and `D/M/YYYY` — no
  unparseable ones, but a few are *after* the 2026-06-01 reference date
  (like `2026-09-26`, `2026-12-01`)
- 4 rows have `existing_merchant_id` filled in
- found 28 groups of duplicate merchant names (same name, different
  submission_id, sometimes different casing/whitespace)

**Me:** perfect, thats exactly the kind of thing i needed before writing the rules blind. also check if any phone numbers actually use the +60 country code format for real, since the data dictionary mentions normalising that

**AI:** only two rows have `+60` in them at all (`+60-99` on A070 and A003)
and both are too short to be real numbers anyway (they'll get rejected by
the 9-digit rule regardless). So the `+60`/`60`-prefix normalisation logic
needs to be written generically even though this particular dataset barely
exercises it — can't just special-case what's here.

---

### The category classification decision

**Me:** ok so for the business_category_freetext thing, isnt the obvious move to just throw each value at an LLM and ask it to pick a category? thats basically what everyone does for messy text right

**AI:** that would work, but before deciding, let's actually see how varied
the free text is — if it's a small set of patterns, an LLM call per row is
solving a much simpler problem the hard/expensive way.

**Me:** fair, pull me every unique value from business_category_freetext across all three files

**AI:** there are only **43 distinct values** across all 195 rows, e.g.
`"nasi lemak stall"`, `"mini grocer"`, `"phone repair"`, `"n/a"`, `"other"`,
`"general trading sdn bhd"`, etc.

**Me:** yeah ok looking at that list, most of these are just 1-2 keywords away from an obvious category. lets try a keyword mapping instead of calling an LLM for every row — should be free, instant, and way easier to test

**AI:** agreed. Built a keyword-to-category dict (one list of keywords per
canonical category) and a `classify_category()` function that returns the
category only if exactly one category's keywords match — if zero or more
than one match, it returns `None` (unmappable). Ran it against all 43
values to eyeball the mapping.

**Me:** run it and show me the results

**AI:** [printed all 43 → category mappings] — everything mapped correctly
except `n/a`, `other`, and `general trading sdn bhd`, which came back
`None` as expected (those are genuinely not classifiable, they should be
rejected under rule 5).

**Me:** wait "shoe store" — did that map to anything? i dont see "shoe" in the fashion keyword list you showed me

**AI:** good catch — it was returning `None` for "shoe store" and
"streetwear fashion" only had "streetwear", not "shoe". Added `"shoe"` to
the `Fashion & Apparel` keyword list since there's no dedicated footwear
category in `reference.db`. Re-ran, now `shoe store` → `Fashion & Apparel`
correctly.

**Me:** ok good, that keyword approach is way simpler than i expected honestly, lets go with that instead of the LLM idea

---

### Writing the validation rules

**Me:** ok next, walk me through rule 1 to 7 from the data dictionary and lets code them one at a time. also — if a row breaks like 3 rules at once, do we just report the first one or all of them?

**AI:** the brief explicitly says "one row may break several rules," so we
should collect **every** applicable reason, not stop at the first. Wrote
`validate_row()` that checks all 7 rules independently and appends to a
`reasons` list, only marking the row invalid at the end if that list is
non-empty.

**Me:** but if a row has a blank region, are we gonna also say "invalid region" on top of "missing required field"? that seems redundant

**AI:** yeah that'd happen if we check rule 2 unconditionally. Changed it so
rule 2 (valid region), rule 3 (valid email), and rule 5 (mappable category)
only run when that specific field isn't already blank — blank is already
covered by rule 1. Phone and date are the exception: those always run,
because something like `"phone n/a"` should get reported as an *invalid
phone number*, not just "missing," since it's more useful for the partner
to know what's actually wrong with the value they submitted.

**Me:** makes sense. whats the plan for rule 7, the already-onboarded one? it says match by id OR by name

**AI:** load two things from `existing_merchants`: a set of `merchant_id`s,
and a set of normalised names (trimmed, whitespace-collapsed, lowercased).
A row is rejected if either its `existing_merchant_id` is in the id set, OR
its normalised `merchant_name` is in the name set.

---

### Normalisation — and a bug I caught

**Me:** ok now normalisation. merchant name, email, phone, date. lets start with phone since thats the messiest

**AI:** first pass:
```python
def normalise_phone(raw):
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("60"):
        digits = "0" + digits[2:]
    return digits
```

**Me:** hold on, wait — run that on a normal 9-digit local number that happens to start with 60, like if someone typed "601234567" as a typo without a leading zero. wouldnt that get mangled into a wrong number?

**AI:** yeah, that's a real bug — that logic would strip the "60" off ANY
number starting with 60, even a short local-looking one, which isn't
necessarily a country code at all. Fixed it to only treat it as a country
code if the string explicitly starts with `+60`, or if the digit-only
string starts with `60` AND is long enough (10+ digits) to plausibly be a
country code + full number rather than a coincidentally-60-prefixed short
number:
```python
if compact.startswith("+60"):
    return "0" + digits[2:]
if digits.startswith("60") and len(digits) >= 10:
    return "0" + digits[2:]
return digits
```

**Me:** good catch (well, my catch lol) — add a test for that specific case so it doesnt regress

**AI:** added `test_normalise_phone_bare_60_prefix` and a plain-digits test
so both paths are locked in.

**Me:** cool, now dates — two formats, D/M/YYYY and YYYY-MM-DD, and reject if it cant be parsed or if its after 2026-06-01

**AI:** wrote `parse_date()` trying `%Y-%m-%d` then `%d/%m/%Y` in order,
returns `None` if neither works, which rule 6 then treats as "unparseable."

---

### De-duplication

**Me:** ok last big piece, the dedup. same merchant might show up more than once, sometimes just different casing or spacing. whats your suggestion for the matching key

**AI:** normalise the merchant name (trim, collapse whitespace, lowercase)
and use that as the group key. Region isn't part of the key — a merchant is
one real-world entity, and the point of dedup here is to collapse repeat
submissions of the *same* business, not the same business per region.

**Me:** and if two duplicates disagree on details, like different phone numbers, which one do we keep?

**AI:** suggest keeping whichever has the more recent `registration_date`
— reasoning being a later submission is more likely to have corrected
info. Tie-break on the smallest `submission_id` so it's fully
deterministic (no dependency on file read order).

**Me:** fine, go with that, just make sure we track how many got collapsed into each output row so its auditable

**AI:** added a `duplicates_collapsed` column to `clean.csv`.

---

### Running it for real and testing

**Me:** ok run the whole thing on the real data and give me the numbers

**AI:** 195 submissions in → 112 clean merchants, 59 rejected, 24 duplicate
submissions collapsed into those 112.

**Me:** spot check a few rejected rows and make sure the reasons actually make sense, and check the category counts look reasonable across the 7 buckets

**AI:** [showed samples] — rejection reasons break down as: invalid phone
(14), already onboarded (10), invalid region (10), missing required field
(9), unmappable category (8), invalid email (7), future date (3). Category
distribution across the 112 clean rows is fairly even across all 7
canonical categories, nothing looks skewed or wrong.

**Me:** looks right to me. now write proper unit tests for all of this — normalisation functions, the classifier, each rule individually, a row that fails multiple rules at once, and both dedup paths

**AI:** wrote `test_consolidate.py`, 24 tests total, including the
multi-rule-failure case and the phone bug we caught earlier. Ran them —
all pass.

**Me:** run it one more time in a completely fresh venv just to make sure nothing's secretly relying on stuff installed in this environment

**AI:** created a fresh venv, installed from `requirements.txt`, ran
`consolidate.py` and the test suite — both work fine with zero third-party
runtime dependencies (only `csv`, `sqlite3`, `re`, `datetime`, `argparse`,
`dataclasses`, `logging` — all stdlib).

---

### Wrapping up

**Me:** last thing, write the README with the design decisions and judgement calls explained, plus a scaling section and the recurring-pipeline stretch notes, and then a one-page AI_REFLECTION.md covering how we actually used AI here including that phone bug and the LLM-vs-keyword decision

**AI:** wrote both, cross-checked every claim in the README against what
the code actually does before finalising.
