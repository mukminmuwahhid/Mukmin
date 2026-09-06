"""
consolidate.py
================

Reads the three regional-partner merchant-submission CSVs, validates and
normalises them against the business rules in DATA_DICTIONARY.md, classifies
each merchant's free-text business category into a canonical category,
de-duplicates repeat submissions, and writes two outputs:

    clean.csv   -- validated, normalised, de-duplicated merchants ready to onboard
    errors.csv  -- every rejected submission with a human-readable reason and
                   the regional PIC email to route it to

Usage:
    python consolidate.py
    python consolidate.py --data-dir data --db reference.db --out-dir .

See README.md for design decisions and assumptions.
"""

from __future__ import annotations

import argparse
import csv
import glob
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("consolidate")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REFERENCE_DATE = date(2026, 6, 1)  # fixed "today" per the brief, for reproducibility

REQUIRED_FIELDS = [
    "merchant_name",
    "region",
    "contact_phone",
    "contact_email",
    "registration_date",
    "business_category_freetext",
]

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Keyword -> canonical category rules for the free-text classifier.
# See README.md ("The AI / category-mapping component") for why a rule-based
# approach was chosen over an LLM call per row, and how it would be extended.
CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "Food & Beverage": [
        "restaurant", "cafe", "kopitiam", "mamak", "bakery", "catering",
        "bubble tea", "roti canai", "nasi lemak", "food", "beverage",
        "eatery", "diner", "bistro", "warung", "tea house", "tea shop",
    ],
    "Grocery & Convenience": [
        "grocer", "grocery", "convenience store", "mini market", "minimarket",
        "sundry", "provision", "neighbourhood mart", "mart", "supermarket",
    ],
    "Electronics & Repair": [
        "electronics", "gadget", "handphone", "phone repair", "computer",
        "laptop", "it services", "it repair", "repair", "mobile accessories",
        "tech",
    ],
    "Health & Beauty": [
        "salon", "barber", "spa", "beauty", "cosmetic", "wellness", "nail",
        "lash", "hair",
    ],
    "Fashion & Apparel": [
        "fashion", "apparel", "boutique", "clothing", "tailor", "shoe",
        "streetwear", "baju kurung", "garment",
    ],
    "Home & Living": [
        "furniture", "home decor", "hardware", "household", "kitchenware",
        "home & living", "decor", "home goods",
    ],
    "Professional Services": [
        "accounting", "bookkeeping", "legal", "consultancy",
        "marketing agency", "printing", "signage", "professional services",
        "audit", "tax",
    ],
}


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

@dataclass
class Reference:
    categories: set                      # canonical category names
    region_pic: Dict[str, Tuple[str, str]]   # region -> (pic_name, pic_email)
    existing_ids: set                    # existing merchant_id values
    existing_names: set                  # normalised existing merchant names


def load_reference(db_path: Path) -> Reference:
    """Load canonical categories, region->PIC mapping, and already-onboarded
    merchants from reference.db using SQL. Nothing here is hardcoded in
    Python -- if the reference data changes, this tool adapts automatically.
    """
    con = sqlite3.connect(str(db_path))
    try:
        categories = {
            row[0] for row in con.execute("SELECT canonical_name FROM categories")
        }
        region_pic = {
            row[0]: (row[1], row[2])
            for row in con.execute(
                "SELECT region, pic_name, pic_email FROM region_pic"
            )
        }
        existing_ids = {
            row[0] for row in con.execute("SELECT merchant_id FROM existing_merchants")
        }
        existing_names = {
            normalise_name(row[0])
            for row in con.execute("SELECT merchant_name FROM existing_merchants")
        }
    finally:
        con.close()

    log.info(
        "Loaded reference data: %d categories, %d regions, %d existing merchants",
        len(categories), len(region_pic), len(existing_ids),
    )
    return Reference(categories, region_pic, existing_ids, existing_names)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def normalise_name(name: str) -> str:
    """Collapse whitespace and lower-case, for matching/dedup keys."""
    return re.sub(r"\s+", " ", name.strip()).lower()


def display_name(name: str) -> str:
    """Trim, collapse whitespace, and title-case for the human-facing output."""
    collapsed = re.sub(r"\s+", " ", name.strip())
    return collapsed.title()


