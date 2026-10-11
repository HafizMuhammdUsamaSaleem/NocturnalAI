"""
Three-Way Inventory Reconciliation System
==========================================
Reconciles goods-received records from:
1. Handwritten register photographs (OCR via Gemini)
2. ERP goods-received records (CSV)
3. Storekeeper records (Excel)

Engineering principle: AI handles perception; deterministic code handles
business truth; humans resolve uncertainty.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).parent / ".env")

# =============================================================================
# CONFIGURATION
# =============================================================================

DATA_DIR = Path(__file__).parent / "kestrel-data"
CACHE_DIR = Path(__file__).parent / ".cache" / "ocr"
DB_PATH = Path(__file__).parent / "data" / "reviews.db"
EXPORT_DIR = Path(__file__).parent / "exports"

# Assessment scenario year for register DD/MM dates without year
REGISTER_DATE_YEAR = 2026


def _get_api_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if key:
        return key
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY=") and not line.strip().startswith("#"):
                return line.split("=", 1)[1].strip()
    return ""


GEMINI_API_KEY = _get_api_key()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
_UNSET = object()

# =============================================================================
# UNIT NORMALIZATION AND CONVERSION (config-driven)
# =============================================================================

UOM_SYNONYMS = {
    "PCS": "PCS", "pcs": "PCS", "PC": "PCS", "pc": "PCS",
    "PIECE": "PCS", "piece": "PCS", "PIECES": "PCS", "pieces": "PCS",
    "BAG": "BAG", "bag": "BAG", "BAGS": "BAG", "bags": "BAG",
    "CONE": "CONE", "cone": "CONE", "CONES": "CONE", "cones": "CONE",
    "DRUM": "DRUM", "drum": "DRUM", "DRUMS": "DRUM", "drums": "DRUM",
    "ROLL": "ROLL", "roll": "ROLL", "ROLLS": "ROLL", "rolls": "ROLL",
    "CTN": "CTN", "ctn": "CTN", "CTNS": "CTN", "ctns": "CTN",
    "CARTON": "CTN", "carton": "CTN", "CARTONS": "CTN", "cartons": "CTN",
    "BDL": "BDL", "bdl": "BDL", "BUNDLE": "BDL", "bundle": "BDL",
    "BUNDLES": "BDL", "bundles": "BDL",
    "M": "M", "m": "M", "MTR": "M", "mtr": "M",
    "METRE": "M", "metre": "M", "METRES": "M", "metres": "M",
    "METER": "M", "meter": "M", "METERS": "M", "meters": "M",
    "YD": "YD", "yd": "YD", "YDS": "YD", "yds": "YD",
    "YARD": "YD", "yard": "YD", "YARDS": "YD", "yards": "YD",
}

# (from_canonical, to_canonical) -> multiply factor
UNIT_CONVERSIONS = {
    ("YD", "M"): 0.9144,
    ("M", "YD"): 1 / 0.9144,
}

# family frozenset -> base unit for comparison
CONVERTIBLE_UNIT_FAMILIES = {
    frozenset({"M", "YD"}): "M",
}


def normalize_uom(raw_uom: Optional[str]) -> Optional[str]:
    if raw_uom is None or str(raw_uom).strip() == "":
        return None
    cleaned = str(raw_uom).strip()
    if cleaned in UOM_SYNONYMS:
        return UOM_SYNONYMS[cleaned]
    return cleaned.upper()


def get_base_unit(unit: Optional[str]) -> Optional[str]:
    canonical = normalize_uom(unit)
    if not canonical:
        return None
    for family, base in CONVERTIBLE_UNIT_FAMILIES.items():
        if canonical in family:
            return base
    return canonical


def convert_qty_to_base(qty: float, from_unit: Optional[str]) -> tuple[float, Optional[str]]:
    """Convert quantity to family base unit using UNIT_CONVERSIONS only."""
    canonical = normalize_uom(from_unit)
    if not canonical:
        return qty, None
    base = get_base_unit(canonical)
    if canonical == base:
        return qty, base
    factor = UNIT_CONVERSIONS.get((canonical, base))
    if factor is None:
        return qty, canonical
    return qty * factor, base


def units_compatible(unit1: Optional[str], unit2: Optional[str]) -> bool:
    norm1 = normalize_uom(unit1)
    norm2 = normalize_uom(unit2)
    if not norm1 or not norm2:
        return False
    if norm1 == norm2:
        return True
    for family in CONVERTIBLE_UNIT_FAMILIES:
        if norm1 in family and norm2 in family:
            return True
    return False


# =============================================================================
# GRN / SUPPLIER / ITEM / DATE NORMALIZATION
# =============================================================================

def normalize_grn(raw: Any) -> Optional[str]:
    """Canonical GRN key: digits only (GRN-4401 / 4401 / 4401.0 -> '4401')."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return None
    s = re.sub(r"(?i)^GRN[-\s]*", "", s)
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    digits = re.sub(r"\D", "", s)
    return digits if digits else None


# Exact normalized-key -> canonical supplier name (no substring matching)
SUPPLIER_ALIASES = {
    "QASR PKG": "QASR PACKAGING (PVT) LTD",
    "QASR PACKAGING": "QASR PACKAGING (PVT) LTD",
    "QASR PACKAGING PVT LTD": "QASR PACKAGING (PVT) LTD",
    "TARAZ POLY": "TARAZ POLY INDUSTRIES",
    "TARAZ POLY INDUSTRIES": "TARAZ POLY INDUSTRIES",
    "ZARNAB CHEM": "ZARNAB CHEMICAL INDUSTRIES (PVT) LTD",
    "ZARNAB CHEMICALS": "ZARNAB CHEMICAL INDUSTRIES (PVT) LTD",
    "ZARNAB CHEMICAL INDUSTRIES PVT LTD": "ZARNAB CHEMICAL INDUSTRIES (PVT) LTD",
    "REHBAR THREAD": "REHBAR THREADS",
    "REHBAR THREADS": "REHBAR THREADS",
    "SAAHIL PALLETS": "SAAHIL PALLETS & TIMBER",
    "SAAHIL PALLETS TIMBER": "SAAHIL PALLETS & TIMBER",
    "NAYAB LABELS": "NAYAB LABELS (PVT) LTD",
    "NAYAB LABELS PVT LTD": "NAYAB LABELS (PVT) LTD",
    "SHAHTOOT": "SHAHTOOT WEAVING MILLS",
    "SHAHTOOT WEAVING": "SHAHTOOT WEAVING MILLS",
    "SHAHTOOT WEAVING MILLS": "SHAHTOOT WEAVING MILLS",
}


def supplier_lookup_key(raw: str) -> str:
    """Collapse punctuation/whitespace for exact alias lookup."""
    s = str(raw or "").upper()
    s = s.replace("&", " ")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_supplier(raw: Optional[str]) -> str:
    """Exact alias map only — no substring matching."""
    if not raw:
        return ""
    key = supplier_lookup_key(raw)
    if key in SUPPLIER_ALIASES:
        return SUPPLIER_ALIASES[key]
    return key


def normalize_item(raw: Optional[str]) -> str:
    if not raw:
        return ""
    s = str(raw).upper().strip()
    s = s.replace('""', '"').replace("''", "'")
    s = re.sub(r"\s+", " ", s)
    return s


@dataclass
class DateParseResult:
    iso: Optional[str]
    ambiguous: bool = False
    note: Optional[str] = None


def parse_date(
    val: Any,
    *,
    preferred_formats: Optional[list[str]] = None,
    default_year: Optional[int] = None,
) -> DateParseResult:
    """
    Parse dates with explicit formats.
    Ambiguous values (different results from competing formats) are flagged.
    """
    if val is None:
        return DateParseResult(None)
    if isinstance(val, datetime):
        return DateParseResult(val.strftime("%Y-%m-%d"))
    # pandas Timestamp
    if hasattr(val, "strftime") and hasattr(val, "year"):
        try:
            return DateParseResult(val.strftime("%Y-%m-%d"))
        except Exception:
            pass

    s = str(val).strip()
    if not s or s.lower() in {"nan", "nat", "none"}:
        return DateParseResult(None)

    # Strip time portion if present
    if " " in s and re.match(r"\d{4}-\d{2}-\d{2}", s):
        s = s.split(" ")[0]

    # Register-style DD/MM without year
    if default_year is not None and re.fullmatch(r"\d{1,2}/\d{1,2}", s):
        try:
            day, month = s.split("/")
            dt = datetime(default_year, int(month), int(day))
            return DateParseResult(dt.strftime("%Y-%m-%d"))
        except ValueError as e:
            return DateParseResult(None, note=f"Invalid DD/MM date: {s} ({e})")

    formats = preferred_formats or ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"]
    parsed: list[tuple[str, str]] = []
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            parsed.append((fmt, dt.strftime("%Y-%m-%d")))
        except ValueError:
            continue

    if not parsed:
        return DateParseResult(None, note=f"Unrecognized date: {s}")

    unique_isos = {iso for _, iso in parsed}
    if len(unique_isos) == 1:
        return DateParseResult(parsed[0][1])

    # Ambiguous: competing formats produce different calendar dates
    return DateParseResult(
        None,
        ambiguous=True,
        note=f"Ambiguous date '{s}' -> {sorted(unique_isos)}",
    )


