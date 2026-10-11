"""
Unit tests for the three-way inventory reconciliation system.
Uses synthetic fixtures — no live Gemini API required.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import (
    SourceRecord,
    ExtractionState,
    CompletenessBundle,
    normalize_uom,
    normalize_supplier,
    normalize_grn,
    normalize_item,
    convert_qty_to_base,
    units_compatible,
    compare_quantities,
    parse_date,
    parse_store_date,
    reconcile_sources,
    validate_completeness,
    extract_register_records,
    apply_review_corrections,
    resolve_review_item,
    run_reconciliation_pipeline,
    DB_PATH,
)


def make_record(
    source_row_id: str,
    source_type: str,
    grn_key: str = None,
    qty: float = None,
    uom: str = None,
    lot: str = None,
    date: str = None,
    supplier: str = None,
    item: str = None,
    uncertain_fields: list = None,
    invalid_fields: list = None,
) -> SourceRecord:
    norm_uom = normalize_uom(uom) if uom else None
    base_qty, base_uom = (qty, norm_uom)
    if qty is not None and norm_uom:
        base_qty, base_uom = convert_qty_to_base(qty, norm_uom)
    return SourceRecord(
        source_row_id=source_row_id,
        source_type=source_type,
        original_grn=grn_key or "",
        original_qty=str(qty) if qty is not None else "",
        original_uom=uom or "",
        original_lot=lot or "",
        original_date=date or "",
        original_supplier=supplier or "",
        original_item=item or "",
        grn_key=normalize_grn(grn_key) if grn_key else None,
        norm_qty=qty,
        norm_uom=norm_uom,
        base_qty=base_qty,
        base_uom=base_uom,
        norm_lot=(lot or "").upper(),
        norm_date=date,
        norm_supplier=normalize_supplier(supplier or ""),
        norm_item=normalize_item(item or ""),
        uncertain_fields=uncertain_fields or [],
        invalid_fields=invalid_fields or [],
    )


class TestUnitNormalization:
    def test_normalize_uom_pcs_variants(self):
        assert normalize_uom("PCS") == "PCS"
        assert normalize_uom("pcs") == "PCS"
        assert normalize_uom("pieces") == "PCS"

    def test_yards_to_metres_generic(self):
        qty, unit = convert_qty_to_base(1200, "yds")
        assert unit == "M"
        assert abs(qty - 1097.28) < 0.01

    def test_compare_yards_metres_equal(self):
        equal, reason = compare_quantities(1200, "yds", 1097.28, "M")
        assert equal is True
        assert "conversion" in reason.lower()

    def test_incompatible_units_reported(self):
        equal, reason = compare_quantities(100, "PCS", 100, "M")
        assert equal is False
        assert "incompatible" in reason.lower()

    def test_units_compatible_convertible(self):
        assert units_compatible("YD", "M") is True
        assert units_compatible("PCS", "BAG") is False


class TestGrnAndSupplier:
    def test_normalize_grn_variants(self):
        assert normalize_grn("GRN-4401") == "4401"
        assert normalize_grn("4401") == "4401"
        assert normalize_grn(4401.0) == "4401"
        assert normalize_grn("") is None
        assert normalize_grn(None) is None

    def test_supplier_exact_alias_not_substring(self):
        assert "QASR PACKAGING" in normalize_supplier("Qasr Pkg")
        # Substring trap: unrelated text containing "PKG" must not alias
        assert normalize_supplier("OTHER PKG CO") == "OTHER PKG CO"
        assert normalize_supplier("REHBAR THREAD") == "REHBAR THREADS"


class TestDateParsing:
    def test_iso_date(self):
        assert parse_date("2026-09-15").iso == "2026-09-15"

    def test_us_mdy_unambiguous(self):
        # day 14 => only M/D is valid
        r = parse_date("9/14/2026", preferred_formats=["%m/%d/%Y", "%d/%m/%Y"])
        assert r.iso == "2026-09-14"
        assert r.ambiguous is False

    def test_ambiguous_md_vs_dm(self):
        r = parse_date("12/09/2026", preferred_formats=["%m/%d/%Y", "%d/%m/%Y"])
        assert r.ambiguous is True
        assert r.iso is None

    def test_register_dd_mm_with_year(self):
        r = parse_date("15/09", default_year=2026)
        assert r.iso == "2026-09-15"

    def test_parse_store_date_helper(self):
        assert parse_store_date("9/14/2026") == "2026-09-14"


class TestReconciliation:
    def test_exact_match_all_three(self):
        register = [make_record("reg:0", "register", "4401", 2000, "pcs", "L1", "2026-09-01", "Qasr Pkg", "Cartons")]
        erp = [make_record("erp:0", "erp", "GRN-4401", 2000, "PCS", "L1", "2026-09-01", "QASR PACKAGING (PVT) LTD", "CARTONS")]
        store = [make_record("store:0", "store", "4401", 2000, "pcs", "L1", "2026-09-01", "Qasr Packaging", "Cartons")]
        results = reconcile_sources(register, erp, store)
        assert len(results) == 1
        assert results[0].status == "matched"
        assert results[0].result_id

    def test_supplier_mismatch(self):
        register = [make_record("reg:0", "register", "4401", 100, "pcs", supplier="Qasr Pkg", item="A")]
        erp = [make_record("erp:0", "erp", "4401", 100, "PCS", supplier="Taraz Poly", item="A")]
        store = [make_record("store:0", "store", "4401", 100, "pcs", supplier="Qasr Packaging", item="A")]
        results = reconcile_sources(register, erp, store)
        assert results[0].status == "mismatched"
        assert "supplier" in results[0].mismatch_fields

    def test_item_mismatch(self):
        register = [make_record("reg:0", "register", "4401", 100, "pcs", item="Cartons 5-ply")]
        erp = [make_record("erp:0", "erp", "4401", 100, "PCS", item="Cartons 3-ply")]
        store = [make_record("store:0", "store", "4401", 100, "pcs", item="Cartons 5-ply")]
        results = reconcile_sources(register, erp, store)
        assert results[0].status == "mismatched"
        assert "item" in results[0].mismatch_fields

    def test_qty_mismatch(self):
        register = [make_record("reg:0", "register", "4417", 48, "ctns")]
        erp = [make_record("erp:0", "erp", "4417", 46, "CTN")]
        store = [make_record("store:0", "store", "4417", 48, "ctns")]
        results = reconcile_sources(register, erp, store)
        assert results[0].status == "mismatched"

    def test_missing_from_each_source(self):
        # missing ERP
        r1 = reconcile_sources(
            [make_record("reg:0", "register", "4421", 75, "rolls")],
            [],
            [make_record("store:0", "store", "4421", 75, "rolls")],
        )
        assert r1[0].status == "missing_from_erp"

        # missing store
        r2 = reconcile_sources(
            [make_record("reg:0", "register", "4401", 1, "pcs")],
            [make_record("erp:0", "erp", "4401", 1, "PCS")],
            [],
        )
        assert r2[0].status == "missing_from_store"

        # missing register
        r3 = reconcile_sources(
            [],
            [make_record("erp:0", "erp", "4401", 1, "PCS")],
            [make_record("store:0", "store", "4401", 1, "pcs")],
        )
        assert r3[0].status == "missing_from_register"

    def test_duplicate_in_each_source_preserved(self):
        # Store duplicates
        store_dup = reconcile_sources(
            [make_record("reg:0", "register", "4409", 80, "rolls")],
            [make_record("erp:0", "erp", "4409", 80, "ROLL")],
            [
                make_record("store:0", "store", "4409", 80, "rolls"),
                make_record("store:1", "store", "4409", 80, "rolls"),
            ],
        )
        assert store_dup[0].status == "duplicate_or_ambiguous"
        assert set(store_dup[0].source_row_ids) == {"reg:0", "erp:0", "store:0", "store:1"}

        # ERP duplicates
        erp_dup = reconcile_sources(
            [make_record("reg:0", "register", "4401", 1, "pcs")],
            [
                make_record("erp:0", "erp", "4401", 1, "PCS"),
                make_record("erp:1", "erp", "4401", 1, "PCS"),
            ],
            [make_record("store:0", "store", "4401", 1, "pcs")],
        )
        assert erp_dup[0].status == "duplicate_or_ambiguous"
        assert "erp:1" in erp_dup[0].source_row_ids

        # Register duplicates
        reg_dup = reconcile_sources(
            [
                make_record("reg:0", "register", "4401", 1, "pcs"),
                make_record("reg:1", "register", "4401", 1, "pcs"),
            ],
            [make_record("erp:0", "erp", "4401", 1, "PCS")],
            [make_record("store:0", "store", "4401", 1, "pcs")],
        )
        assert reg_dup[0].status == "duplicate_or_ambiguous"
        assert "reg:1" in reg_dup[0].source_row_ids

    def test_uncertain_ocr_never_matched(self):
        register = [make_record("reg:0", "register", "4428", None, "bdl", uncertain_fields=["qty"])]
        erp = [make_record("erp:0", "erp", "4428", 250, "BDL")]
        store = [make_record("store:0", "store", "4428", 250, "bdl")]
        results = reconcile_sources(register, erp, store)
        assert results[0].status == "uncertain_ocr_review"

    def test_blank_grn_not_dropped(self):
        results = reconcile_sources([], [], [make_record("store:0", "store", None, -5000, "pcs")])
        assert len(results) == 1
        assert results[0].source_row_ids == ["store:0"]


class TestCompletenessSafety:
    def test_extraction_fail_blocks_overall_pass(self):
        register = [make_record("reg:0", "register", "4401", 100, "pcs")]
        erp = [make_record("erp:0", "erp", "4401", 100, "PCS")]
        store = [make_record("store:0", "store", "4401", 100, "pcs")]
        results = reconcile_sources(register, erp, store)
        bad_extraction = ExtractionState(
            expected_pages=2,
            pages_ok=["p1"],
            pages_missing_cache=["p2"],
            ok=False,
            messages=["missing cache"],
        )
        bundle = validate_completeness(register, erp, store, results, bad_extraction)
        assert isinstance(bundle, CompletenessBundle)
        assert bundle.row_accounting_pass is True
        assert bundle.extraction_pass is False
        assert bundle.overall_pass is False

    def test_missing_cache_fails_extraction(self, tmp_path):
        # Nonexistent image path + no API key => extraction fail
        fake = {
            "path": tmp_path / "missing.jpg",
            "filename": "missing.jpg",
            "page_number": 1,
        }
        # create empty file so hash/cache lookup can run, but no cache
        fake["path"].write_bytes(b"not-a-real-image")
        records, state = extract_register_records([fake], api_key=None, use_cache=True)
        assert state.ok is False
        assert records == []
        assert state.pages_missing_cache or state.messages

    def test_ocr_failure_fails_extraction(self, monkeypatch, tmp_path):
        img = tmp_path / "page.jpg"
        img.write_bytes(b"fake")
        images = [{"path": img, "filename": "page.jpg", "page_number": 1}]

        def boom(*args, **kwargs):
            raise RuntimeError("API down")

        monkeypatch.setattr("app.extract_register_with_gemini", boom)
        records, state = extract_register_records(
            images, api_key="fake-key", use_cache=False
        )
        assert state.ok is False
        assert "page.jpg" in state.pages_failed
        assert records == []

    def test_exactly_once_accounting(self):
        register = [make_record(f"reg:{i}", "register", str(4401 + i), 100, "pcs") for i in range(3)]
        erp = [make_record(f"erp:{i}", "erp", str(4401 + i), 100, "PCS") for i in range(3)]
        store = [make_record(f"store:{i}", "store", str(4401 + i), 100, "pcs") for i in range(3)]
        results = reconcile_sources(register, erp, store)
        ok_extraction = ExtractionState(expected_pages=1, pages_ok=["x"], ok=True, record_count=3)
        bundle = validate_completeness(register, erp, store, results, ok_extraction)
        assert bundle.overall_pass is True
        assert all(r.passed for r in bundle.source_reports)

    def test_invalid_count_not_hardcoded_zero(self):
        register = [make_record("reg:0", "register", "4401", 100, "pcs", invalid_fields=["qty"])]
        erp = [make_record("erp:0", "erp", "4401", 100, "PCS")]
        store = [make_record("store:0", "store", "4401", 100, "pcs")]
        results = reconcile_sources(register, erp, store)
        ok_extraction = ExtractionState(ok=True, expected_pages=1, pages_ok=["x"])
        bundle = validate_completeness(register, erp, store, results, ok_extraction)
        reg_report = next(r for r in bundle.source_reports if r.source == "Register")
        assert reg_report.invalid_count == 1


class TestHumanReview:
    def test_confirm_and_correct_change_outcome(self, tmp_path, monkeypatch):
        db = tmp_path / "reviews.db"
        monkeypatch.setattr("app.DB_PATH", db)

        register = [make_record("reg:0", "register", "4428", None, "bdl", uncertain_fields=["qty"])]
        erp = [make_record("erp:0", "erp", "4428", 250, "BDL")]
        store = [make_record("store:0", "store", "4428", 250, "bdl")]
        extraction = ExtractionState(ok=True, expected_pages=1, pages_ok=["p"], record_count=1)

        _, results_before, _ = run_reconciliation_pipeline(
            register, erp, store, extraction, apply_reviews=True
        )
        assert results_before[0].status == "uncertain_ocr_review"

        # Correct requires reason
        with pytest.raises(ValueError):
            resolve_review_item("reg:0", "qty", "correct", "", "250", reason="")

        resolve_review_item("reg:0", "qty", "correct", "", "250", reason="Read from blot as 250")
        register2, results_after, bundle = run_reconciliation_pipeline(
            [make_record("reg:0", "register", "4428", None, "bdl", uncertain_fields=["qty"])],
            erp,
            store,
            extraction,
            apply_reviews=True,
        )
        assert results_after[0].status == "matched"
        assert register2[0].norm_qty == 250.0
        assert "qty" not in register2[0].uncertain_fields

    def test_confirm_clears_uncertainty_without_changing_value(self, tmp_path, monkeypatch):
        db = tmp_path / "reviews2.db"
        monkeypatch.setattr("app.DB_PATH", db)
        register = [make_record("reg:0", "register", "4419", 66, "drums", uncertain_fields=["qty"])]
        resolve_review_item("reg:0", "qty", "confirm", "66", reason="Confirmed strike-through correction")
        out = apply_review_corrections(register)
        assert out[0].norm_qty == 66
        assert "qty" not in out[0].uncertain_fields


class TestAssessmentDataSmoke:
    """Smoke test against real kestrel-data — skips OCR assertions if cache missing."""

    def test_assessment_pipeline(self):
        from app import load_erp_data, load_store_data, load_register_images

        erp = load_erp_data()
        store = load_store_data()
        images = load_register_images()
        register, extraction = extract_register_records(images, api_key=None, use_cache=True)
        register, results, bundle = run_reconciliation_pipeline(
            register, erp, store, extraction
        )

        assert len(erp) == 28
        assert len(store) == 30
        if not extraction.ok:
            assert bundle.overall_pass is False
            pytest.skip("OCR cache unavailable — cannot assert 28 register rows")

        assert len(register) == 28
        assert bundle.extraction_pass is True
        assert bundle.row_accounting_pass is True
        assert bundle.overall_pass is True

        by = {r.grn_key: r for r in results if r.grn_key}
        assert by["4409"].status == "duplicate_or_ambiguous"
        assert "lot_no" in by["4412"].mismatch_fields
        assert "date" in by["4415"].mismatch_fields
        assert by["4417"].status == "mismatched"
        assert by["4419"].status == "mismatched"
        assert by["4421"].status == "missing_from_erp"
        assert by["4425"].status == "mismatched"
        # Register 1200 yds ≡ ERP 1097.28 M; Store 1200 m mismatches both
        assert "qty_register_vs_erp" not in by["4425"].mismatch_fields
        assert "qty_erp_vs_store" in by["4425"].mismatch_fields
        assert by["4428"].status == "uncertain_ocr_review"
        assert by["4430"].status == "missing_from_multiple"

        blanks = [r for r in results if r.grn_key is None]
        assert len(blanks) == 1
        assert blanks[0].store_record is not None
        assert float(blanks[0].store_record.norm_qty) == -5000


class TestAuditHistory:
    def test_repeated_reviews_append_audit(self, tmp_path, monkeypatch):
        db = tmp_path / "audit.db"
        monkeypatch.setattr("app.DB_PATH", db)
        resolve_review_item("reg:0", "qty", "correct", "", "100", reason="first")
        resolve_review_item("reg:0", "qty", "correct", "100", "250", reason="second")
        from app import get_review_decisions
        import sqlite3
        from app import DB_PATH
        decisions = get_review_decisions()
        assert len(decisions) == 1
        assert decisions[0]["corrected_value"] == "250"
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        conn.close()
        assert n == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
