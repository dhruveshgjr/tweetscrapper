#!/usr/bin/env python3
"""
tests/test_excel_export.py
Validation for the Excel export pipeline (6_excel_exporter.py):
Tweet-ID text integrity, Clean Dates column, column ordering,
dataset counts, invalid-date handling, methodology length.
"""

import csv
import re
import sqlite3
import sys
import importlib.util
from datetime import date
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
PROJECT_DIR = TESTS_DIR.parent


def import_module_from_file(name, rel_path):
    path = PROJECT_DIR / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


excel = import_module_from_file("excel_exporter", "6_excel_exporter.py")

try:
    from openpyxl import load_workbook

    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

needs_openpyxl = pytest.mark.skipif(not HAS_OPENPYXL, reason="openpyxl not installed")

# Real-world 18–19 digit Tweet IDs from the project datasets.
REAL_IDS = [
    "1311404034893516805",  # FOWODE_UGANDA example from the bug report
    "1311375114085294080",
    "1575030693578838016",
    "1930233313823801568",  # 19 digits
    "1217440663991615489",
]

DATE_CASES = [  # (original Date, expected client-facing 'D Mon YYYY')
    ("Wed Sep 30 20:34:21 +0000 2020", "30 Sep 2020"),
    ("Wed Sep 27 12:31:43 +0000 2023", "27 Sep 2023"),
    ("Wed Sep 25 07:56:49 +0000 2024", "25 Sep 2024"),
    ("Wed Sep 23 13:18:52 +0000 2020", "23 Sep 2020"),
    ("Fri Jan 15 13:37:27 +0000 2021", "15 Jan 2021"),
    ("Sat Dec 31 23:59:59 +0000 2022", "31 Dec 2022"),
    ("Mon Jan 02 08:00:00 +0000 2025", "2 Jan 2025"),
    ("Fri Apr 03 07:30:49 +0000 2026", "3 Apr 2026"),
]