def parse_store_date(val: Any) -> Optional[str]:
    """Backward-compatible helper — returns ISO or None."""
    return parse_date(val, preferred_formats=["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"]).iso


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class SourceRecord:
    source_row_id: str
    source_type: str

    original_grn: Optional[str] = None
    original_date: Optional[str] = None
    original_supplier: Optional[str] = None
    original_item: Optional[str] = None
    original_lot: Optional[str] = None
    original_qty: Optional[Any] = None
    original_uom: Optional[str] = None
    original_remarks: Optional[str] = None

    grn_key: Optional[str] = None
    norm_date: Optional[str] = None
    norm_supplier: Optional[str] = None
    norm_item: Optional[str] = None
    norm_lot: Optional[str] = None
    norm_qty: Optional[float] = None
    norm_uom: Optional[str] = None
    base_qty: Optional[float] = None
    base_uom: Optional[str] = None

    uncertain_fields: list = field(default_factory=list)
    date_ambiguous: bool = False
    invalid_fields: list = field(default_factory=list)
    page_number: Optional[int] = None
    row_index: Optional[int] = None
    extra_data: dict = field(default_factory=dict)


class RegisterRecordSchema(BaseModel):
    date: Optional[str] = None
    grn_no: Optional[int] = None
    supplier: Optional[str] = None
    item: Optional[str] = None
    lot_no: Optional[str] = None
    qty: Optional[float] = None
    unit: Optional[str] = None
    received_by: Optional[str] = None
    uncertain_fields: list[str] = Field(default_factory=list)
    notes: Optional[str] = None


class RegisterExtractionResult(BaseModel):
    page_number: int
    source_image: str
    records: list[RegisterRecordSchema]
    extraction_notes: Optional[str] = None
    confidence: str = "unavailable"
    content_hash: Optional[str] = None


@dataclass
class ExtractionState:
    """Track OCR/register extraction independently of row accounting."""
    expected_pages: int = 0
    pages_ok: list[str] = field(default_factory=list)
    pages_failed: list[str] = field(default_factory=list)
    pages_missing_cache: list[str] = field(default_factory=list)
    used_api: bool = False
    used_cache: bool = False
    used_manual: bool = False
    ok: bool = False
    messages: list[str] = field(default_factory=list)
    record_count: int = 0

    def summarize(self) -> str:
        if self.used_manual:
            return f"Manual register load OK ({self.record_count} rows)"
        if self.ok:
            src = []
            if self.used_cache:
                src.append("cache")
            if self.used_api:
                src.append("API")
            return (
                f"Extraction OK — {len(self.pages_ok)}/{self.expected_pages} pages "
                f"via {', '.join(src) or 'unknown'} ({self.record_count} rows)"
            )
        parts = []
        if self.pages_failed:
            parts.append(f"failed: {self.pages_failed}")
        if self.pages_missing_cache:
            parts.append(f"missing cache/API: {self.pages_missing_cache}")
        return "Extraction FAIL — " + ("; ".join(parts) or "no register data")


@dataclass
class ReconciliationResult:
    result_id: str
    grn_key: Optional[str]
    status: str

    register_record: Optional[SourceRecord] = None
    erp_record: Optional[SourceRecord] = None
    store_record: Optional[SourceRecord] = None
    # Preserve extra duplicate rows not shown in primary slots
    extra_records: list[SourceRecord] = field(default_factory=list)

    mismatch_fields: list[str] = field(default_factory=list)
    mismatch_details: dict = field(default_factory=dict)
    explanation: str = ""
    source_row_ids: list[str] = field(default_factory=list)


@dataclass
class CompletenessReport:
    source: str
    original_count: int
    accounted_count: int
    unresolved_count: int
    duplicate_count: int
    invalid_count: int
    unexpected_count: int
    passed: bool
    details: list[str] = field(default_factory=list)
    unresolved_rows: list[dict] = field(default_factory=list)
    unexpected_ids: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class CompletenessBundle:
    """Separates extraction, row accounting, and business discrepancies."""
    extraction: ExtractionState
    source_reports: list[CompletenessReport]
    row_accounting_pass: bool
    extraction_pass: bool
    overall_pass: bool
    business_exception_count: int
    pending_review_count: int
    messages: list[str] = field(default_factory=list)


def describe_source_row(rec: SourceRecord) -> dict:
    page = f"page {rec.page_number}" if rec.page_number else None
    location_parts = []
    if page:
        location_parts.append(page)
    if rec.row_index is not None:
        location_parts.append(f"row {rec.row_index + 1}")
    location = ", ".join(location_parts) if location_parts else rec.source_row_id

    grn = rec.grn_key or "(no GRN)"
    qty = f"{rec.original_qty} {rec.original_uom}".strip() if (rec.original_qty not in (None, "") or rec.original_uom) else "(no qty)"
    item = rec.original_item or "(no item)"

    why = []
    if not rec.grn_key:
        why.append("No GRN number")
    if rec.invalid_fields:
        why.append(f"Invalid fields: {rec.invalid_fields}")
    if rec.date_ambiguous:
        why.append("Ambiguous date")
    if rec.uncertain_fields:
        why.append(f"Uncertain: {rec.uncertain_fields}")
    if rec.original_remarks:
        why.append(f"Remarks: {rec.original_remarks}")

    return {
        "id": rec.source_row_id,
        "location": location,
        "grn": grn,
        "item": item,
        "qty": qty,
        "lot": rec.original_lot or "(no lot)",
        "why": "; ".join(why) if why else "Not included in any reconciliation result",
        "label": f"{location}: GRN {grn} | {item} | {qty}",
    }


STATUS_LABELS = {
    "matched": "Matched — compared fields agree",
    "mismatched": "Mismatch — values differ",
    "missing_from_erp": "Missing in ERP",
    "missing_from_store": "Missing in Store",
    "missing_from_register": "Missing in Register",
    "missing_from_multiple": "Missing in multiple sources",
    "duplicate_or_ambiguous": "Duplicate / needs investigation",
    "uncertain_ocr_review": "OCR unclear — needs human review",
    "unclassified": "Unclassified — validation failure",
}


# =============================================================================
# DATA LOADERS
# =============================================================================

def _finalize_qty_uom(qty: Optional[float], raw_uom: str) -> tuple[Optional[float], Optional[str], Optional[float], Optional[str]]:
    norm_uom = normalize_uom(raw_uom)
    if qty is not None and norm_uom:
        base_qty, base_uom = convert_qty_to_base(qty, norm_uom)
        return qty, norm_uom, base_qty, base_uom
    return qty, norm_uom, qty, norm_uom


def load_erp_data(filepath: Path = None) -> list[SourceRecord]:
    filepath = filepath or (DATA_DIR / "t4_erp_grn.csv")
    if not filepath.exists():
        raise FileNotFoundError(f"ERP file not found: {filepath}")

    records = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            raw_qty = row.get("qty", "")
            invalid = []
            try:
                qty = float(raw_qty) if raw_qty not in (None, "") else None
            except ValueError:
                qty = None
                invalid.append("qty")

            raw_uom = row.get("uom", "")
            qty, norm_uom, base_qty, base_uom = _finalize_qty_uom(qty, raw_uom)

            date_res = parse_date(row.get("grn_date", ""), preferred_formats=["%Y-%m-%d"])
            if date_res.ambiguous:
                invalid.append("date")

            records.append(SourceRecord(
                source_row_id=f"erp:{i}",
                source_type="erp",
                original_grn=row.get("grn_no", ""),
                original_date=row.get("grn_date", ""),
                original_supplier=row.get("supplier_name", ""),
                original_item=row.get("item_desc", ""),
                original_lot=row.get("lot_no", ""),
                original_qty=raw_qty,
                original_uom=raw_uom,
                grn_key=normalize_grn(row.get("grn_no", "")),
                norm_date=date_res.iso,
                norm_supplier=normalize_supplier(row.get("supplier_name", "")),
                norm_item=normalize_item(row.get("item_desc", "")),
                norm_lot=str(row.get("lot_no", "")).strip().upper(),
                norm_qty=qty,
                norm_uom=norm_uom,
                base_qty=base_qty,
                base_uom=base_uom,
                date_ambiguous=date_res.ambiguous,
                invalid_fields=invalid,
                row_index=i,
                extra_data={
                    "supplier_code": row.get("supplier_code", ""),
                    "entered_by": row.get("entered_by", ""),
                    "entered_at": row.get("entered_at", ""),
                },
            ))
    logger.info("Loaded %s ERP records from %s", len(records), filepath)
    return records


