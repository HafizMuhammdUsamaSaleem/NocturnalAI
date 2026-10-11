# Three-Way Inventory Reconciliation System

A Streamlit application that reconciles goods-received records from three sources:

1. **Handwritten register photographs** (OCR via Google Gemini Vision)
2. **ERP goods-received records** (CSV)
3. **Storekeeper records** (Excel)

## Engineering Principle

**AI handles perception; deterministic Python handles business truth; humans resolve uncertainty.**

- Gemini Vision extracts structured rows from handwritten registers
- Python deterministically matches and compares records (no LLM judgement for matching)
- Human reviewers resolve OCR uncertainty via Confirm / Correct
- Completeness validation proves loaded source rows are not silently dropped

## Features

- Three-way reconciliation with field-level comparison (quantity/units, supplier, item, lot, date)
- Unit normalization (PCS/pcs, BAG/bags, etc.)
- Generic unit conversion (yards ↔ metres: 1200 yds = 1097.28 m)
- Duplicate detection within all sources; blank-GRN rows preserved
- Missing-record identification per source
- OCR **uncertainty** tracking (`uncertain_fields`) and human review workflow
- Completeness validation: extraction gate + exactly-once row accounting (PASS/FAIL)
- SQLite persistence for review decisions and audit history
- CSV export: results, discrepancies, source-row disposition, audit log

## Installation

```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# Optional: set GEMINI_API_KEY for live OCR
```

## Usage

### Streamlit dashboard

```bash
source venv/bin/activate
streamlit run app.py
```

**Data source modes (sidebar):**

| Mode | Behaviour |
|------|-----------|
| Provided assessment data | Uses `kestrel-data/` files; register OCR from local cache when available |
| Upload files | Processes **only** the files you upload (Register + ERP + Store all required) |

### CLI check on assessment data

```bash
source venv/bin/activate
python app.py test
```

With a usable OCR cache under `.cache/ocr/`, CLI mode reports Register 28 / ERP 28 / Store 30 and overall completeness PASS. Business exceptions (mismatches, missing GRNs, OCR review) still appear in the status summary — completeness PASS does not clear them.

If the OCR cache is missing and no API key is set, extraction FAIL and overall completeness FAIL.

### Unit tests

```bash
source venv/bin/activate
pytest tests/ -v
```

Tests use synthetic fixtures and do not require a live Gemini API key. Assessment-data assertions skip if the OCR cache is unavailable.

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `GEMINI_API_KEY` | Google Gemini API key for live OCR | (empty — use cache / provided mode) |
| `GEMINI_MODEL` | Gemini model name | `gemini-3.8-flash` |

## Data Sources

Assessment files in `kestrel-data/`:

| File | Role |
|------|------|
| `t4_register_page1.jpg` | Handwritten register page 1 |
| `t4_register_page2.jpg` | Handwritten register page 2 |
| `t4_erp_grn.csv` | ERP goods-received records |
| `t4_store_sheet.xlsx` | Storekeeper sheet `Sep-2026` |

## Matching Rules

### Primary match key

- **GRN number** — normalized to digits only (`GRN-4401`, `4401`, `4401.0` → `4401`)

### Field comparisons

1. Quantity — after compatible-unit conversion where configured
2. Unit of measure — synonym map to canonical codes
3. Supplier — exact normalized alias map (no substring guessing)
4. Item — normalized casing/whitespace
5. Lot number — uppercase trim
6. Date — explicit formats; ambiguous M/D vs D/M values are flagged, not guessed

### Unit normalization

| Variants | Canonical |
|----------|-----------|
| PCS, pcs, pieces | PCS |
| BAG, bags | BAG |
| CONE, cones | CONE |
| DRUM, drums | DRUM |
| ROLL, rolls | ROLL |
| CTN, ctns, cartons | CTN |
| BDL, bdl, bundles | BDL |
| M, m, metres, meters | M |
| YD, yds, yards | YD |

### Unit conversions

| From | To | Factor |
|------|-----|--------|
| YD (yards) | M (metres) | × 0.9144 |

Example: 1200 yards = 1097.28 metres.

Incompatible units (e.g. PCS vs M) are never treated as equal merely because the numeric values match.

### Status precedence