def normalise_email(email: str) -> str:
    return email.strip().lower()


def normalise_phone(raw: str) -> str:
    """Digits only; a leading +60 / 60 country code is converted to a
    leading 0 (Malaysian local format). See README for the exact heuristic
    used to distinguish a "60" country code from a coincidental local
    number, since Malaysian domestic numbers never start with 60.
    """
    raw = raw.strip()
    compact = raw.replace(" ", "").replace("-", "")
    digits = re.sub(r"\D", "", raw)

    if compact.startswith("+60"):
        return "0" + digits[2:]
    if digits.startswith("60") and len(digits) >= 10:
        return "0" + digits[2:]
    return digits


def parse_date(raw: str) -> Optional[date]:
    """Try both formats seen in the data: YYYY-MM-DD and D/M/YYYY."""
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def classify_category(freetext: str) -> Optional[str]:
    """Map free-text business category to exactly one canonical category
    using keyword matching. Returns None if unmappable (no match, or an
    ambiguous match against more than one category).
    """
    text = freetext.strip().lower()
    if not text:
        return None
    matched = set()
    for canonical, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            matched.add(canonical)
    if len(matched) == 1:
        return matched.pop()
    return None


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    ok: bool
    reasons: List[str] = field(default_factory=list)
    normalised: Optional[dict] = None


def validate_row(row: dict, ref: Reference) -> ValidationResult:
    reasons: List[str] = []

    # Rule 1: required fields
    blank_fields = [f for f in REQUIRED_FIELDS if not row.get(f, "").strip()]
    for f in blank_fields:
        reasons.append(f"Missing required field: {f}")

    region_val = row["region"].strip()
    email_val = row["contact_email"].strip()
    category_val = row["business_category_freetext"].strip()
    date_val = row["registration_date"].strip()

    # Rule 2: valid region (only meaningful if region present)
    if region_val and region_val not in ref.region_pic:
        reasons.append(f"Invalid region: '{region_val}' is not a recognised region")

    # Rule 3: valid email
    if email_val and not EMAIL_RE.match(email_val):
        reasons.append(f"Invalid email format: '{email_val}'")

    # Rule 4: valid phone (>= 9 digits after normalisation)
    normalised_phone = normalise_phone(row["contact_phone"])
    if len(normalised_phone) < 9:
        reasons.append(
            f"Invalid phone number: '{row['contact_phone']}' has fewer than 9 digits"
        )

    # Rule 5: mappable category
    canonical_category = classify_category(category_val) if category_val else None
    if category_val and canonical_category is None:
        reasons.append(
            f"Unmappable business category: '{category_val}' does not match "
            "any canonical category"
        )

    # Rule 6: sane date
    parsed_date = parse_date(date_val) if date_val else None
    if date_val:
        if parsed_date is None:
            reasons.append(f"Unparseable registration date: '{date_val}'")
        elif parsed_date > REFERENCE_DATE:
            reasons.append(
                f"Registration date is in the future: '{date_val}' is after "
                f"the reference date {REFERENCE_DATE.isoformat()}"
            )

    # Rule 7: not already onboarded (by id OR by name)
    existing_id = row.get("existing_merchant_id", "").strip()
    name_key = normalise_name(row["merchant_name"]) if row["merchant_name"].strip() else None
    already_onboarded = False
    if existing_id and existing_id in ref.existing_ids:
        already_onboarded = True
    if name_key and name_key in ref.existing_names:
        already_onboarded = True
    if already_onboarded:
        reasons.append("Merchant is already onboarded (matched by id or name)")

    if reasons:
        return ValidationResult(ok=False, reasons=reasons)

    # All rules passed -- build the normalised record.
    normalised = {
        "merchant_name": display_name(row["merchant_name"]),
        "canonical_category": canonical_category,
        "region": region_val,
        "contact_phone": normalised_phone,
        "contact_email": normalise_email(email_val),
        "registration_date": parsed_date.isoformat(),
        "region_pic_email": ref.region_pic[region_val][1],
        "source_submission_id": row["submission_id"],
        "_dedup_key": normalise_name(row["merchant_name"]),
    }
    return ValidationResult(ok=True, normalised=normalised)