def load_store_data(filepath: Path = None) -> list[SourceRecord]:
    import pandas as pd

    filepath = filepath or (DATA_DIR / "t4_store_sheet.xlsx")
    if not filepath.exists():
        raise FileNotFoundError(f"Store file not found: {filepath}")

    df = pd.read_excel(filepath, sheet_name="Sep-2026")
    records = []
    for i, row in df.iterrows():
        invalid = []
        raw_grn = row.get("GRN")
        if pd.isna(raw_grn):
            raw_grn_str = ""
            grn_key = None
        else:
            raw_grn_str = str(raw_grn)
            grn_key = normalize_grn(raw_grn)

        raw_qty = row.get("Qty Received")
        if pd.isna(raw_qty):
            qty = None
            raw_qty_str = ""
        else:
            try:
                qty = float(raw_qty)
                raw_qty_str = str(raw_qty)
            except (ValueError, TypeError):
                qty = None
                raw_qty_str = str(raw_qty)
                invalid.append("qty")

        raw_date = row.get("Date")
        raw_date_str = str(raw_date) if not pd.isna(raw_date) else ""
        # Prefer ISO then US M/D (observed store string), then D/M
        date_res = parse_date(
            None if pd.isna(raw_date) else raw_date,
            preferred_formats=["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"],
        )
        if date_res.ambiguous:
            invalid.append("date")

        raw_uom = "" if pd.isna(row.get("UOM", "")) else str(row.get("UOM", ""))
        qty, norm_uom, base_qty, base_uom = _finalize_qty_uom(qty, raw_uom)

        supplier = "" if pd.isna(row.get("Supplier", "")) else str(row.get("Supplier", ""))
        material = "" if pd.isna(row.get("Material", "")) else str(row.get("Material", ""))
        lot = "" if pd.isna(row.get("Lot", "")) else str(row.get("Lot", ""))
        remarks = "" if pd.isna(row.get("Remarks", "")) else str(row.get("Remarks", ""))

        records.append(SourceRecord(
            source_row_id=f"store:{i}",
            source_type="store",
            original_grn=raw_grn_str,
            original_date=raw_date_str,
            original_supplier=supplier,
            original_item=material,
            original_lot=lot,
            original_qty=raw_qty_str,
            original_uom=raw_uom,
            original_remarks=remarks,
            grn_key=grn_key,
            norm_date=date_res.iso,
            norm_supplier=normalize_supplier(supplier),
            norm_item=normalize_item(material),
            norm_lot=lot.strip().upper(),
            norm_qty=qty,
            norm_uom=norm_uom,
            base_qty=base_qty,
            base_uom=base_uom,
            date_ambiguous=date_res.ambiguous,
            invalid_fields=invalid,
            row_index=int(i) if not isinstance(i, int) else i,
        ))
    logger.info("Loaded %s Store records from %s", len(records), filepath)
    return records


def load_register_images() -> list[dict]:
    images = []
    for page_num, filename in enumerate(
        ["t4_register_page1.jpg", "t4_register_page2.jpg"], start=1
    ):
        filepath = DATA_DIR / filename
        if filepath.exists():
            images.append({
                "path": filepath,
                "filename": filename,
                "page_number": page_num,
            })
        else:
            logger.warning("Register image not found: %s", filepath)
    return images


# =============================================================================
# GEMINI OCR (google-genai SDK) + content-hash cache
# =============================================================================

def image_content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def get_ocr_cache_path(content_hash: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{content_hash}.json"


def legacy_cache_path(filename: str) -> Path:
    return CACHE_DIR / f"{filename}.json"


def load_cached_ocr(path: Path, filename: str) -> Optional[RegisterExtractionResult]:
    h = image_content_hash(path)
    for cache_path in (get_ocr_cache_path(h), legacy_cache_path(filename)):
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text())
                result = RegisterExtractionResult(**data)
                result.content_hash = h
                return result
            except Exception as e:
                logger.warning("Failed to load OCR cache %s: %s", cache_path, e)
    return None


def save_ocr_cache(path: Path, filename: str, result: RegisterExtractionResult):
    h = image_content_hash(path)
    result.content_hash = h
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    get_ocr_cache_path(h).write_text(json.dumps(result.model_dump(), indent=2))
    # Keep legacy filename cache for interview demos
    legacy_cache_path(filename).write_text(json.dumps(result.model_dump(), indent=2))


def extract_register_with_gemini(
    image_path: Path,
    page_number: int,
    api_key: str,
    model: str = None,
) -> RegisterExtractionResult:
    """Extract register records using the google-genai SDK."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise ImportError(
            "google-genai is required: pip install google-genai"
        ) from e

    model = model or GEMINI_MODEL
    client = genai.Client(api_key=api_key)
    image_bytes = image_path.read_bytes()
    mime = "image/jpeg"
    if image_path.suffix.lower() == ".png":
        mime = "image/png"

    prompt = """Analyze this handwritten goods receiving register page and extract all filled data rows.

For each row extract:
- date (DD/MM), grn_no (integer), supplier, item, lot_no, qty (number or null), unit, received_by
- uncertain_fields: list of field names that are unclear, corrected/struck-through, or unreadable
- notes: describe corrections/smudges