def make_db(path: Path, rows) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE tweets (
        tweet_id TEXT PRIMARY KEY, handle TEXT, created_at TEXT, full_text TEXT,
        likes INTEGER, retweets INTEGER, replies INTEGER, quotes INTEGER, views INTEGER,
        url TEXT, scraped_at TEXT)""")
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO tweets VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))", r)
    conn.commit()
    conn.close()
    return path


def sample_rows(handle="TestHandle", ids=None, dates=None):
    ids = ids or REAL_IDS
    dates = dates or [c[0] for c in DATE_CASES]
    rows = []
    for i, tid in enumerate(ids):
        created = dates[i % len(dates)]
        rows.append((tid, handle, created, f"Sample text {i}",
                     i, i, 0, 0, 100 + i, f"https://x.com/{handle}/status/{tid}"))
    return rows


def find_header_row(ws):
    for row in ws.iter_rows(values_only=False):
        vals = [c.value for c in row]
        if vals and vals[0] == "Tweet ID":
            return row
    raise AssertionError("Header row starting with 'Tweet ID' not found")


class TestValidateTweetId:
    @pytest.mark.parametrize("tid", REAL_IDS)
    def test_real_ids_accepted_as_strings(self, tid):
        out = excel.validate_tweet_id(tid)
        assert isinstance(out, str)
        assert out == tid
        assert out.isdigit()

    def test_int_coerced_without_precision_loss(self):
        assert excel.validate_tweet_id(1311404034893516805) == "1311404034893516805"

    @pytest.mark.parametrize("bad", [
        "1.31E+18", "1.3114E+18", "1311404034893516800.0", "", "   ",
        "abc123", "123-456", 1.31e18, None, b"123",
    ])
    def test_rejects_scientific_notation_and_non_strings(self, bad):
        with pytest.raises(ValueError):
            excel.validate_tweet_id(bad)


class TestCleanDates:
    @pytest.mark.parametrize("raw,expected", DATE_CASES)
    def test_conversion_across_years(self, raw, expected):
        assert excel.format_clean_date_csv(raw) == expected
        d = excel.parse_clean_date(raw)
        assert isinstance(d, date)
        assert excel.format_client_date(d) == expected

    @pytest.mark.parametrize("bad", ["", None, "not a date", "32/13/99", 12345, "N/A"])
    def test_invalid_dates_never_crash(self, bad):
        assert excel.parse_clean_date(bad) is None
        assert excel.format_clean_date_csv(bad) == ""


class TestWorkbookExport:
    @needs_openpyxl
    def test_tweet_ids_stored_as_text_with_at_format(self, tmp_path):
        db = make_db(tmp_path / "t.db", sample_rows())
        out = excel.export_to_workbook("TestHandle", db, tmp_path / "out.xlsx")
        wb = load_workbook(out)
        ws = wb.active
        header = find_header_row(ws)
        tid_col = next(i for i, c in enumerate(header) if c.value == "Tweet ID") + 1
        ids = [ws.cell(row=r, column=tid_col).value
               for r in range(header[0].row + 1, ws.max_row + 1)
               if ws.cell(row=r, column=tid_col).value not in (None, "METHODOLOGY")]
        # Drop trailing methodology block rows.
        ids = [v for v in ids if isinstance(v, str) and v.isdigit()]
        assert sorted(ids) == sorted(REAL_IDS)
        for r in range(header[0].row + 1, header[0].row + 1 + len(ids)):
            cell = ws.cell(row=r, column=tid_col)
            assert isinstance(cell.value, str), f"Row {r}: ID not stored as text"
            assert cell.number_format == "@", f"Row {r}: ID format is not Text (@)"
            assert "E" not in str(cell.value).upper() or not re.match(
                r"^\d+(\.\d+)?E[+-]?\d+$", str(cell.value), re.IGNORECASE)

    @needs_openpyxl
    def test_column_order_and_original_date_preserved(self, tmp_path):
        raw_dates = [c[0] for c in DATE_CASES]
        db = make_db(tmp_path / "t.db", sample_rows(dates=raw_dates))
        out = excel.export_to_workbook("TestHandle", db, tmp_path / "out.xlsx")
        ws = load_workbook(out).active
        header = [c.value for c in find_header_row(ws)]
        assert header[:4] == ["Tweet ID", "Date", "Clean Dates", "Author"]
        assert header.index("Clean Dates") == header.index("Date") + 1
        assert header.index("Author") == header.index("Clean Dates") + 1
        tid_i, date_i, clean_i = (header.index("Tweet ID"), header.index("Date"),
                                 header.index("Clean Dates"))
        first = find_header_row(ws)[0].row + 1
        for k, raw in enumerate(raw_dates[: len(REAL_IDS)]):
            assert ws.cell(row=first + k, column=date_i + 1).value == raw
            clean_cell = ws.cell(row=first + k, column=clean_i + 1)
            assert clean_cell.number_format == "d mmm yyyy"
            assert clean_cell.value is not None and clean_cell.value != ""

    @needs_openpyxl
    def test_no_formulas_used_for_ids_or_dates(self, tmp_path):
        db = make_db(tmp_path / "t.db", sample_rows())
        out = excel.export_to_workbook("TestHandle", db, tmp_path / "out.xlsx")
        ws = load_workbook(out).active
        for row in ws.iter_rows():
            for c in row:
                assert not (isinstance(c.value, str) and c.value.startswith("=")), (
                    f"Formula found at {c.coordinate}: {c.value!r}")

    @needs_openpyxl
    def test_export_reimport_roundtrip_preserves_ids(self, tmp_path):
        db = make_db(tmp_path / "t.db", sample_rows())
        out = excel.export_to_workbook("TestHandle", db, tmp_path / "out.xlsx")
        ws = load_workbook(out, data_only=True).active
        header = [c.value for c in find_header_row(ws)]
        tid_col = header.index("Tweet ID") + 1
        seen = {ws.cell(row=r, column=tid_col).value
                for r in range(find_header_row(ws)[0].row + 1, ws.max_row + 1)}
        for tid in REAL_IDS:
            assert tid in seen, f"Original ID {tid} lost in roundtrip"

    @needs_openpyxl
    def test_missing_invalid_dates_export_as_blank(self, tmp_path):
        rows = [
            ("1311404034893516805", "TestHandle", "Wed Sep 30 20:34:21 +0000 2020",
             "ok", 1, 0, 0, 0, 0, "https://x.com/t/status/1"),
            ("1311375114085294080", "TestHandle", "garbage-date",
             "bad date", 1, 0, 0, 0, 0, "https://x.com/t/status/2"),
            ("1575030693578838016", "TestHandle", "",
             "missing date", 1, 0, 0, 0, 0, "https://x.com/t/status/3"),
        ]
        db = make_db(tmp_path / "t.db", rows)
        out = excel.export_to_workbook("TestHandle", db, tmp_path / "out.xlsx")
        ws = load_workbook(out).active
        header = [c.value for c in find_header_row(ws)]
        clean_col = header.index("Clean Dates") + 1
        first = find_header_row(ws)[0].row + 1
        # ORDER BY created_at DESC: garbage/empty sort around the valid date;
        # just check one valid date cell exists and two blanks.
        vals = [ws.cell(row=first + k, column=clean_col).value for k in range(3)]
        non_empty = [v for v in vals if v not in (None, "")]
        assert len(non_empty) == 1
        assert len([v for v in vals if v in (None, "")]) == 2


class TestCsvCompanion:
    def test_csv_ids_full_digits_no_scientific_notation(self, tmp_path):
        db = make_db(tmp_path / "t.db", sample_rows())
        out = excel.export_to_csv("TestHandle", db, tmp_path / "out.csv")
        with open(out, "r", encoding="utf-8-sig") as f:
            content = f.read()
        assert "E+18" not in content and "e+18" not in content
        lines = content.splitlines()
        header_idx = next(i for i, l in enumerate(lines) if l.startswith('"Tweet ID"'))
        reader = csv.DictReader(lines[header_idx:])

        def unwrap_excel_text(v: str) -> str:
            # Export writes IDs as ="<digits>" so Excel keeps them as text.
            if v.startswith('="') and v.endswith('"'):
                return v[2:-1]
            return v

        got = [unwrap_excel_text(r["Tweet ID"]) for r in reader
               if unwrap_excel_text(r.get("Tweet ID") or "").isdigit()]
        assert all('"' not in v and '=' not in v for v in got)
        assert sorted(got) == sorted(REAL_IDS)
        assert reader.fieldnames[:4] == ["Tweet ID", "Date", "Clean Dates", "Author"]

    def test_csv_bom_present(self, tmp_path):
        db = make_db(tmp_path / "t.db", sample_rows())
        out = excel.export_to_csv("TestHandle", db, tmp_path / "out.csv")
        with open(out, "rb") as f:
            assert f.read(3) == b"\xef\xbb\xbf"


class TestSummaryAndMethodology:
    def test_counts_come_from_actual_data(self, tmp_path):
        rows_a = sample_rows(handle="CREAWKenya", ids=REAL_IDS[:3])
        # NOTE: tweet_id is the DB primary key, so handles need distinct IDs.
        other_ids = ["2000000000000000001", "2000000000000000002",
                     "2000000000000000003", "2000000000000000004",
                     "2000000000000000005"]
        rows_b = sample_rows(handle="FOWODE_Uganda", ids=other_ids)
        db = make_db(tmp_path / "t.db", rows_a + rows_b)
        table = excel.dataset_summary_table(db)
        by_handle = {r["target_handle"]: r for r in table}
        assert by_handle["@CREAWKenya"]["total_tweets"] == 3
        assert by_handle["@FOWODE_Uganda"]["total_tweets"] == 5

    def test_zero_and_missing_datasets_shown_clearly(self, tmp_path):
        db = make_db(tmp_path / "t.db", sample_rows(handle="OnlyOne"))
        table = excel.dataset_summary_table(db, handles=["OnlyOne", "GhostHandle"])
        by_handle = {r["target_handle"]: r for r in table}
        assert by_handle["@OnlyOne"]["status"] == "OK"
        assert by_handle["@GhostHandle"]["total_tweets"] == 0
        assert "NO DATA" in by_handle["@GhostHandle"]["status"]

    def test_methodology_is_exactly_4_or_5_sentences(self):
        text = excel.build_methodology()
        sentences = [s for s in re.split(r"\.\s+", text.strip().rstrip(".")) if s.strip()]
        assert 4 <= len(sentences) <= 5, f"Expected 4-5 sentences, got {len(sentences)}"
        lowered = text.lower()
        assert "tweet id" in lowered and "clean dates" in lowered

    @needs_openpyxl
    def test_summary_report_workbook(self, tmp_path):
        db = make_db(tmp_path / "t.db",
                     sample_rows(handle="A", ids=REAL_IDS[:2])
                     + sample_rows(handle="B", ids=REAL_IDS[2:]))
        out = excel.export_summary_report(db, tmp_path / "SUMMARY.xlsx")
        ws = load_workbook(out).active
        vals = [[c.value for c in row] for row in ws.iter_rows()]
        flat = [str(v) for row in vals for v in row if v is not None]
        assert any("METHODOLOGY" in v for v in flat)
        total_row = next(row for row in vals if row[0] == "Total")
        assert total_row[2] == 5