# ---------------------------------------------------------------------------
# Loading & pipeline
# ---------------------------------------------------------------------------

def load_submissions(data_dir: Path) -> List[dict]:
    rows: List[dict] = []
    files = sorted(glob.glob(str(data_dir / "submissions_partner*.csv")))
    if not files:
        raise FileNotFoundError(f"No submissions_partner*.csv files found in {data_dir}")
    for filepath in files:
        source_file = Path(filepath).name
        with open(filepath, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            n = 0
            for row in reader:
                row["source_file"] = source_file
                rows.append(row)
                n += 1
            log.info("Read %d rows from %s", n, source_file)
    return rows


def deduplicate(valid_records: List[dict]) -> List[dict]:
    """Group normalised, valid records by merchant name (trimmed/whitespace-
    collapsed/case-insensitive) and collapse each group to a single row.

    Winner rule: within a duplicate group, keep the record with the most
    recent registration_date (most up-to-date info); ties are broken by the
    lexicographically smallest source_submission_id, for a fully
    deterministic result. See README for rationale.
    """
    groups: Dict[str, List[dict]] = {}
    for rec in valid_records:
        groups.setdefault(rec["_dedup_key"], []).append(rec)

    deduped: List[dict] = []
    for key, group in groups.items():
        winner = sorted(
            group,
            key=lambda r: (r["registration_date"], ""), # newest date wins
        )[-1]
        # break ties deterministically on submission id if dates are equal
        best_date = winner["registration_date"]
        tied = [r for r in group if r["registration_date"] == best_date]
        winner = sorted(tied, key=lambda r: r["source_submission_id"])[0]

        winner = dict(winner)
        winner["duplicates_collapsed"] = len(group)
        del winner["_dedup_key"]
        deduped.append(winner)

    deduped.sort(key=lambda r: r["source_submission_id"])
    return deduped


def write_clean_csv(records: List[dict], path: Path) -> None:
    columns = [
        "merchant_name", "canonical_category", "region", "contact_phone",
        "contact_email", "registration_date", "region_pic_email",
        "source_submission_id", "duplicates_collapsed",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for rec in records:
            writer.writerow({c: rec[c] for c in columns})
    log.info("Wrote %d rows to %s", len(records), path)


def write_errors_csv(errors: List[dict], path: Path) -> None:
    columns = [
        "submission_id", "source_file", "merchant_name", "region",
        "region_pic_email", "rejection_reasons",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for err in errors:
            writer.writerow({c: err[c] for c in columns})
    log.info("Wrote %d rows to %s", len(errors), path)


def run(data_dir: Path, db_path: Path, out_dir: Path) -> None:
    ref = load_reference(db_path)
    rows = load_submissions(data_dir)

    valid_records: List[dict] = []
    error_rows: List[dict] = []

    for row in rows:
        result = validate_row(row, ref)
        if result.ok:
            valid_records.append(result.normalised)
        else:
            region_val = row["region"].strip()
            pic_email = ref.region_pic.get(region_val, (None, "UNKNOWN - invalid region"))[1]
            error_rows.append({
                "submission_id": row["submission_id"],
                "source_file": row["source_file"],
                "merchant_name": row["merchant_name"].strip(),
                "region": region_val,
                "region_pic_email": pic_email,
                "rejection_reasons": "; ".join(result.reasons),
            })

    clean_records = deduplicate(valid_records)

    out_dir.mkdir(parents=True, exist_ok=True)
    write_clean_csv(clean_records, out_dir / "clean.csv")
    write_errors_csv(error_rows, out_dir / "errors.csv")

    log.info(
        "Summary: %d submissions in -> %d clean merchants out, %d rejected, "
        "%d duplicate submissions collapsed",
        len(rows), len(clean_records), len(error_rows),
        len(valid_records) - len(clean_records),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data", type=Path,
                         help="Directory containing submissions_partner*.csv")
    parser.add_argument("--db", default="reference.db", type=Path,
                         help="Path to the reference SQLite database")
    parser.add_argument("--out-dir", default=".", type=Path,
                         help="Directory to write clean.csv and errors.csv into")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(args.data_dir, args.db, args.out_dir)