Rules:
- Use the CORRECTED value when text is struck through and rewritten; list the field in uncertain_fields
- If a value is unreadable, set it to null and list the field in uncertain_fields
- Do NOT guess quantities from other systems
- Return ONLY valid JSON:
{"records":[...], "extraction_notes":"..."}
"""

    try:
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=image_bytes, mime_type=mime),
                    ],
                )
            ],
        )
        response_text = (response.text or "").strip()
        if response_text.startswith("```"):
            response_text = re.sub(r"^```json?\s*", "", response_text)
            response_text = re.sub(r"\s*```$", "", response_text)

        data = json.loads(response_text)
        validated = []
        for rec in data.get("records", []):
            try:
                validated.append(RegisterRecordSchema(**rec))
            except ValidationError as ve:
                logger.warning("Invalid OCR record: %s", ve)

        return RegisterExtractionResult(
            page_number=page_number,
            source_image=image_path.name,
            records=validated,
            extraction_notes=data.get("extraction_notes"),
            confidence="unavailable",
            content_hash=image_content_hash(image_path),
        )
    except Exception as e:
        logger.error("Gemini extraction failed for %s: %s", image_path, e)
        raise


def _ocr_record_to_source(rec: RegisterRecordSchema, page_num: int, idx: int) -> SourceRecord:
    date_res = parse_date(rec.date, default_year=REGISTER_DATE_YEAR)
    uncertain = list(rec.uncertain_fields or [])
    invalid = []
    if date_res.ambiguous:
        uncertain.append("date")
        invalid.append("date")
    elif rec.date and not date_res.iso:
        invalid.append("date")

    qty = rec.qty
    qty, norm_uom, base_qty, base_uom = _finalize_qty_uom(qty, rec.unit or "")

    return SourceRecord(
        source_row_id=f"register:p{page_num}:r{idx}",
        source_type="register",
        original_grn=str(rec.grn_no) if rec.grn_no is not None else "",
        original_date=rec.date or "",
        original_supplier=rec.supplier or "",
        original_item=rec.item or "",
        original_lot=rec.lot_no or "",
        original_qty=str(rec.qty) if rec.qty is not None else "",
        original_uom=rec.unit or "",
        grn_key=normalize_grn(rec.grn_no),
        norm_date=date_res.iso,
        norm_supplier=normalize_supplier(rec.supplier or ""),
        norm_item=normalize_item(rec.item or ""),
        norm_lot=(rec.lot_no or "").strip().upper(),
        norm_qty=qty,
        norm_uom=norm_uom,
        base_qty=base_qty,
        base_uom=base_uom,
        uncertain_fields=sorted(set(uncertain)),
        date_ambiguous=date_res.ambiguous,
        invalid_fields=invalid,
        page_number=page_num,
        row_index=idx,
        extra_data={"received_by": rec.received_by, "notes": rec.notes},
    )


def extract_register_records(
    images: list[dict],
    api_key=_UNSET,
    model: str = None,
    use_cache: bool = True,
) -> tuple[list[SourceRecord], ExtractionState]:
    """
    Extract register rows from images.
    Returns (records, extraction_state). Extraction failure is never silent:
    state.ok is False if any expected page fails.

    Pass api_key=None to disable API (cache/manual only). Omit api_key to use GEMINI_API_KEY.
    """
    if api_key is _UNSET:
        api_key = GEMINI_API_KEY
    model = model or GEMINI_MODEL

    state = ExtractionState(expected_pages=len(images))
    all_records: list[SourceRecord] = []

    if not images:
        state.messages.append("No register images provided")
        state.ok = False
        return all_records, state

    for img_info in images:
        filename = img_info["filename"]
        page_num = img_info["page_number"]
        path = Path(img_info["path"])
        extraction = None

        if use_cache and path.exists():
            extraction = load_cached_ocr(path, filename)
            if extraction:
                state.used_cache = True
                logger.info("Using cached OCR for %s", filename)

        if extraction is None:
            if not api_key:
                state.pages_missing_cache.append(filename)
                state.messages.append(
                    f"{filename}: no API key and no usable OCR cache"
                )
                continue
            try:
                extraction = extract_register_with_gemini(path, page_num, api_key, model)
                save_ocr_cache(path, filename, extraction)
                state.used_api = True
            except Exception as e:
                state.pages_failed.append(filename)
                state.messages.append(f"{filename}: OCR/API error: {e}")
                continue

        state.pages_ok.append(filename)
        for idx, rec in enumerate(extraction.records):
            all_records.append(_ocr_record_to_source(rec, page_num, idx))

    state.record_count = len(all_records)
    state.ok = (
        len(state.pages_ok) == state.expected_pages
        and len(state.pages_failed) == 0
        and len(state.pages_missing_cache) == 0
        and state.expected_pages > 0
    )
    if not state.ok and not state.messages:
        state.messages.append("Incomplete register extraction")
    logger.info(
        "Extracted %s register records; extraction_ok=%s",
        len(all_records),
        state.ok,
    )
    return all_records, state


def load_manual_register_data(filepath: Path) -> tuple[list[SourceRecord], ExtractionState]:
    state = ExtractionState(expected_pages=0, used_manual=True, ok=False)
    if not filepath.exists():
        state.messages.append(f"Manual register file missing: {filepath}")
        return [], state

    records = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            page = int(row.get("page", 1) or 1)
            raw_qty = row.get("qty", "")
            invalid = []
            try:
                qty = float(raw_qty) if raw_qty not in (None, "") else None
            except ValueError:
                qty = None
                invalid.append("qty")

            qty, norm_uom, base_qty, base_uom = _finalize_qty_uom(qty, row.get("unit", ""))
            date_res = parse_date(
                row.get("date", ""),
                preferred_formats=["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"],
                default_year=REGISTER_DATE_YEAR,
            )
            uncertain = [x.strip() for x in (row.get("uncertain_fields") or "").split(",") if x.strip()]
            if date_res.ambiguous:
                uncertain.append("date")
                invalid.append("date")

            records.append(SourceRecord(
                source_row_id=f"register:p{page}:r{i}",
                source_type="register",
                original_grn=row.get("grn_no", ""),
                original_date=row.get("date", ""),
                original_supplier=row.get("supplier", ""),
                original_item=row.get("item", ""),
                original_lot=row.get("lot_no", ""),
                original_qty=raw_qty,
                original_uom=row.get("unit", ""),
                grn_key=normalize_grn(row.get("grn_no", "")),
                norm_date=date_res.iso,
                norm_supplier=normalize_supplier(row.get("supplier", "")),
                norm_item=normalize_item(row.get("item", "")),
                norm_lot=str(row.get("lot_no", "")).strip().upper(),
                norm_qty=qty,
                norm_uom=norm_uom,
                base_qty=base_qty,
                base_uom=base_uom,
                uncertain_fields=sorted(set(uncertain)),
                date_ambiguous=date_res.ambiguous,
                invalid_fields=invalid,
                page_number=page,
                row_index=i,
            ))
    state.record_count = len(records)
    state.ok = len(records) > 0
    state.messages.append(f"Loaded {len(records)} manual register rows from CSV")
    return records, state


def load_manual_register_excel(filepath: Path) -> tuple[list[SourceRecord], ExtractionState]:
    import pandas as pd

    state = ExtractionState(expected_pages=0, used_manual=True, ok=False)
    if not filepath.exists():
        state.messages.append(f"Manual register Excel missing: {filepath}")
        return [], state

    df = pd.read_excel(filepath)
    df.columns = [str(c).lower().strip() for c in df.columns]
    records = []

    for i, row in df.iterrows():
        page = 1
        for col in ("page", "page_number", "pg"):
            if col in df.columns and pd.notna(row.get(col)):
                try:
                    page = int(row[col])
                except Exception:
                    pass
                break

        grn_val = None
        for col in ("grn_no", "grn", "grn no", "grn_number"):
            if col in df.columns and pd.notna(row.get(col)):
                grn_val = row[col]
                break

        qty = None
        raw_qty = ""
        invalid = []
        for col in ("qty", "quantity", "qty received"):
            if col in df.columns and pd.notna(row.get(col)):
                raw_qty = str(row[col])
                try:
                    qty = float(row[col])
                except Exception:
                    invalid.append("qty")
                break

        raw_uom = ""
        for col in ("unit", "uom", "units"):
            if col in df.columns and pd.notna(row.get(col)):
                raw_uom = str(row[col])
                break

        qty, norm_uom, base_qty, base_uom = _finalize_qty_uom(qty, raw_uom)

        date_val = ""
        for col in ("date", "grn_date"):
            if col in df.columns and pd.notna(row.get(col)):
                date_val = row[col]
                break
        date_res = parse_date(
            date_val if date_val != "" else None,
            preferred_formats=["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"],
            default_year=REGISTER_DATE_YEAR,
        )
        uncertain = []
        if date_res.ambiguous:
            uncertain.append("date")
            invalid.append("date")

        supplier_val = ""
        for col in ("supplier", "supplier_name"):
            if col in df.columns and pd.notna(row.get(col)):
                supplier_val = str(row[col])
                break
        item_val = ""
        for col in ("item", "item_desc", "description", "material"):
            if col in df.columns and pd.notna(row.get(col)):
                item_val = str(row[col])
                break
        lot_val = ""
        for col in ("lot_no", "lot", "lot_number"):
            if col in df.columns and pd.notna(row.get(col)):
                lot_val = str(row[col])
                break

        records.append(SourceRecord(
            source_row_id=f"register:p{page}:r{i}",
            source_type="register",
            original_grn=str(grn_val) if grn_val is not None else "",
            original_date=str(date_val) if date_val != "" else "",
            original_supplier=supplier_val,
            original_item=item_val,
            original_lot=lot_val,
            original_qty=raw_qty,
            original_uom=raw_uom,
            grn_key=normalize_grn(grn_val),
            norm_date=date_res.iso,
            norm_supplier=normalize_supplier(supplier_val),
            norm_item=normalize_item(item_val),
            norm_lot=lot_val.strip().upper(),
            norm_qty=qty,
            norm_uom=norm_uom,
            base_qty=base_qty,
            base_uom=base_uom,
            uncertain_fields=uncertain,
            date_ambiguous=date_res.ambiguous,
            invalid_fields=invalid,
            page_number=page,
            row_index=int(i) if not isinstance(i, int) else i,
        ))

    state.record_count = len(records)
    state.ok = len(records) > 0
    state.messages.append(f"Loaded {len(records)} manual register rows from Excel")
    return records, state


# =============================================================================
# RECONCILIATION ENGINE
# =============================================================================

def compare_quantities(
    qty1: Optional[float],
    uom1: Optional[str],
    qty2: Optional[float],
    uom2: Optional[str],
    tolerance: float = 0.01,
) -> tuple[bool, str]:
    if qty1 is None or qty2 is None:
        return False, "One or both quantities are null"
    if not units_compatible(uom1, uom2):
        return False, f"Incompatible units: {uom1} vs {uom2}"
    base_qty1, _ = convert_qty_to_base(qty1, uom1)
    base_qty2, _ = convert_qty_to_base(qty2, uom2)
    if abs(base_qty1 - base_qty2) <= tolerance:
        if normalize_uom(uom1) != normalize_uom(uom2):
            return True, f"equal after conversion: {qty1} {uom1} = {qty2} {uom2}"
        return True, "Quantities match"
    return False, f"Quantity difference: {base_qty1 - base_qty2:+.2f} (base unit)"


def _compare_text_field(
    name: str,
    a: Optional[SourceRecord],
    b: Optional[SourceRecord],
    attr: str,
    label_a: str,
    label_b: str,
) -> tuple[Optional[str], Optional[dict]]:
    if not a or not b:
        return None, None
    va = getattr(a, attr) or ""
    vb = getattr(b, attr) or ""
    if not va and not vb:
        return None, None
    if va != vb:
        return name, {label_a: va or "(empty)", label_b: vb or "(empty)"}
    return None, None


def reconcile_sources(
    register_records: list[SourceRecord],
    erp_records: list[SourceRecord],
    store_records: list[SourceRecord],
) -> list[ReconciliationResult]:
    """Deterministic three-way reconciliation. Never silently drops source rows."""
    results: list[ReconciliationResult] = []
    result_counter = 0

    def next_id(prefix: str) -> str:
        nonlocal result_counter
        result_counter += 1
        return f"{prefix}:{result_counter:04d}"

    def build_lookup(records: list[SourceRecord]) -> dict[str, list[SourceRecord]]:
        lookup: dict[str, list[SourceRecord]] = {}
        for rec in records:
            if rec.grn_key:
                lookup.setdefault(rec.grn_key, []).append(rec)
        return lookup

    register_by_grn = build_lookup(register_records)
    erp_by_grn = build_lookup(erp_records)
    store_by_grn = build_lookup(store_records)
    all_grns = set(register_by_grn) | set(erp_by_grn) | set(store_by_grn)
    processed_ids: set[str] = set()

    for grn in sorted(all_grns, key=lambda x: int(x) if x.isdigit() else 0):
        reg_list = list(register_by_grn.get(grn, []))
        erp_list = list(erp_by_grn.get(grn, []))
        store_list = list(store_by_grn.get(grn, []))

        # Duplicates in ANY source → preserve every row, do not collapse
        if len(reg_list) > 1 or len(erp_list) > 1 or len(store_list) > 1:
            all_rows = reg_list + erp_list + store_list
            all_ids = [r.source_row_id for r in all_rows]
            dup_parts = []
            if len(reg_list) > 1:
                dup_parts.append(f"register×{len(reg_list)}")
            if len(erp_list) > 1:
                dup_parts.append(f"ERP×{len(erp_list)}")
            if len(store_list) > 1:
                dup_parts.append(f"store×{len(store_list)}")
            results.append(ReconciliationResult(
                result_id=next_id("dup"),
                grn_key=grn,
                status="duplicate_or_ambiguous",
                register_record=reg_list[0] if reg_list else None,
                erp_record=erp_list[0] if erp_list else None,
                store_record=store_list[0] if store_list else None,
                extra_records=all_rows[1:] if len(all_rows) > 1 else [],
                explanation=f"Duplicate GRN {grn} ({', '.join(dup_parts)}); all rows preserved for investigation",
                source_row_ids=all_ids,
            ))
            processed_ids.update(all_ids)
            continue

        reg_rec = reg_list[0] if reg_list else None
        erp_rec = erp_list[0] if erp_list else None
        store_rec = store_list[0] if store_list else None
        source_ids = [r.source_row_id for r in (reg_rec, erp_rec, store_rec) if r]

        status = "matched"
        mismatch_fields: list[str] = []
        mismatch_details: dict = {}
        explanation_parts: list[str] = []

        # Precedence 1: unresolved OCR / invalid critical fields — never verified
        if reg_rec and (reg_rec.uncertain_fields or reg_rec.norm_qty is None or reg_rec.date_ambiguous):
            status = "uncertain_ocr_review"
            if reg_rec.uncertain_fields:
                explanation_parts.append(f"Uncertain OCR fields: {reg_rec.uncertain_fields}")
            if reg_rec.norm_qty is None:
                explanation_parts.append("Register quantity is null/unreadable")
            if reg_rec.date_ambiguous:
                explanation_parts.append("Register date is ambiguous")

        # Precedence 2: missing sources (explicit combinations)
        if status == "matched":
            present = {
                "register": reg_rec is not None,
                "erp": erp_rec is not None,
                "store": store_rec is not None,
            }
            missing = [k for k, v in present.items() if not v]
            if missing:
                if missing == ["erp"]:
                    status = "missing_from_erp"
                elif missing == ["store"]:
                    status = "missing_from_store"
                elif missing == ["register"]:
                    status = "missing_from_register"
                else:
                    status = "missing_from_multiple"
                explanation_parts.append(
                    f"GRN {grn} present in {[k for k,v in present.items() if v]}; missing {missing}"
                )

        # Precedence 3: field comparisons among present sources
        if status == "matched":
            pairs = []
            if reg_rec and erp_rec:
                pairs.append(("register", reg_rec, "erp", erp_rec))
            if reg_rec and store_rec:
                pairs.append(("register", reg_rec, "store", store_rec))
            if erp_rec and store_rec:
                pairs.append(("erp", erp_rec, "store", store_rec))

            original_attr = {
                "supplier": "original_supplier",
                "item": "original_item",
                "lot_no": "original_lot",
                "date": "original_date",
            }
            for la, a, lb, b in pairs:
                equal, reason = compare_quantities(a.norm_qty, a.norm_uom, b.norm_qty, b.norm_uom)
                key = f"qty_{la}_vs_{lb}"
                if not equal:
                    mismatch_fields.append(key)
                    mismatch_details[key] = {
                        la: f"{a.original_qty} {a.original_uom}",
                        lb: f"{b.original_qty} {b.original_uom}",
                        "reason": reason,
                    }

                for field_name, attr in (
                    ("supplier", "norm_supplier"),
                    ("item", "norm_item"),
                    ("lot_no", "norm_lot"),
                    ("date", "norm_date"),
                ):
                    fname, _detail = _compare_text_field(field_name, a, b, attr, la, lb)
                    if fname:
                        if field_name not in mismatch_fields:
                            mismatch_fields.append(field_name)
                        mismatch_details.setdefault(field_name, {})
                        oattr = original_attr[field_name]
                        mismatch_details[field_name][la] = getattr(a, oattr) or getattr(a, attr)
                        mismatch_details[field_name][lb] = getattr(b, oattr) or getattr(b, attr)

            if mismatch_fields:
                status = "mismatched"
                explanation_parts.append(f"Fields differ: {', '.join(mismatch_fields)}")
            else:
                explanation_parts.append("All compared fields match (qty/unit, supplier, item, lot, date)")

        results.append(ReconciliationResult(
            result_id=next_id("grp"),
            grn_key=grn,
            status=status,
            register_record=reg_rec,
            erp_record=erp_rec,
            store_record=store_rec,
            mismatch_fields=mismatch_fields,
            mismatch_details=mismatch_details,
            explanation="; ".join(explanation_parts),
            source_row_ids=source_ids,
        ))
        processed_ids.update(source_ids)

    # Rows without GRN — never drop
    for source_name, recs in (
        ("register", register_records),
        ("erp", erp_records),
        ("store", store_records),
    ):
        for rec in recs:
            if rec.source_row_id in processed_ids:
                continue
            results.append(ReconciliationResult(
                result_id=next_id("nogrn"),
                grn_key=None,
                status="duplicate_or_ambiguous",
                register_record=rec if source_name == "register" else None,
                erp_record=rec if source_name == "erp" else None,
                store_record=rec if source_name == "store" else None,
                explanation=(
                    f"{source_name.title()} row without GRN"
                    + (f": {rec.original_remarks}" if rec.original_remarks else "")
                ),
                source_row_ids=[rec.source_row_id],
            ))
            processed_ids.add(rec.source_row_id)

    return results


# =============================================================================
# COMPLETENESS VALIDATION
# =============================================================================

def validate_completeness(
    register_records: list[SourceRecord],
    erp_records: list[SourceRecord],
    store_records: list[SourceRecord],
    reconciliation_results: list[ReconciliationResult],
    extraction_state: Optional[ExtractionState] = None,
) -> CompletenessBundle:
    """
    Prove exactly-once accounting AND gate on extraction success.
    Overall PASS requires extraction OK + every source row accounted exactly once.
    """
    if extraction_state is None:
        extraction_state = ExtractionState(
            ok=False,
            messages=["Extraction state missing — cannot claim completeness PASS"],
        )

    accounted_counts: dict[str, int] = {}
    for result in reconciliation_results:
        for sid in result.source_row_ids:
            accounted_counts[sid] = accounted_counts.get(sid, 0) + 1

    all_input_ids: set[str] = set()
    reports: list[CompletenessReport] = []

    for source_name, records in (
        ("Register", register_records),
        ("ERP", erp_records),
        ("Store", store_records),
    ):
        by_id = {r.source_row_id: r for r in records}
        # Detect duplicate IDs inside the source list itself
        id_list = [r.source_row_id for r in records]
        source_dup_ids = {sid for sid in id_list if id_list.count(sid) > 1}
        original_ids = set(by_id.keys())
        all_input_ids |= original_ids

        accounted = {sid for sid in original_ids if accounted_counts.get(sid, 0) == 1}
        missing = original_ids - set(accounted_counts.keys())
        duplicated = {
            sid for sid in original_ids
            if accounted_counts.get(sid, 0) > 1 or sid in source_dup_ids
        }
        invalid_count = sum(1 for r in records if r.invalid_fields)

        unexpected_in_source = []  # filled later globally
        unresolved_rows = [describe_source_row(by_id[sid]) for sid in sorted(missing)]

        details = []
        if missing:
            details.append(f"{len(missing)} row(s) missing from results")
        if duplicated:
            details.append(f"{len(duplicated)} row(s) duplicated in results/source")
        if invalid_count:
            details.append(f"{invalid_count} row(s) have invalid fields")

        # Row-accounting PASS is exactly-once only; invalid fields are reported separately
        row_pass = len(missing) == 0 and len(duplicated) == 0
        if row_pass:
            summary = (
                f"Row accounting OK — {len(accounted)}/{len(records)} "
                f"{source_name} rows appear exactly once"
            )
            if invalid_count:
                summary += (
                    f" (note: {invalid_count} row(s) have invalid fields; "
                    f"reported separately, does not fail row accounting)"
                )
            summary += "."
        else:
            summary = (
                f"Row accounting FAIL — accounted={len(accounted)}, "
                f"missing={len(missing)}, duplicated={len(duplicated)}."
            )
            if invalid_count:
                summary += f" Invalid fields (separate): {invalid_count}."

        reports.append(CompletenessReport(
            source=source_name,
            original_count=len(records),
            accounted_count=len(accounted),
            unresolved_count=len(missing),
            duplicate_count=len(duplicated),
            invalid_count=invalid_count,
            unexpected_count=0,
            passed=row_pass,
            details=details,
            unresolved_rows=unresolved_rows,
            summary=summary,
        ))

    # Unexpected IDs in results that are not in any input source
    result_ids = set(accounted_counts.keys())
    unexpected = sorted(result_ids - all_input_ids)
    if unexpected:
        for report in reports:
            report.details.append(
                f"Global unexpected source-row IDs in results: {unexpected}"
            )
            report.unexpected_ids = unexpected
            report.unexpected_count = len(unexpected)
            report.passed = False
            report.summary += f" Unexpected IDs: {len(unexpected)}."

    # Unclassified results
    unclassified = [r for r in reconciliation_results if r.status == "unclassified"]
    messages = list(extraction_state.messages)
    if unclassified:
        messages.append(f"{len(unclassified)} unclassified reconciliation result(s)")

    row_accounting_pass = all(r.passed for r in reports) and not unexpected and not unclassified
    extraction_pass = bool(extraction_state.ok)
    overall_pass = row_accounting_pass and extraction_pass

    if not extraction_pass:
        messages.append(
            "Overall completeness FAIL: register extraction incomplete "
            "(missing API key, failed OCR, or missing cache)."
        )
    if not row_accounting_pass:
        messages.append("Overall completeness FAIL: source-row accounting did not tie out.")

    business_exception_count = sum(
        1 for r in reconciliation_results if r.status != "matched"
    )
    pending_review_count = sum(
        1 for r in reconciliation_results if r.status == "uncertain_ocr_review"
    )

    return CompletenessBundle(
        extraction=extraction_state,
        source_reports=reports,
        row_accounting_pass=row_accounting_pass,
        extraction_pass=extraction_pass,
        overall_pass=overall_pass,
        business_exception_count=business_exception_count,
        pending_review_count=pending_review_count,
        messages=messages,
    )


# =============================================================================
# HUMAN REVIEW
# =============================================================================

def init_review_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS review_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_row_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            original_value TEXT,
            corrected_value TEXT,
            decision TEXT NOT NULL,
            reason TEXT,
            reviewer TEXT,
            timestamp TEXT NOT NULL,
            UNIQUE(source_row_id, field_name)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_review_decision(
    source_row_id: str,
    field_name: str,
    original_value: str,
    corrected_value: str,
    decision: str,
    reason: str = "",
    reviewer: str = "user",
):
    """
    Persist latest decision per (source_row_id, field) and always append audit_log.
    Latest decision is upserted; audit history is append-only.
    """
    if decision not in {"confirm", "correct"}:
        raise ValueError("decision must be 'confirm' or 'correct'")
    if decision == "correct":
        if not reason or not str(reason).strip():
            raise ValueError("A reason is required for corrections")
        if corrected_value is None or str(corrected_value).strip() == "":
            raise ValueError("Corrected value is required")
        if field_name == "qty":
            float(str(corrected_value).strip())  # validate numeric

    init_review_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    timestamp = datetime.now().isoformat()
    # Upsert latest decision only (UNIQUE source_row_id + field_name)
    cur.execute("""
        INSERT INTO review_decisions
        (source_row_id, field_name, original_value, corrected_value, decision, reason, reviewer, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_row_id, field_name) DO UPDATE SET
            original_value=excluded.original_value,
            corrected_value=excluded.corrected_value,
            decision=excluded.decision,
            reason=excluded.reason,
            reviewer=excluded.reviewer,
            timestamp=excluded.timestamp
    """, (
        source_row_id, field_name, original_value, corrected_value,
        decision, reason, reviewer, timestamp,
    ))
    # Append-only audit trail — never replaced
    cur.execute(
        "INSERT INTO audit_log (action, details, timestamp) VALUES (?, ?, ?)",
        ("review_decision", json.dumps({
            "source_row_id": source_row_id,
            "field": field_name,
            "original": original_value,
            "corrected": corrected_value,
            "decision": decision,
            "reason": reason,
        }), timestamp),
    )
    conn.commit()
    conn.close()


def get_review_decisions() -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM review_decisions ORDER BY timestamp DESC")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def apply_review_corrections(records: list[SourceRecord]) -> list[SourceRecord]:
    """Apply confirm/correct decisions. Confirm clears uncertainty; correct sets value."""
    from dataclasses import replace

    decisions = get_review_decisions()
    if not decisions:
        return records

    by_sid: dict[str, list[dict]] = {}
    for d in decisions:
        by_sid.setdefault(d["source_row_id"], []).append(d)

    out = []
    for rec in records:
        if rec.source_row_id not in by_sid:
            out.append(rec)
            continue

        new_rec = replace(rec, extra_data=dict(rec.extra_data), uncertain_fields=list(rec.uncertain_fields))
        new_uncertain = list(rec.uncertain_fields)

        for d in by_sid[rec.source_row_id]:
            field_name = d["field_name"]
            if d["decision"] == "confirm":
                if field_name in new_uncertain:
                    new_uncertain.remove(field_name)
                # Confirming qty when value exists clears null uncertainty
                if field_name == "qty" and new_rec.norm_qty is None and new_rec.original_qty not in (None, ""):
                    try:
                        new_rec.norm_qty = float(new_rec.original_qty)
                        new_rec.base_qty, new_rec.base_uom = convert_qty_to_base(
                            new_rec.norm_qty, new_rec.norm_uom or ""
                        )
                    except ValueError:
                        pass
            elif d["decision"] == "correct":
                value = d["corrected_value"]
                if field_name == "qty":
                    new_rec.norm_qty = float(value)
                    new_rec.original_qty = str(value)
                    new_rec.base_qty, new_rec.base_uom = convert_qty_to_base(
                        new_rec.norm_qty, new_rec.norm_uom or ""
                    )
                elif field_name == "lot_no":
                    new_rec.norm_lot = str(value).strip().upper()
                    new_rec.original_lot = str(value)
                elif field_name == "date":
                    parsed = parse_date(
                        value,
                        preferred_formats=["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"],
                        default_year=REGISTER_DATE_YEAR,
                    )
                    new_rec.norm_date = parsed.iso
                    new_rec.original_date = str(value)
                    new_rec.date_ambiguous = parsed.ambiguous
                elif field_name == "supplier":
                    new_rec.original_supplier = str(value)
                    new_rec.norm_supplier = normalize_supplier(value)
                elif field_name == "item":
                    new_rec.original_item = str(value)
                    new_rec.norm_item = normalize_item(value)
                if field_name in new_uncertain:
                    new_uncertain.remove(field_name)

        new_rec.uncertain_fields = new_uncertain
        out.append(new_rec)
    return out


def resolve_review_item(
    source_row_id: str,
    field_name: str,
    decision: str,
    original_value: str,
    corrected_value: str = "",
    reason: str = "",
) -> None:
    """Persist a review decision (confirm or correct)."""
    save_review_decision(
        source_row_id=source_row_id,
        field_name=field_name,
        original_value=original_value,
        corrected_value=corrected_value if decision == "correct" else original_value,
        decision=decision,
        reason=reason if decision == "correct" else (reason or "Confirmed extracted value"),
    )


# =============================================================================
# PIPELINE
# =============================================================================

def run_reconciliation_pipeline(
    register_records: list[SourceRecord],
    erp_records: list[SourceRecord],
    store_records: list[SourceRecord],
    extraction_state: ExtractionState,
    apply_reviews: bool = True,
) -> tuple[list[SourceRecord], list[ReconciliationResult], CompletenessBundle]:
    if apply_reviews:
        register_records = apply_review_corrections(register_records)
    results = reconcile_sources(register_records, erp_records, store_records)
    bundle = validate_completeness(
        register_records, erp_records, store_records, results, extraction_state
    )
    return register_records, results, bundle


# =============================================================================
# EXPORTS
# =============================================================================

def export_results_to_csv(results: list[ReconciliationResult], filepath: Path):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "result_id", "GRN", "Status",
            "Register_Supplier", "Register_Item", "Register_Qty", "Register_UOM",
            "Register_Lot", "Register_Date",
            "ERP_Supplier", "ERP_Item", "ERP_Qty", "ERP_UOM", "ERP_Lot", "ERP_Date",
            "Store_Supplier", "Store_Item", "Store_Qty", "Store_UOM", "Store_Lot", "Store_Date",
            "Mismatch_Fields", "Mismatch_Details", "Explanation", "Source_Row_IDs",
        ])
        for r in results:
            reg, erp, store = r.register_record, r.erp_record, r.store_record
            w.writerow([
                r.result_id, r.grn_key or "", r.status,
                reg.original_supplier if reg else "",
                reg.original_item if reg else "",
                reg.original_qty if reg else "",
                reg.original_uom if reg else "",
                reg.original_lot if reg else "",
                reg.original_date if reg else "",
                erp.original_supplier if erp else "",
                erp.original_item if erp else "",
                erp.original_qty if erp else "",
                erp.original_uom if erp else "",
                erp.original_lot if erp else "",
                erp.original_date if erp else "",
                store.original_supplier if store else "",
                store.original_item if store else "",
                store.original_qty if store else "",
                store.original_uom if store else "",
                store.original_lot if store else "",
                store.original_date if store else "",
                "; ".join(r.mismatch_fields),
                json.dumps(r.mismatch_details),
                r.explanation,
                "; ".join(r.source_row_ids),
            ])


def export_discrepancies_csv(results: list[ReconciliationResult], filepath: Path):
    export_results_to_csv([r for r in results if r.status != "matched"], filepath)


def export_disposition_csv(
    register_records: list[SourceRecord],
    erp_records: list[SourceRecord],
    store_records: list[SourceRecord],
    results: list[ReconciliationResult],
    filepath: Path,
):
    """One row per source-row ID with final disposition."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    disposition: dict[str, dict] = {}
    for r in results:
        for sid in r.source_row_ids:
            disposition.setdefault(sid, {
                "source_row_id": sid,
                "result_id": r.result_id,
                "grn_key": r.grn_key or "",
                "status": r.status,
                "explanation": r.explanation,
            })

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "source_row_id", "source_type", "grn_key", "result_id", "status",
            "original_qty", "original_uom", "original_lot", "original_date",
            "explanation",
        ])
        for rec in register_records + erp_records + store_records:
            d = disposition.get(rec.source_row_id, {})
            w.writerow([
                rec.source_row_id,
                rec.source_type,
                rec.grn_key or "",
                d.get("result_id", ""),
                d.get("status", "MISSING_FROM_RESULTS"),
                rec.original_qty or "",
                rec.original_uom or "",
                rec.original_lot or "",
                rec.original_date or "",
                d.get("explanation", "NOT ACCOUNTED"),
            ])