1. `uncertain_ocr_review` — unclear/null OCR fields; not treated as verified matches
2. `duplicate_or_ambiguous` — duplicate GRNs within a source, or blank GRN
3. `missing_from_erp` / `missing_from_store` / `missing_from_register` / `missing_from_multiple`
4. `mismatched` — linked records with field-level differences
5. `matched` — compared fields agree under normalization rules

## Completeness Validation

Completeness has two independent gates:

1. **Extraction** — register pages obtained successfully (API, usable cache, or manual register file)
2. **Exactly-once row accounting** — every `source_row_id` appears in results exactly once

**Overall PASS** requires both. It does **not** mean all business discrepancies are resolved. Mismatches, missing GRNs, duplicates, and OCR review items remain listed as exceptions.

## Human Review Workflow

For records flagged `uncertain_ocr_review`:

1. Open the Review tab and inspect the register image with extracted values next to ERP/Store
2. **Confirm** the extracted value, or **Correct** it (a reason is required for Correct; numeric qty is validated)
3. The decision is saved to SQLite with audit history
4. Normalization, reconciliation, and completeness **re-run automatically**

Unresolved OCR items are never marked as verified matches.

## OCR and Offline Mode

- Successful OCR responses are cached under `.cache/ocr/` (by image content hash; legacy filename cache also supported)
- **Provided mode** without an API key uses the local cache when present
- If the cache is missing or unusable and no API key is available, **extraction FAIL** and **overall completeness FAIL** — values are not invented
- Upload mode with register images uses live OCR (`use_cache=False`); API key required for that path

## Known Discrepancies in Assessment Data

| GRN | Issue | Details |
|-----|-------|---------|
| 4409 | Duplicate in Store | Identical row appears twice |
| 4412 | Lot mismatch | ERP: L2609-021; Register/Store: L2609-012 |
| 4415 | Date mismatch | ERP: 2026-09-13; Register/Store: 2026-09-12 |
| 4417 | Qty mismatch | ERP: 46; Register/Store: 48 |
| 4419 | Qty mismatch | Store: 60; Register/ERP: 66 (register shows correction) |
| 4421 | Missing from ERP | Present in Register and Store only |
| 4425 | Unit conflict | Register: 1200 yds; ERP: 1097.28 M (equivalent); Store: 1200 m |
| 4428 | OCR unreadable | Qty obscured by ink blot — human review |
| 4430 | Missing from Register/Store | Present in ERP only |
| (blank) | Store return | −5000 pcs on lot L2609-002; no GRN |

## Error Handling

- Missing input files — clear errors
- Invalid CSV/Excel — logged; invalid fields counted in completeness
- OCR/API failure — recorded in extraction state; completeness FAIL if pages incomplete
- Missing/unusable OCR cache offline — extraction FAIL (no silent fabrication)
- Malformed model output — Pydantic validation; invalid rows skipped with warnings
- Duplicate identifiers — flagged; every source row preserved
- Unrecognized units — uppercased; not converted
- Incomplete reconciliation — completeness FAIL

## Privacy and Security

- API keys are read from environment variables / `.env` only (not committed)
- Keys are not shown in the results UI or written into exports
- **Reconciliation, matching, and completeness run locally**
- **When Gemini OCR is used, register images are sent to Google’s Gemini API**
- Offline/provided mode with cache keeps image bytes local for that run

## Limitations

- OCR quality depends on handwriting and image clarity
- No calibrated OCR confidence score is fabricated; uncertainty is field-level (`uncertain_fields`)
- Unit conversions are limited to documented rules (yards ↔ metres)
- Supplier matching uses a fixed exact-alias map
- Store returns are not automatically netted against receipts
- Ambiguous dates are flagged rather than guessed

## Project Structure

```
NocturnalAI/
├── app.py                      # Loaders, OCR, reconciliation, Streamlit UI
├── requirements.txt
├── .env.example
├── README.md
├── tests/
│   └── test_reconciliation.py
├── .cache/ocr/                 # Local OCR cache (gitignored)
├── data/reviews.db             # Review/audit SQLite (runtime)
├── exports/                    # Generated CSVs (runtime)
└── kestrel-data/               # Assessment inputs
    ├── t4_register_page1.jpg
    ├── t4_register_page2.jpg
    ├── t4_erp_grn.csv
    └── t4_store_sheet.xlsx
```

## License

Assessment project for demonstration purposes.
