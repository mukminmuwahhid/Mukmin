# Merchant Onboarding Consolidation Tool

Turns the three weekly partner submission spreadsheets into a clean,
onboard-ready list plus a routable error report.

## How to run

```bash
python -m venv venv && source venv/bin/activate   # venv\Scripts\activate on Windows
pip install -r requirements.txt
python consolidate.py
```

By default it reads `data/submissions_partner*.csv` and `reference.db` from
the current directory and writes `clean.csv` / `errors.csv` next to it. All
three are overridable:

```bash
python consolidate.py --data-dir data --db reference.db --out-dir .
```

Run the tests with:

```bash
python -m unittest test_consolidate.py -v
# or, if pytest is installed:
pytest test_consolidate.py -v
```

## How it works

1. **Load reference data** (`load_reference`) — canonical categories,
   region→PIC mapping, and already-onboarded merchants are read straight
   from `reference.db` with SQL. Nothing from the DB is hardcoded in Python;
   if `reference.db` changes, the tool picks it up automatically.
2. **Load submissions** — all `submissions_partner*.csv` files are read and
   tagged with their source filename, then processed together in one pass.
3. **Validate** (`validate_row`) — each row is checked against **all seven**
   rules independently (not short-circuited), so a row that breaks three
   rules gets three reasons in `errors.csv`, not just the first one found.
4. **Normalise & classify** — rows that pass every rule are normalised
   (name casing/whitespace, phone digits, ISO dates, lower-cased email) and
   their free-text category is mapped to a canonical one.
5. **De-duplicate** (`deduplicate`) — valid records are grouped by a
   normalised merchant name and collapsed to one row per group.
6. **Write outputs** — `clean.csv` and `errors.csv`.

## Key design decisions & judgement calls

- **Evaluate all rules, not just the first failure.** The brief explicitly
  notes "one row may break several rules," so `validate_row` collects every
  applicable reason rather than stopping at the first. This gives partners a
  complete fix-list in one round trip instead of a slow back-and-forth.

- **Rule evaluation order avoids redundant/confusing messages.** If a field
  is blank, rule 1 ("missing required field") already explains it, so rules
  2/3/5 (region/email/category) are skipped for that specific blank field —
  otherwise every blank row would also get a spurious "not a valid X"
  message. Phone and date are always evaluated even when technically blank,
  because a garbage value like `"phone n/a"` should be reported as an
  *invalid phone*, which is more actionable for a partner than "missing."

- **Category classification: rule-based keyword matching, not an LLM call
  per row.** See [AI_REFLECTION.md](./AI_REFLECTION.md) for the full
  reasoning; in short, the ~40 free-text variants in real data cluster
  cleanly around a handful of keywords per canonical category, so a
  keyword-membership classifier (`classify_category`) is deterministic,
  free, instant, unit-testable, and gets every value in the sample data
  right. A value that matches zero categories (`"n/a"`, `"other"`) or
  *more than one* category (ambiguous) is treated as unmappable and
  rejected per rule 5, rather than guessed.

- **Phone normalisation heuristic.** Malaysian domestic numbers always start
  with a `0` area code, so any number that arrives with an explicit `+60`
  prefix, or as a bare `60...` string of 10+ digits (i.e., long enough to be
  a country code + full number rather than a coincidental local number), is
  treated as a `+60` international number and rewritten with a leading `0`.
  Anything else is just stripped to digits. This handled every phone value
  in the sample data correctly; it's the one rule most worth revisiting with
  real-world edge cases (e.g. actual overseas numbers).

- **De-duplication key: normalised merchant name only** (trimmed, whitespace
  collapsed, lower-cased) — *not* name+region. Rationale: the sample data's
  duplicates are the same merchant re-submitted by a different partner or
  with cleaned-up formatting, and a merchant is a single real-world entity,
  not one-per-partner. Region is assumed stable per merchant, so this key is
  sufficient in practice; if a merchant legitimately expands across regions
  under the same name, that would currently over-collapse — flagged as a
  known limitation.

- **De-duplication winner rule:** keep the record with the **most recent
  `registration_date`**, tie-broken by the lexicographically smallest
  `source_submission_id` (fully deterministic, no reliance on file
  ordering). Most-recent was chosen on the assumption that a later
  submission is more likely to carry corrected/updated details than an
  earlier one; `duplicates_collapsed` records how many source rows fed into
  each output row, for auditability.

- **Output columns follow the brief's suggested schema** unchanged, since
  they already covered everything needed for both onboarding and error
  routing.

- **Errors routed even when the region itself is invalid or missing.** In
  that case there's no PIC to route to, so `region_pic_email` is set to a
  clearly-flagged placeholder (`"UNKNOWN - invalid region"`) rather than
  silently blank, so nothing falls through the cracks unnoticed.

## Results on the provided sample data

195 submissions in → **112 clean merchants**, **59 rejected**, **24**
duplicate submissions collapsed into the 112. Rejection reasons break down
roughly as: invalid phone (14), already onboarded (10), invalid region (10),
missing required field (9), unmappable category (8), invalid email (7),
future registration date (3) — several rows contribute to more than one
count since a row can fail multiple rules.

## How this would scale / what I'd change first

The current script is a single-pass, in-memory batch job — fine for
tens-of-thousands of rows a week, but it has three assumptions that
wouldn't survive a jump to a **daily, multi-trigger, crash-halfway**
pipeline (see also the stretch note below):

1. **No idempotency / resumability.** Re-running today overwrites the
   outputs cleanly, but there's no record of *which submissions were
   already processed*, so a crash mid-run followed by a naive re-run could
   double-onboard or silently drop rows once inputs start arriving
   continuously rather than as three fixed files. I'd add a small
   "processed submission IDs" table/log in SQLite (or the existing
   reference DB) that the pipeline consults and updates transactionally,
   so re-runs are idempotent by `submission_id`.
2. **No concurrency control.** If several ops staff can trigger the same
   run concurrently, two processes could read/write `clean.csv` at the same
   time. I'd move outputs into a real database table (even SQLite with a
   lock, or Postgres) instead of flat CSVs, and/or add a simple run-lock.
3. **No structured logging / metrics for monitoring at scale.** Right now
   success is "did clean.csv and errors.csv get written." At real volume
   I'd emit run-level metrics (rows in, rows out, rejection-reason
   breakdown, run duration) to whatever the team's monitoring stack is, and
   alert if the rejection rate spikes — that's usually the earliest signal
   that a partner changed their export format upstream.

## Stretch: turning this into a recurring pipeline

Not required, but since it'll come up live — a few bullets on what changes:

- **Trigger model:** move from "run manually on three files" to a
  scheduled job (daily) that also supports on-demand triggers by ops staff,
  reading from a landing folder/queue rather than fixed filenames.
- **Idempotency:** key every run by a batch ID; track processed
  `submission_id`s so a crash-and-retry never double-processes a row.
- **Partial-failure recovery:** process and commit in small batches (e.g.
  per source file, or per N rows) rather than one giant in-memory pass, so
  a crash loses at most one batch, not the whole run.
- **Concurrency:** a lightweight lock (DB row lock, or a `SELECT ... FOR
  UPDATE`-style mechanism) so two ops people triggering it back-to-back
  don't race on the same input files.
- **Output destination:** write `clean` records into a proper onboarding
  queue/table with a status column, not a CSV that gets overwritten each
  run; append to a persistent `errors` table instead of replacing it, so
  history isn't lost.
- **Observability:** structured run logs + metrics (counts, timing,
  rejection-reason histogram) and alerting on anomalies (e.g. sudden spike
  in one rejection reason usually means a partner's export format changed).