def export_audit_log(filepath: Path):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        filepath.write_text("id,action,details,timestamp\n")
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM audit_log ORDER BY timestamp")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    conn.close()
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)


# =============================================================================
# STREAMLIT UI
# =============================================================================

def run_streamlit_app():
    import streamlit as st
    import pandas as pd
    import tempfile

    st.set_page_config(
        page_title="GRN Reconciliation",
        layout="wide",
    )
    st.title("Three-Way GRN Reconciliation")
    st.caption("Register · ERP · Store")

    if "pipeline_version" not in st.session_state:
        st.session_state.pipeline_version = 0

    with st.sidebar:
        st.header("Run")
        data_mode = st.radio(
            "Data source",
            options=["provided", "upload"],
            format_func=lambda m: (
                "Provided assessment data"
                if m == "provided"
                else "Upload files"
            ),
            help=(
                "Provided: uses kestrel-data (OCR cache for register). "
                "Upload: processes only the files you select."
            ),
        )
        use_gemini = st.checkbox(
            "Gemini OCR",
            value=bool(GEMINI_API_KEY),
            help="Required for uploaded register images.",
        )
        if use_gemini and not GEMINI_API_KEY:
            st.caption("API key not configured.")

        uploaded_images = None
        manual_register_file = None
        uploaded_erp = None
        uploaded_store = None

        if data_mode == "upload":
            st.divider()
            st.caption("Upload all three sources.")
            uploaded_images = st.file_uploader(
                "Register images",
                type=["jpg", "jpeg", "png"],
                accept_multiple_files=True,
            )
            manual_register_file = st.file_uploader(
                "Or typed register (CSV / Excel)",
                type=["csv", "xlsx", "xls"],
            )
            uploaded_erp = st.file_uploader("ERP CSV", type=["csv"])
            uploaded_store = st.file_uploader("Store Excel", type=["xlsx", "xls"])
        else:
            st.caption("Using provided data in kestrel-data/.")

        run_clicked = st.button("Run reconciliation", type="primary", use_container_width=True)

    tabs = st.tabs([
        "Overview", "Results", "Review", "Completeness", "Export",
    ])

    def _save_upload(upload, suffix: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(upload.read())
        tmp.close()
        return Path(tmp.name)

    def execute_pipeline():
        sources_used = {"register": "", "erp": "", "store": "", "mode": data_mode}

        if data_mode == "provided":
            erp_records = load_erp_data()
            store_records = load_store_data()
            images = load_register_images()
            register_records, extraction_state = extract_register_records(
                images,
                api_key=(GEMINI_API_KEY if use_gemini else None),
                use_cache=True,
            )
            sources_used["register"] = f"Provided data ({len(register_records)} rows)"
            sources_used["erp"] = f"Provided data ({len(erp_records)} rows)"
            sources_used["store"] = f"Provided data ({len(store_records)} rows)"
        else:
            # Upload mode: only process uploaded files — no silent defaults.
            missing = []
            if not uploaded_images and not manual_register_file:
                missing.append("Register (images or typed file)")
            if not uploaded_erp:
                missing.append("ERP CSV")
            if not uploaded_store:
                missing.append("Store Excel")
            if missing:
                raise ValueError(
                    "Upload mode requires all sources. Missing: " + ", ".join(missing)
                )

            erp_path = _save_upload(uploaded_erp, ".csv")
            erp_records = load_erp_data(erp_path)
            sources_used["erp"] = f"Upload · {uploaded_erp.name} ({len(erp_records)})"
            erp_path.unlink(missing_ok=True)

            store_path = _save_upload(
                uploaded_store, Path(uploaded_store.name).suffix.lower() or ".xlsx"
            )
            store_records = load_store_data(store_path)
            sources_used["store"] = f"Upload · {uploaded_store.name} ({len(store_records)})"
            store_path.unlink(missing_ok=True)

            if uploaded_images:
                temp_images = []
                for i, img_file in enumerate(uploaded_images):
                    tmp_path = _save_upload(img_file, ".jpg")
                    temp_images.append({
                        "path": tmp_path,
                        "filename": img_file.name,
                        "page_number": i + 1,
                    })
                register_records, extraction_state = extract_register_records(
                    temp_images,
                    api_key=(GEMINI_API_KEY if use_gemini else None),
                    use_cache=False,
                )
                sources_used["register"] = (
                    f"Upload · {len(uploaded_images)} image(s) ({len(register_records)} rows)"
                )
                for info in temp_images:
                    Path(info["path"]).unlink(missing_ok=True)
            else:
                ext = Path(manual_register_file.name).suffix.lower()
                tmp_path = _save_upload(manual_register_file, ext)
                if ext in {".xlsx", ".xls"}:
                    register_records, extraction_state = load_manual_register_excel(tmp_path)
                else:
                    register_records, extraction_state = load_manual_register_data(tmp_path)
                sources_used["register"] = (
                    f"Upload · {manual_register_file.name} ({len(register_records)} rows)"
                )
                tmp_path.unlink(missing_ok=True)

        register_records, results, bundle = run_reconciliation_pipeline(
            register_records, erp_records, store_records, extraction_state
        )
        st.session_state.erp_records = erp_records
        st.session_state.store_records = store_records
        st.session_state.register_records = register_records
        st.session_state.results = results
        st.session_state.bundle = bundle
        st.session_state.extraction_state = extraction_state
        st.session_state.sources_used = sources_used
        st.session_state.pipeline_version += 1

    if run_clicked:
        with st.spinner("Running reconciliation…"):
            try:
                execute_pipeline()
            except Exception as e:
                st.error(str(e))
                logger.exception("Pipeline failed")

    if "results" not in st.session_state:
        with tabs[0]:
            st.write("Select a data source in the sidebar, then run reconciliation.")
        return

    results = st.session_state.results
    erp_records = st.session_state.erp_records
    store_records = st.session_state.store_records
    register_records = st.session_state.register_records
    bundle: CompletenessBundle = st.session_state.bundle
    extraction_state: ExtractionState = st.session_state.extraction_state
    sources_used = st.session_state.get("sources_used", {})

    # ----- Overview -----
    with tabs[0]:
        st.subheader("Overview")
        mode_label = (
            "Provided assessment data"
            if sources_used.get("mode") == "provided"
            else "Uploaded files"
        )
        st.write(f"**Mode:** {mode_label}")
        st.write(
            f"Register: {sources_used.get('register', '—')}  ·  "
            f"ERP: {sources_used.get('erp', '—')}  ·  "
            f"Store: {sources_used.get('store', '—')}"
        )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Register", len(register_records))
        c2.metric("ERP", len(erp_records))
        c3.metric("Store", len(store_records))
        c4.metric("Groups", len(results))

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Exceptions", bundle.business_exception_count)
        c6.metric("Pending review", bundle.pending_review_count)
        c7.metric("Extraction", "PASS" if bundle.extraction_pass else "FAIL")
        c8.metric("Completeness", "PASS" if bundle.overall_pass else "FAIL")

        if not bundle.overall_pass:
            for msg in bundle.messages:
                st.warning(msg)

        st.subheader("Status summary")
        status_counts: dict[str, int] = {}
        for r in results:
            status_counts[r.status] = status_counts.get(r.status, 0) + 1
        summary_rows = [
            {"Status": STATUS_LABELS.get(s, s), "Count": n}
            for s, n in sorted(status_counts.items(), key=lambda x: -x[1])
        ]
        st.dataframe(pd.DataFrame(summary_rows), hide_index=True, use_container_width=True)

    # ----- Results -----
    with tabs[1]:
        st.subheader("Results")
        status_options = sorted({r.status for r in results})
        f1, f2 = st.columns(2)
        with f1:
            status_filter = st.multiselect(
                "Status",
                options=status_options,
                format_func=lambda s: STATUS_LABELS.get(s, s),
            )
        with f2:
            grn_search = st.text_input("GRN")

        filtered = results
        if status_filter:
            filtered = [r for r in filtered if r.status in status_filter]
        if grn_search:
            filtered = [r for r in filtered if grn_search in (r.grn_key or "")]

        rows = []
        for r in filtered:
            reg, erp, store = r.register_record, r.erp_record, r.store_record
            rows.append({
                "Result ID": r.result_id,
                "GRN": r.grn_key or "",
                "Status": STATUS_LABELS.get(r.status, r.status),
                "Register supplier": reg.original_supplier if reg else "",
                "Register item": reg.original_item if reg else "",
                "Register qty": f"{reg.original_qty} {reg.original_uom}".strip() if reg else "",
                "Register lot": reg.original_lot if reg else "",
                "Register date": reg.original_date if reg else "",
                "ERP qty": f"{erp.original_qty} {erp.original_uom}".strip() if erp else "",
                "Store qty": f"{store.original_qty} {store.original_uom}".strip() if store else "",
                "Mismatches": ", ".join(r.mismatch_fields),
                "Source rows": "; ".join(r.source_row_ids),
                "Notes": r.explanation,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        id_options = [r.result_id for r in filtered]
        selected_id = st.selectbox("Detail", options=id_options, index=None)
        if selected_id:
            selected = next(r for r in results if r.result_id == selected_id)
            st.json({
                "result_id": selected.result_id,
                "grn": selected.grn_key,
                "status": selected.status,
                "explanation": selected.explanation,
                "mismatch_fields": selected.mismatch_fields,
                "mismatch_details": selected.mismatch_details,
                "source_row_ids": selected.source_row_ids,
            })

    # ----- Human Review -----
    with tabs[2]:
        st.subheader("Human review")
        review_items = [r for r in results if r.status == "uncertain_ocr_review"]
        if not review_items:
            st.write("No items pending review.")
        else:
            st.write(f"{len(review_items)} item(s) require confirmation or correction.")
            for item in review_items:
                reg = item.register_record
                if not reg:
                    continue
                with st.expander(f"{item.result_id} · GRN {item.grn_key}", expanded=False):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.write("Register")
                        st.write(f"Qty: {reg.original_qty} {reg.original_uom}")
                        st.write(f"Lot: {reg.original_lot}")
                        st.write(f"Date: {reg.original_date}")
                        st.write(f"Uncertain: {reg.uncertain_fields}")
                        if reg.page_number:
                            img_path = DATA_DIR / f"t4_register_page{reg.page_number}.jpg"
                            if img_path.exists():
                                st.image(str(img_path), width=400)
                    with col2:
                        st.write("ERP / Store")
                        if item.erp_record:
                            st.write(
                                f"ERP: {item.erp_record.original_qty} "
                                f"{item.erp_record.original_uom}"
                            )
                        if item.store_record:
                            st.write(
                                f"Store: {item.store_record.original_qty} "
                                f"{item.store_record.original_uom}"
                            )

                    field_name = st.selectbox(
                        "Field",
                        options=sorted(
                            set((reg.uncertain_fields or []) + (
                                ["qty"] if reg.norm_qty is None else []
                            ))
                        ) or ["qty"],
                        key=f"field_{item.result_id}",
                    )
                    original_val = {
                        "qty": reg.original_qty or "",
                        "lot_no": reg.original_lot or "",
                        "date": reg.original_date or "",
                        "supplier": reg.original_supplier or "",
                        "item": reg.original_item or "",
                    }.get(field_name, "")
                    corrected = st.text_input(
                        "Corrected value",
                        value=original_val,
                        key=f"corr_{item.result_id}",
                    )
                    reason = st.text_input("Reason", key=f"reason_{item.result_id}")
                    b1, b2 = st.columns(2)
                    with b1:
                        if st.button("Confirm", key=f"confirm_{item.result_id}"):
                            try:
                                resolve_review_item(
                                    source_row_id=reg.source_row_id,
                                    field_name=field_name,
                                    decision="confirm",
                                    original_value=str(original_val),
                                    reason="Confirmed",
                                )
                                execute_pipeline()
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                    with b2:
                        if st.button("Correct", key=f"correct_{item.result_id}"):
                            try:
                                resolve_review_item(
                                    source_row_id=reg.source_row_id,
                                    field_name=field_name,
                                    decision="correct",
                                    original_value=str(original_val),
                                    corrected_value=corrected,
                                    reason=reason,
                                )
                                execute_pipeline()
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

    # ----- Completeness -----
    with tabs[3]:
        st.subheader("Completeness")
        m1, m2, m3 = st.columns(3)
        m1.metric("Extraction", "PASS" if bundle.extraction_pass else "FAIL")
        m2.metric("Row accounting", "PASS" if bundle.row_accounting_pass else "FAIL")
        m3.metric("Overall", "PASS" if bundle.overall_pass else "FAIL")
        st.caption(
            "Overall PASS = extraction OK and exactly-once row accounting. "
            "It does not mean business mismatches are resolved. "
            "Invalid field counts are reported per source separately."
        )

        for report in bundle.source_reports:
            st.write(
                f"**{report.source}** — "
                f"{'PASS' if report.passed else 'FAIL'} · "
                f"loaded {report.original_count}, "
                f"accounted {report.accounted_count}, "
                f"left out {report.unresolved_count}, "
                f"duplicated {report.duplicate_count}, "
                f"invalid {report.invalid_count}"
            )
            if report.unresolved_rows:
                for row in report.unresolved_rows:
                    st.caption(f"{row['label']} — {row['why']}")

        st.subheader("Exceptions")
        exceptions = [r for r in results if r.status != "matched"]
        if not exceptions:
            st.write("None.")
        else:
            st.dataframe(
                pd.DataFrame([
                    {
                        "Result ID": r.result_id,
                        "GRN": r.grn_key or "",
                        "Status": STATUS_LABELS.get(r.status, r.status),
                        "Notes": r.explanation,
                    }
                    for r in exceptions
                ]),
                hide_index=True,
                use_container_width=True,
            )

        st.subheader("Review history")
        decisions = get_review_decisions()
        if decisions:
            st.dataframe(pd.DataFrame(decisions), hide_index=True, use_container_width=True)
        else:
            st.write("No review decisions recorded.")

    # ----- Export -----
    with tabs[4]:
        st.subheader("Export")
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        results_path = EXPORT_DIR / "reconciliation_results.csv"
        disc_path = EXPORT_DIR / "discrepancies.csv"
        disp_path = EXPORT_DIR / "source_row_disposition.csv"
        audit_path = EXPORT_DIR / "audit_log.csv"
        export_results_to_csv(results, results_path)
        export_discrepancies_csv(results, disc_path)
        export_disposition_csv(
            register_records, erp_records, store_records, results, disp_path
        )
        export_audit_log(audit_path)
        for label, path in (
            ("Results", results_path),
            ("Discrepancies", disc_path),
            ("Source-row disposition", disp_path),
            ("Audit log", audit_path),
        ):
            st.download_button(
                label,
                data=path.read_bytes(),
                file_name=path.name,
                mime="text/csv",
                key=f"dl_{path.name}",
            )


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("Running reconciliation against assessment data...")
        erp = load_erp_data()
        store = load_store_data()
        images = load_register_images()
        register, extraction = extract_register_records(
            images, api_key=None, use_cache=True
        )
        print(extraction.summarize())
        register, results, bundle = run_reconciliation_pipeline(
            register, erp, store, extraction
        )
        print(f"Register={len(register)} ERP={len(erp)} Store={len(store)}")
        print(f"Groups={len(results)}")
        counts: dict[str, int] = {}
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        for k, v in sorted(counts.items()):
            print(f"  {k}: {v}")
        print(f"Extraction: {'PASS' if bundle.extraction_pass else 'FAIL'}")
        print(f"Row accounting: {'PASS' if bundle.row_accounting_pass else 'FAIL'}")
        print(f"Overall completeness: {'PASS' if bundle.overall_pass else 'FAIL'}")
        for rep in bundle.source_reports:
            print(
                f"  {rep.source}: {rep.accounted_count}/{rep.original_count} "
                f"invalid={rep.invalid_count} "
                f"({'PASS' if rep.passed else 'FAIL'})"
            )
        for msg in bundle.messages:
            print(f"  note: {msg}")
    else:
        run_streamlit_app()
