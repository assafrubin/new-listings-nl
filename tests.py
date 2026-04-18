#!/usr/bin/env python3
"""
Comprehensive test suite for the NL Rental Scanner.

Run with:  python -m unittest tests -v
           python tests.py

Coverage
--------
  db.py       — schema creation, backward-compatible migrations, upsert,
                numeric parsers, subscriber/query CRUD, student flags
  scanner.py  — matches_query (all filter combinations), student LLM
                classification, description fetching, enrichment pipeline,
                Pararius HTML parsing, Funda HTML parsing
  app.py      — all Flask routes, form submissions, UI content / pills
"""

import os
import sys
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock, call, mock_open

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)


# ── helpers ───────────────────────────────────────────────────────────────────

def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _cols(db_path: str, table: str) -> set:
    conn = sqlite3.connect(db_path)
    result = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return result


def _tables(db_path: str) -> set:
    conn = sqlite3.connect(db_path)
    result = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    return result


# ══════════════════════════════════════════════════════════════════════════════
# DB — fresh schema
# ══════════════════════════════════════════════════════════════════════════════

class TestDBSchema(unittest.TestCase):
    """All expected tables and columns are created on a fresh database."""

    def setUp(self):
        self.db_path = _tmp_db()
        self.patcher = patch("db.DB_FILE", self.db_path)
        self.patcher.start()
        import db
        self.db = db

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def test_all_tables_created(self):
        self.db.init_db()
        tables = _tables(self.db_path)
        for t in ["listings", "scan_runs", "subscribers", "customer_queries"]:
            self.assertIn(t, tables)

    def test_listings_columns(self):
        self.db.init_db()
        cols = _cols(self.db_path, "listings")
        expected = ["id", "source", "title", "price", "price_num", "size", "size_num",
                    "rooms", "rooms_num", "energy", "agency", "phone", "url",
                    "first_seen", "last_seen", "city", "student"]
        for c in expected:
            self.assertIn(c, cols, f"listings missing column '{c}'")

    def test_customer_queries_columns(self):
        self.db.init_db()
        cols = _cols(self.db_path, "customer_queries")
        expected = ["id", "subscriber_id", "customer_name", "cities",
                    "min_price", "max_price", "min_rooms", "student",
                    "created_at", "active"]
        for c in expected:
            self.assertIn(c, cols, f"customer_queries missing column '{c}'")

    def test_subscribers_columns(self):
        self.db.init_db()
        cols = _cols(self.db_path, "subscribers")
        for c in ["id", "email", "first_name", "last_name",
                  "whatsapp_group", "created_at", "active"]:
            self.assertIn(c, cols)

    def test_indexes_created(self):
        self.db.init_db()
        conn = sqlite3.connect(self.db_path)
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        for idx in ["idx_source", "idx_price_num", "idx_rooms_num",
                    "idx_first_seen", "idx_sub_email", "idx_cq_sub"]:
            self.assertIn(idx, indexes)

    def test_init_db_is_idempotent(self):
        """Calling init_db() twice must not raise."""
        self.db.init_db()
        self.db.init_db()


# ══════════════════════════════════════════════════════════════════════════════
# DB — backward-compatible migrations
# ══════════════════════════════════════════════════════════════════════════════

class TestDBBackwardCompatibility(unittest.TestCase):
    """
    Simulates upgrading a pre-existing database that is missing the new
    'city' and 'student' columns.  init_db() must add them without losing data.
    """

    def setUp(self):
        self.db_path = _tmp_db()
        self.patcher = patch("db.DB_FILE", self.db_path)
        self.patcher.start()
        import db
        self.db = db

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _create_legacy_schema(self):
        """Reproduce the original schema (no 'city' or 'student' columns)."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE listings (
                id TEXT PRIMARY KEY, source TEXT, title TEXT,
                price TEXT, price_num REAL, size TEXT, size_num REAL,
                rooms TEXT, rooms_num INTEGER, energy TEXT,
                agency TEXT, phone TEXT, url TEXT,
                first_seen TEXT, last_seen TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE customer_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscriber_id INTEGER NOT NULL,
                customer_name TEXT NOT NULL DEFAULT '',
                cities TEXT NOT NULL DEFAULT '[]',
                min_price REAL, max_price REAL, min_rooms INTEGER,
                created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL, created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TEXT NOT NULL, new_count INTEGER NOT NULL DEFAULT 0,
                new_ids TEXT NOT NULL DEFAULT '[]'
            )
        """)
        # Pre-existing listing row
        conn.execute(
            "INSERT INTO listings (id, source, title, price, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?)",
            ("funda-1234567", "Funda", "Old Street 1", "€ 1500 /mnd",
             "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

    def test_migrate_adds_city_to_listings(self):
        self._create_legacy_schema()
        self.db.init_db()
        self.assertIn("city", _cols(self.db_path, "listings"))

    def test_migrate_adds_student_to_listings(self):
        self._create_legacy_schema()
        self.db.init_db()
        self.assertIn("student", _cols(self.db_path, "listings"))

    def test_migrate_adds_student_to_customer_queries(self):
        self._create_legacy_schema()
        self.db.init_db()
        self.assertIn("student", _cols(self.db_path, "customer_queries"))

    def test_migrate_adds_first_last_name_to_subscribers(self):
        self._create_legacy_schema()
        self.db.init_db()
        cols = _cols(self.db_path, "subscribers")
        self.assertIn("first_name", cols)
        self.assertIn("last_name", cols)

    def test_existing_data_preserved_after_migration(self):
        self._create_legacy_schema()
        self.db.init_db()
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT id, title FROM listings WHERE id='funda-1234567'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "Old Street 1")

    def test_student_defaults_to_zero_for_migrated_rows(self):
        self._create_legacy_schema()
        self.db.init_db()
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT student FROM listings WHERE id='funda-1234567'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 0)

    def test_migrate_adds_whatsapp_group_to_subscribers(self):
        self._create_legacy_schema()
        self.db.init_db()
        self.assertIn("whatsapp_group", _cols(self.db_path, "subscribers"))

    def test_whatsapp_group_defaults_to_empty_after_migration(self):
        self._create_legacy_schema()
        # Insert a subscriber in the old schema
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO subscribers (email, created_at) VALUES (?,?)",
                     ("old@example.com", "2024-01-01T00:00:00Z"))
        conn.commit(); conn.close()
        self.db.init_db()
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT whatsapp_group FROM subscribers WHERE email='old@example.com'").fetchone()
        conn.close()
        self.assertEqual(row[0], "")

    def test_migration_is_idempotent(self):
        """Running init_db() on an already-migrated DB must not raise."""
        self._create_legacy_schema()
        self.db.init_db()
        self.db.init_db()


# ══════════════════════════════════════════════════════════════════════════════
# DB — upsert_listings
# ══════════════════════════════════════════════════════════════════════════════

class TestDBUpsert(unittest.TestCase):

    def setUp(self):
        self.db_path = _tmp_db()
        self.patcher = patch("db.DB_FILE", self.db_path)
        self.patcher.start()
        import db
        self.db = db
        db.init_db()

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _get(self, lid: str) -> dict | None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def _base_listing(self, **kw) -> dict:
        base = dict(
            id="pararius-aabbccdd", source="Pararius", city="amsterdam",
            title="Nice Apt", price="€ 1.500 per maand", size="60 m²",
            rooms="2", energy="A", agency="TestAgency", phone="", url="https://x.com/1",
        )
        base.update(kw)
        return base

    def test_insert_new_listing_stored(self):
        self.db.upsert_listings([self._base_listing()])
        row = self._get("pararius-aabbccdd")
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Nice Apt")

    def test_price_num_parsed_on_insert(self):
        self.db.upsert_listings([self._base_listing(price="€ 1.500 per maand")])
        self.assertEqual(self._get("pararius-aabbccdd")["price_num"], 1500.0)

    def test_size_num_parsed_on_insert(self):
        self.db.upsert_listings([self._base_listing(size="60 m²")])
        self.assertEqual(self._get("pararius-aabbccdd")["size_num"], 60.0)

    def test_rooms_num_parsed_on_insert(self):
        self.db.upsert_listings([self._base_listing(rooms="2")])
        self.assertEqual(self._get("pararius-aabbccdd")["rooms_num"], 2)

    def test_student_true_stored_as_1(self):
        self.db.upsert_listings([self._base_listing(student=True)])
        self.assertEqual(self._get("pararius-aabbccdd")["student"], 1)

    def test_student_false_stored_as_0(self):
        self.db.upsert_listings([self._base_listing(student=False)])
        self.assertEqual(self._get("pararius-aabbccdd")["student"], 0)

    def test_student_absent_defaults_to_0(self):
        listing = self._base_listing()
        listing.pop("student", None)
        self.db.upsert_listings([listing])
        self.assertEqual(self._get("pararius-aabbccdd")["student"], 0)

    def test_upsert_preserves_first_seen(self):
        self.db.upsert_listings([self._base_listing()])
        first = self._get("pararius-aabbccdd")["first_seen"]
        self.db.upsert_listings([self._base_listing(title="Updated")])
        self.assertEqual(self._get("pararius-aabbccdd")["first_seen"], first)

    def test_upsert_does_not_downgrade_student_flag(self):
        """A listing classified student=1 stays student=1 on re-upsert with student=0."""
        self.db.upsert_listings([self._base_listing(student=True)])
        self.db.upsert_listings([self._base_listing(student=False)])
        self.assertEqual(self._get("pararius-aabbccdd")["student"], 1)

    def test_upsert_upgrades_student_flag(self):
        """A listing classified student=0 can be upgraded to student=1."""
        self.db.upsert_listings([self._base_listing(student=False)])
        self.db.upsert_listings([self._base_listing(student=True)])
        self.assertEqual(self._get("pararius-aabbccdd")["student"], 1)

    def test_get_seen_ids_returns_all(self):
        self.db.upsert_listings([
            self._base_listing(id="funda-1111111"),
            self._base_listing(id="pararius-aaaaaaaa"),
        ])
        seen = self.db.get_seen_ids()
        self.assertIn("funda-1111111", seen)
        self.assertIn("pararius-aaaaaaaa", seen)

    def test_empty_list_is_noop(self):
        self.db.upsert_listings([])
        seen = self.db.get_seen_ids()
        self.assertEqual(len(seen), 0)

    def test_multiple_listings_inserted(self):
        self.db.upsert_listings([
            self._base_listing(id=f"funda-{i}000000") for i in range(1, 6)
        ])
        self.assertEqual(len(self.db.get_seen_ids()), 5)


# ══════════════════════════════════════════════════════════════════════════════
# DB — subscriber / query CRUD
# ══════════════════════════════════════════════════════════════════════════════

class TestDBSubscriberQueries(unittest.TestCase):

    def setUp(self):
        self.db_path = _tmp_db()
        self.patcher = patch("db.DB_FILE", self.db_path)
        self.patcher.start()
        import db
        self.db = db
        db.init_db()
        self.sub_id = db.add_subscriber("test@example.com", "Test", "User")

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _queries(self) -> list:
        return self.db.get_subscribers_with_queries()[0]["queries"]

    def test_add_subscriber_returns_id(self):
        self.assertIsInstance(self.sub_id, int)
        self.assertGreater(self.sub_id, 0)

    def test_add_query_default_student_is_0(self):
        self.db.add_customer_query(self.sub_id, "John", ["amsterdam"], 1000, 2000, 2)
        self.assertEqual(self._queries()[0]["student"], 0)

    def test_add_query_student_true(self):
        self.db.add_customer_query(self.sub_id, "Maria", ["amsterdam"], None, None, None, student=True)
        self.assertEqual(self._queries()[0]["student"], 1)

    def test_add_query_student_false_explicit(self):
        self.db.add_customer_query(self.sub_id, "Bob", ["utrecht"], None, None, None, student=False)
        self.assertEqual(self._queries()[0]["student"], 0)

    def test_cities_lowercased_and_deserialized(self):
        self.db.add_customer_query(self.sub_id, "Multi", ["Amsterdam", "Utrecht", "Den Haag"], None, None, None)
        cities = self._queries()[0]["cities"]
        self.assertIsInstance(cities, list)
        self.assertIn("amsterdam", cities)
        self.assertIn("utrecht", cities)
        self.assertIn("den haag", cities)

    def test_multiple_queries_for_same_subscriber(self):
        self.db.add_customer_query(self.sub_id, "Q1", ["amsterdam"], None, None, None)
        self.db.add_customer_query(self.sub_id, "Q2", ["utrecht"], 1000, 2500, 2, student=True)
        self.assertEqual(len(self._queries()), 2)

    def test_remove_query_soft_deletes(self):
        qid = self.db.add_customer_query(self.sub_id, "Remove Me", ["amsterdam"], None, None, None)
        self.db.remove_customer_query(qid)
        self.assertEqual(len(self._queries()), 0)

    def test_remove_subscriber_soft_deletes(self):
        self.db.remove_subscriber(self.sub_id)
        self.assertEqual(len(self.db.get_subscribers_with_queries()), 0)

    def test_remove_subscriber_also_removes_queries(self):
        self.db.add_customer_query(self.sub_id, "Q", ["amsterdam"], None, None, None)
        self.db.remove_subscriber(self.sub_id)
        # Row still in DB but marked inactive
        conn = sqlite3.connect(self.db_path)
        active = conn.execute(
            "SELECT active FROM customer_queries WHERE subscriber_id=?", (self.sub_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(active[0], 0)

    def test_add_subscriber_default_whatsapp_group_empty(self):
        subs = self.db.get_subscribers_with_queries()
        self.assertEqual(subs[0]["whatsapp_group"], "")

    def test_add_subscriber_with_whatsapp_group(self):
        sid = self.db.add_subscriber("wa@example.com", "WA", "User",
                                     whatsapp_group="120363043051405349@g.us")
        subs = self.db.get_subscribers_with_queries()
        sub = next(s for s in subs if s["id"] == sid)
        self.assertEqual(sub["whatsapp_group"], "120363043051405349@g.us")

    def test_set_subscriber_whatsapp_group(self):
        self.db.set_subscriber_whatsapp_group(self.sub_id, "120363043051405349@g.us")
        subs = self.db.get_subscribers_with_queries()
        self.assertEqual(subs[0]["whatsapp_group"], "120363043051405349@g.us")

    def test_set_subscriber_whatsapp_group_clear(self):
        self.db.set_subscriber_whatsapp_group(self.sub_id, "120363043051405349@g.us")
        self.db.set_subscriber_whatsapp_group(self.sub_id, "")
        subs = self.db.get_subscribers_with_queries()
        self.assertEqual(subs[0]["whatsapp_group"], "")

    def test_record_scan_run(self):
        self.db.record_scan_run(["funda-1111111", "pararius-aaaaaaaa"])
        runs = self.db.get_scan_runs(limit=1)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["new_count"], 2)


# ══════════════════════════════════════════════════════════════════════════════
# DB — numeric parsers
# ══════════════════════════════════════════════════════════════════════════════

class TestDBParsers(unittest.TestCase):

    def setUp(self):
        import db
        self.db = db

    def test_price_dutch_period_separator(self):
        self.assertEqual(self.db._parse_price("€ 2.500 per maand"), 2500.0)

    def test_price_english_comma_separator(self):
        self.assertEqual(self.db._parse_price("€2,500 /month"), 2500.0)

    def test_price_plain_number(self):
        self.assertEqual(self.db._parse_price("€ 1200 per maand"), 1200.0)

    def test_price_unparseable_returns_none(self):
        self.assertIsNone(self.db._parse_price("Prijs op aanvraag"))

    def test_price_empty_returns_none(self):
        self.assertIsNone(self.db._parse_price(""))

    def test_size_m2(self):
        self.assertEqual(self.db._parse_size("75 m²"), 75.0)

    def test_size_m2_no_space(self):
        self.assertEqual(self.db._parse_size("120m2"), 120.0)

    def test_size_empty_returns_none(self):
        self.assertIsNone(self.db._parse_size(""))

    def test_rooms_plain_number(self):
        self.assertEqual(self.db._parse_rooms("3"), 3)

    def test_rooms_with_label(self):
        self.assertEqual(self.db._parse_rooms("2 bedrooms"), 2)

    def test_rooms_empty_returns_none(self):
        self.assertIsNone(self.db._parse_rooms(""))


# ══════════════════════════════════════════════════════════════════════════════
# Scanner — matches_query (all filter combinations)
# ══════════════════════════════════════════════════════════════════════════════

class TestMatchesQuery(unittest.TestCase):

    def setUp(self):
        import scanner
        self.matches = scanner.matches_query

    def _listing(self, **kw) -> dict:
        base = dict(
            id="funda-1234567", source="Funda", city="amsterdam",
            title="Test", price="€ 2000 /mnd", size="60 m²",
            rooms="2", energy="A", agency="", phone="", url="",
            student=False,
        )
        base.update(kw)
        return base

    def _query(self, **kw) -> dict:
        base = dict(cities=["amsterdam"], min_price=None, max_price=None,
                    min_rooms=None, student=0)
        base.update(kw)
        return base

    # city ────────────────────────────────────────────────────────────────────

    def test_city_exact_match(self):
        self.assertTrue(self.matches(self._listing(city="amsterdam"), self._query(cities=["amsterdam"])))

    def test_city_mismatch(self):
        self.assertFalse(self.matches(self._listing(city="rotterdam"), self._query(cities=["amsterdam"])))

    def test_city_case_insensitive(self):
        self.assertTrue(self.matches(self._listing(city="Amsterdam"), self._query(cities=["amsterdam"])))

    def test_city_empty_query_matches_any(self):
        self.assertTrue(self.matches(self._listing(city="eindhoven"), self._query(cities=[])))

    def test_city_one_of_multiple(self):
        self.assertTrue(self.matches(self._listing(city="utrecht"),
                                     self._query(cities=["amsterdam", "utrecht"])))

    def test_city_not_in_multiple(self):
        self.assertFalse(self.matches(self._listing(city="groningen"),
                                      self._query(cities=["amsterdam", "utrecht"])))

    # price ───────────────────────────────────────────────────────────────────

    def test_price_within_range(self):
        self.assertTrue(self.matches(self._listing(price="€ 1500 /mnd"),
                                     self._query(min_price=1000, max_price=2000)))

    def test_price_below_min_excluded(self):
        self.assertFalse(self.matches(self._listing(price="€ 800 /mnd"),
                                      self._query(min_price=1000)))

    def test_price_above_max_excluded(self):
        self.assertFalse(self.matches(self._listing(price="€ 3000 /mnd"),
                                      self._query(max_price=2500)))

    def test_price_at_min_boundary_included(self):
        self.assertTrue(self.matches(self._listing(price="€ 1000 /mnd"),
                                     self._query(min_price=1000)))

    def test_price_at_max_boundary_included(self):
        self.assertTrue(self.matches(self._listing(price="€ 2500 /mnd"),
                                     self._query(max_price=2500)))

    def test_no_price_filter_passes_all(self):
        self.assertTrue(self.matches(self._listing(price="€ 9999 /mnd"), self._query()))

    def test_unparseable_price_skips_filter(self):
        self.assertTrue(self.matches(self._listing(price="Prijs op aanvraag"),
                                     self._query(min_price=1000, max_price=2000)))

    def test_empty_price_skips_filter(self):
        self.assertTrue(self.matches(self._listing(price=""),
                                     self._query(min_price=500, max_price=3000)))

    # rooms ───────────────────────────────────────────────────────────────────

    def test_rooms_meets_minimum(self):
        self.assertTrue(self.matches(self._listing(rooms="3"), self._query(min_rooms=2)))

    def test_rooms_below_minimum_excluded(self):
        self.assertFalse(self.matches(self._listing(rooms="1"), self._query(min_rooms=2)))

    def test_rooms_exactly_at_minimum(self):
        self.assertTrue(self.matches(self._listing(rooms="2"), self._query(min_rooms=2)))

    def test_no_rooms_filter_passes_all(self):
        self.assertTrue(self.matches(self._listing(rooms="1"), self._query()))

    def test_unparseable_rooms_skips_filter(self):
        self.assertTrue(self.matches(self._listing(rooms=""), self._query(min_rooms=2)))

    # student ─────────────────────────────────────────────────────────────────

    def test_student_listing_excluded_from_non_student_query(self):
        """Core rule: student-only listing is hidden from non-student queries."""
        self.assertFalse(self.matches(self._listing(student=True), self._query(student=0)))

    def test_student_listing_shown_to_student_query(self):
        """Student query receives student listings."""
        self.assertTrue(self.matches(self._listing(student=True), self._query(student=1)))

    def test_normal_listing_shown_to_non_student_query(self):
        """Normal listing is always visible to non-student queries."""
        self.assertTrue(self.matches(self._listing(student=False), self._query(student=0)))

    def test_normal_listing_also_shown_to_student_query(self):
        """Student query sees normal listings too (not exclusive)."""
        self.assertTrue(self.matches(self._listing(student=False), self._query(student=1)))

    def test_student_flag_as_truthy_int(self):
        """student=1 (integer) from DB also triggers the filter."""
        self.assertFalse(self.matches(self._listing(student=1), self._query(student=0)))

    # combined ────────────────────────────────────────────────────────────────

    def test_all_filters_pass(self):
        self.assertTrue(self.matches(
            self._listing(city="amsterdam", price="€ 1800 /mnd", rooms="3", student=False),
            self._query(cities=["amsterdam"], min_price=1500, max_price=2000,
                        min_rooms=2, student=0),
        ))

    def test_city_fails_rest_pass(self):
        self.assertFalse(self.matches(
            self._listing(city="rotterdam", price="€ 1800 /mnd", rooms="3", student=False),
            self._query(cities=["amsterdam"], min_price=1500, max_price=2000, min_rooms=2),
        ))

    def test_price_fails_rest_pass(self):
        self.assertFalse(self.matches(
            self._listing(city="amsterdam", price="€ 3500 /mnd", rooms="3", student=False),
            self._query(cities=["amsterdam"], min_price=1500, max_price=2000, min_rooms=2),
        ))

    def test_rooms_fails_rest_pass(self):
        self.assertFalse(self.matches(
            self._listing(city="amsterdam", price="€ 1800 /mnd", rooms="1", student=False),
            self._query(cities=["amsterdam"], min_price=1500, max_price=2000, min_rooms=2),
        ))

    def test_student_fails_rest_pass(self):
        self.assertFalse(self.matches(
            self._listing(city="amsterdam", price="€ 1800 /mnd", rooms="3", student=True),
            self._query(cities=["amsterdam"], min_price=1500, max_price=2000,
                        min_rooms=2, student=0),
        ))

    def test_student_query_all_filters_pass_including_student_listing(self):
        self.assertTrue(self.matches(
            self._listing(city="amsterdam", price="€ 700 /mnd", rooms="1", student=True),
            self._query(cities=["amsterdam"], min_price=500, max_price=900,
                        min_rooms=1, student=1),
        ))


# ══════════════════════════════════════════════════════════════════════════════
# Scanner — student LLM classification
# ══════════════════════════════════════════════════════════════════════════════

class TestClassifyStudentListing(unittest.TestCase):

    def setUp(self):
        import scanner
        self.classify = scanner.classify_student_listing

    def _mock_anthropic(self, answer: str):
        client = MagicMock()
        client.messages.create.return_value.content = [MagicMock(text=answer)]
        return client

    @patch("scanner.anthropic.Anthropic")
    def test_yes_returns_true(self, MockAnthropic):
        MockAnthropic.return_value = self._mock_anthropic("YES")
        self.assertTrue(self.classify("Alleen voor studenten", "key"))

    @patch("scanner.anthropic.Anthropic")
    def test_no_returns_false(self, MockAnthropic):
        MockAnthropic.return_value = self._mock_anthropic("NO")
        self.assertFalse(self.classify("Ruime woning in Amsterdam", "key"))

    @patch("scanner.anthropic.Anthropic")
    def test_yes_lowercase_accepted(self, MockAnthropic):
        MockAnthropic.return_value = self._mock_anthropic("yes")
        self.assertTrue(self.classify("student only housing", "key"))

    @patch("scanner.anthropic.Anthropic")
    def test_yes_with_trailing_text(self, MockAnthropic):
        MockAnthropic.return_value = self._mock_anthropic("YES.")
        self.assertTrue(self.classify("studentenwoning vereist inschrijfbewijs", "key"))

    def test_empty_text_returns_false_without_api_call(self):
        self.assertFalse(self.classify("", "key"))

    def test_whitespace_only_returns_false_without_api_call(self):
        self.assertFalse(self.classify("   \n\t  ", "key"))

    def test_no_api_key_returns_false(self):
        self.assertFalse(self.classify("alleen voor studenten", ""))

    @patch("scanner.anthropic.Anthropic")
    def test_api_error_returns_false(self, MockAnthropic):
        MockAnthropic.return_value.messages.create.side_effect = Exception("timeout")
        self.assertFalse(self.classify("some text", "key"))

    @patch("scanner.anthropic.Anthropic")
    def test_uses_haiku_model(self, MockAnthropic):
        mock_client = self._mock_anthropic("NO")
        MockAnthropic.return_value = mock_client
        self.classify("some text", "key")
        kwargs = mock_client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-4-5-20251001")

    @patch("scanner.anthropic.Anthropic")
    def test_text_appears_in_prompt(self, MockAnthropic):
        mock_client = self._mock_anthropic("NO")
        MockAnthropic.return_value = mock_client
        self.classify("studentenwoning centrum", "key")
        prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("studentenwoning centrum", prompt)

    @patch("scanner.anthropic.Anthropic")
    def test_long_text_truncated_in_prompt(self, MockAnthropic):
        mock_client = self._mock_anthropic("NO")
        MockAnthropic.return_value = mock_client
        long_text = "x" * 5000
        self.classify(long_text, "key")
        prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        # Prompt must be bounded even for very long input
        self.assertLess(len(prompt), 6000)


# ══════════════════════════════════════════════════════════════════════════════
# Scanner — description fetching
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchListingDescription(unittest.TestCase):

    def setUp(self):
        import scanner
        self.fetch = scanner.fetch_listing_description

    @patch("scanner.requests.get")
    def test_pararius_description_extracted(self, mock_get):
        html = ('<html><body>'
                '<div class="listing-detail-description">'
                'Exclusief voor studenten. Bewijs van inschrijving vereist.'
                '</div></body></html>')
        mock_get.return_value = MagicMock(status_code=200, text=html)
        result = self.fetch("https://www.pararius.nl/huurwoning/amsterdam/abc12345/x", "Pararius")
        self.assertIn("studenten", result)

    @patch("scanner.requests.get")
    def test_pararius_additional_description_selector(self, mock_get):
        html = ('<html><body>'
                '<div class="listing-detail-description__additional">'
                'Student only, proof of enrollment required.'
                '</div></body></html>')
        mock_get.return_value = MagicMock(status_code=200, text=html)
        result = self.fetch("https://www.pararius.nl/x", "Pararius")
        self.assertIn("Student only", result)

    @patch("scanner.requests.get")
    def test_funda_description_extracted(self, mock_get):
        html = ('<html><body>'
                '<div class="object-description-body">'
                'Ruime woning in het centrum. Ideaal voor gezinnen.'
                '</div></body></html>')
        mock_get.return_value = MagicMock(status_code=200, text=html)
        result = self.fetch("https://www.funda.nl/detail/huur/amsterdam/x-1234567/", "Funda")
        self.assertIn("woning", result)

    @patch("scanner.requests.get")
    def test_non_200_returns_empty(self, mock_get):
        mock_get.return_value = MagicMock(status_code=403, text="Forbidden")
        self.assertEqual(self.fetch("https://example.com", "Pararius"), "")

    @patch("scanner.requests.get")
    def test_connection_error_returns_empty(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")
        self.assertEqual(self.fetch("https://example.com", "Pararius"), "")

    @patch("scanner.requests.get")
    def test_description_capped_at_3000_chars(self, mock_get):
        long = "a" * 5000
        html = f'<html><body><div class="listing-detail-description">{long}</div></body></html>'
        mock_get.return_value = MagicMock(status_code=200, text=html)
        result = self.fetch("https://example.com", "Pararius")
        self.assertLessEqual(len(result), 3000)

    @patch("scanner.requests.get")
    def test_missing_description_div_returns_empty(self, mock_get):
        html = "<html><body><p>No matching div here</p></body></html>"
        mock_get.return_value = MagicMock(status_code=200, text=html)
        self.assertEqual(self.fetch("https://example.com", "Pararius"), "")


# ══════════════════════════════════════════════════════════════════════════════
# Scanner — enrich_with_student_flag
# ══════════════════════════════════════════════════════════════════════════════

class TestEnrichWithStudentFlag(unittest.TestCase):

    def setUp(self):
        import scanner
        self.enrich = scanner.enrich_with_student_flag

    def _listing(self, lid="funda-1234567") -> dict:
        return {"id": lid, "source": "Funda", "url": "https://x.com", "title": "T"}

    @patch("scanner.classify_student_listing", return_value=True)
    @patch("scanner.fetch_listing_description", return_value="Alleen voor studenten")
    def test_student_listing_flagged(self, _fetch, _classify):
        listings = [self._listing()]
        self.enrich(listings, "key")
        self.assertTrue(listings[0]["student"])

    @patch("scanner.classify_student_listing", return_value=False)
    @patch("scanner.fetch_listing_description", return_value="Ruime woning")
    def test_normal_listing_not_flagged(self, _fetch, _classify):
        listings = [self._listing()]
        self.enrich(listings, "key")
        self.assertFalse(listings[0]["student"])

    def test_no_api_key_all_default_false(self):
        listings = [self._listing()]
        self.enrich(listings, "")
        self.assertFalse(listings[0]["student"])

    def test_no_api_key_still_sets_student_key(self):
        listings = [self._listing()]
        self.enrich(listings, "")
        self.assertIn("student", listings[0])

    @patch("scanner.classify_student_listing", return_value=False)
    @patch("scanner.fetch_listing_description", return_value="")
    def test_all_listings_processed(self, mock_fetch, mock_classify):
        listings = [self._listing(f"funda-{i}000000") for i in range(1, 4)]
        self.enrich(listings, "key")
        self.assertEqual(mock_fetch.call_count, 3)
        self.assertEqual(mock_classify.call_count, 3)

    @patch("scanner.classify_student_listing", return_value=False)
    @patch("scanner.fetch_listing_description", return_value="")
    def test_fetch_called_with_correct_url_and_source(self, mock_fetch, _classify):
        listing = {"id": "pararius-abc123", "source": "Pararius",
                   "url": "https://www.pararius.nl/x", "title": "T"}
        self.enrich([listing], "key")
        mock_fetch.assert_called_once_with("https://www.pararius.nl/x", "Pararius")

    @patch("scanner.classify_student_listing", return_value=False)
    @patch("scanner.fetch_listing_description", return_value="desc")
    def test_classify_called_with_description(self, _fetch, mock_classify):
        _fetch.return_value = "some description text"
        listings = [self._listing()]
        self.enrich(listings, "test-key")
        mock_classify.assert_called_once_with("some description text", "test-key")


# ══════════════════════════════════════════════════════════════════════════════
# Scanner — free-text filter
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckFreeTextFilter(unittest.TestCase):

    def setUp(self):
        import scanner
        self.check = scanner.check_free_text_filter

    def _mock_client(self, answer):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=answer)]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        return mock_client

    @patch("scanner.anthropic.Anthropic")
    def test_returns_false_when_listing_violates_filter(self, MockAnthropic):
        MockAnthropic.return_value = self._mock_client("YES")
        result = self.check("Corner house listing", "No corner houses", "key")
        self.assertFalse(result)

    @patch("scanner.anthropic.Anthropic")
    def test_returns_true_when_listing_acceptable(self, MockAnthropic):
        MockAnthropic.return_value = self._mock_client("NO")
        result = self.check("City centre apartment", "No corner houses", "key")
        self.assertTrue(result)

    def test_empty_filter_always_includes(self):
        result = self.check("any description", "", "key")
        self.assertTrue(result)

    def test_no_api_key_always_includes(self):
        result = self.check("any description", "no corner houses", "")
        self.assertTrue(result)

    def test_empty_description_always_includes(self):
        result = self.check("", "no corner houses", "key")
        self.assertTrue(result)

    @patch("scanner.anthropic.Anthropic")
    def test_api_error_includes_listing(self, MockAnthropic):
        MockAnthropic.return_value.messages.create.side_effect = Exception("API error")
        result = self.check("some description", "no corner houses", "key")
        self.assertTrue(result)


class TestMergeFreeTextFilter(unittest.TestCase):

    def setUp(self):
        import scanner
        self.merge = scanner.merge_free_text_filter

    def _mock_client(self, answer):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=answer)]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        return mock_client

    @patch("scanner.anthropic.Anthropic")
    def test_merges_with_existing_filter(self, MockAnthropic):
        MockAnthropic.return_value = self._mock_client("No corner houses. No ground floor.")
        result = self.merge("No corner houses", "also no ground floor", "key")
        self.assertEqual(result, "No corner houses. No ground floor.")

    @patch("scanner.anthropic.Anthropic")
    def test_sets_filter_when_no_existing(self, MockAnthropic):
        MockAnthropic.return_value = self._mock_client("No corner houses.")
        result = self.merge("", "no corner houses", "key")
        self.assertEqual(result, "No corner houses.")

    def test_no_api_key_concatenates(self):
        result = self.merge("No corner houses", "also no ground floor", "")
        self.assertIn("No corner houses", result)
        self.assertIn("also no ground floor", result)

    @patch("scanner.anthropic.Anthropic")
    def test_api_error_concatenates(self, MockAnthropic):
        MockAnthropic.return_value.messages.create.side_effect = Exception("fail")
        result = self.merge("existing", "new instruction", "key")
        self.assertIn("existing", result)
        self.assertIn("new instruction", result)


class TestNotifySubscribersWithFreeTextFilter(unittest.TestCase):
    """notify_subscribers applies the free-text filter via LLM."""

    def setUp(self):
        import tempfile, db as db_module, scanner
        self.db_path = tempfile.mktemp(suffix=".db")
        self.db_patcher = patch("db.DB_FILE", self.db_path)
        self.db_patcher.start()
        db_module.init_db()
        self.db = db_module
        self.scanner = scanner

    def tearDown(self):
        self.db_patcher.stop()
        import os
        try: os.unlink(self.db_path)
        except: pass

    @patch("scanner.send_whatsapp_group")
    @patch("scanner.send_email")
    @patch("scanner.check_free_text_filter")
    def test_free_text_filter_excludes_listings(self, mock_check, mock_email, mock_wa):
        mock_check.return_value = False  # all listings excluded by filter
        sub_id = self.db.add_subscriber("a@example.com", "A", "B")
        q_id = self.db.add_customer_query(
            sub_id, "TestCustomer", ["amsterdam"],
            None, None, None, False, "no corner houses"
        )
        listing = {
            "id": "x1", "source": "Pararius", "city": "amsterdam",
            "price": "€ 1500 per maand", "price_num": 1500.0,
            "rooms": "2", "rooms_num": 2, "student": 0,
            "title": "Test House", "url": "http://example.com", "_description": "corner house",
        }
        self.scanner.notify_subscribers([listing], {}, api_key="key")
        mock_email.assert_not_called()

    @patch("scanner.send_whatsapp_group")
    @patch("scanner.send_email")
    @patch("scanner.check_free_text_filter")
    def test_no_filter_skips_llm_check(self, mock_check, mock_email, mock_wa):
        sub_id = self.db.add_subscriber("b@example.com", "B", "C")
        self.db.add_customer_query(sub_id, "TestCustomer", ["amsterdam"], None, None, None)
        listing = {
            "id": "x2", "source": "Pararius", "city": "amsterdam",
            "price": "€ 1500 per maand", "price_num": 1500.0,
            "rooms": "2", "rooms_num": 2, "student": 0,
            "title": "Test", "url": "http://example.com",
        }
        self.scanner.notify_subscribers([listing], {}, api_key="key")
        mock_check.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# DB — free_text_filter column
# ══════════════════════════════════════════════════════════════════════════════

class TestDBFreeTextFilter(unittest.TestCase):

    def setUp(self):
        import tempfile, db as db_module
        self.db_path = tempfile.mktemp(suffix=".db")
        self.db_patcher = patch("db.DB_FILE", self.db_path)
        self.db_patcher.start()
        db_module.init_db()
        self.db = db_module

    def tearDown(self):
        self.db_patcher.stop()
        import os
        try: os.unlink(self.db_path)
        except: pass

    def test_add_query_with_filter(self):
        sub_id = self.db.add_subscriber("t@example.com", "T", "T")
        self.db.add_customer_query(
            sub_id, "Alice", ["amsterdam"], None, None, None,
            False, "no corner houses"
        )
        subs = self.db.get_subscribers_with_queries()
        q = subs[0]["queries"][0]
        self.assertEqual(q["free_text_filter"], "no corner houses")

    def test_add_query_default_filter_empty(self):
        sub_id = self.db.add_subscriber("t2@example.com", "T", "T")
        self.db.add_customer_query(sub_id, "Bob", ["amsterdam"], None, None, None)
        subs = self.db.get_subscribers_with_queries()
        q = subs[0]["queries"][0]
        self.assertEqual(q["free_text_filter"], "")

    def test_update_query_filter(self):
        sub_id = self.db.add_subscriber("t3@example.com", "T", "T")
        q_id = self.db.add_customer_query(sub_id, "Carol", ["amsterdam"], None, None, None)
        self.db.update_query_filter(q_id, "no ground floor apartments")
        subs = self.db.get_subscribers_with_queries()
        q = subs[0]["queries"][0]
        self.assertEqual(q["free_text_filter"], "no ground floor apartments")

    def test_update_query_filter_clear(self):
        sub_id = self.db.add_subscriber("t4@example.com", "T", "T")
        q_id = self.db.add_customer_query(sub_id, "Dave", ["amsterdam"], None, None, None,
                                          False, "some filter")
        self.db.update_query_filter(q_id, "")
        subs = self.db.get_subscribers_with_queries()
        self.assertEqual(subs[0]["queries"][0]["free_text_filter"], "")

    def test_update_customer_query_all_fields(self):
        sub_id = self.db.add_subscriber("t5@example.com", "T", "T")
        q_id = self.db.add_customer_query(sub_id, "Eve", ["amsterdam"], 1000, 2000, 2)
        self.db.update_customer_query(
            q_id, "Eve Updated", ["rotterdam", "den haag"],
            1500, 2500, 3, True, "no ground floor"
        )
        subs = self.db.get_subscribers_with_queries()
        q = subs[0]["queries"][0]
        self.assertEqual(q["customer_name"], "Eve Updated")
        self.assertIn("rotterdam", q["cities"])
        self.assertIn("den haag", q["cities"])
        self.assertEqual(q["min_price"], 1500)
        self.assertEqual(q["max_price"], 2500)
        self.assertEqual(q["min_rooms"], 3)
        self.assertEqual(q["student"], 1)
        self.assertEqual(q["free_text_filter"], "no ground floor")

    def test_update_customer_query_clears_filter(self):
        sub_id = self.db.add_subscriber("t6@example.com", "T", "T")
        q_id = self.db.add_customer_query(sub_id, "Frank", ["amsterdam"], None, None, None,
                                          False, "old filter")
        self.db.update_customer_query(q_id, "Frank", ["amsterdam"], None, None, None, False, "")
        subs = self.db.get_subscribers_with_queries()
        self.assertEqual(subs[0]["queries"][0]["free_text_filter"], "")


# ══════════════════════════════════════════════════════════════════════════════
# Scanner — WhatsApp group notifications
# ══════════════════════════════════════════════════════════════════════════════

class TestSendWhatsAppGroup(unittest.TestCase):
    """send_whatsapp_group calls the local Baileys microservice (POST /send)."""

    def setUp(self):
        import scanner
        self.send = scanner.send_whatsapp_group

    def _cfg(self, **kw):
        base = {
            "whatsapp_service_url": "http://localhost:3001",
            "whatsapp_service_token": "testtoken",
        }
        base.update(kw)
        return base

    @patch("scanner.requests.post")
    def test_posts_to_service_send_endpoint(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        self.send("120363043051405349@g.us", "Hello group!", self._cfg())
        mock_post.assert_called_once()
        url = mock_post.call_args.args[0]
        self.assertEqual(url, "http://localhost:3001/send")

    @patch("scanner.requests.post")
    def test_sends_correct_chat_id_and_message(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        self.send("120363043051405349@g.us", "Test message", self._cfg())
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["chatId"], "120363043051405349@g.us")
        self.assertEqual(payload["message"], "Test message")

    @patch("scanner.requests.post")
    def test_bearer_token_sent_in_header(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        self.send("120363043051405349@g.us", "msg", self._cfg())
        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer testtoken")

    @patch("scanner.requests.post")
    def test_no_auth_header_when_token_empty(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        self.send("120363043051405349@g.us", "msg", self._cfg(whatsapp_service_token=""))
        headers = mock_post.call_args.kwargs.get("headers", {})
        self.assertNotIn("Authorization", headers)

    @patch("scanner.requests.post")
    def test_empty_group_id_skips(self, mock_post):
        self.send("", "msg", self._cfg())
        mock_post.assert_not_called()

    @patch("scanner.requests.post")
    def test_non_200_response_logs_warning(self, mock_post):
        mock_post.return_value = MagicMock(status_code=503, text="Not connected")
        # Should not raise, just log
        self.send("120363043051405349@g.us", "msg", self._cfg())

    @patch("scanner.requests.post")
    def test_request_exception_does_not_raise(self, mock_post):
        mock_post.side_effect = Exception("Connection refused")
        self.send("120363043051405349@g.us", "msg", self._cfg())


class TestFormatWhatsAppMessage(unittest.TestCase):

    def setUp(self):
        import scanner
        self.fmt = scanner._format_whatsapp_message

    def _query(self, **kw):
        base = dict(customer_name="John", cities=["amsterdam"],
                    min_price=1500, max_price=2500, min_rooms=2, student=0)
        base.update(kw)
        return base

    def _listing(self, **kw):
        base = dict(source="Pararius", title="Keizersgracht 123",
                    price="€ 2.000 per maand", size="75 m²",
                    rooms="3", url="https://www.pararius.nl/x", student=False)
        base.update(kw)
        return base

    def test_contains_customer_name(self):
        msg = self.fmt(self._query(), [self._listing()])
        self.assertIn("John", msg)

    def test_contains_listing_title(self):
        msg = self.fmt(self._query(), [self._listing()])
        self.assertIn("Keizersgracht 123", msg)

    def test_contains_listing_url(self):
        msg = self.fmt(self._query(), [self._listing()])
        self.assertIn("https://www.pararius.nl/x", msg)

    def test_contains_source(self):
        msg = self.fmt(self._query(), [self._listing()])
        self.assertIn("Pararius", msg)

    def test_contains_price_filter(self):
        msg = self.fmt(self._query(), [self._listing()])
        self.assertIn("1500", msg)
        self.assertIn("2500", msg)

    def test_contains_city(self):
        msg = self.fmt(self._query(), [self._listing()])
        self.assertIn("Amsterdam", msg)

    def test_count_singular(self):
        msg = self.fmt(self._query(), [self._listing()])
        self.assertIn("1 new rental", msg)

    def test_count_plural(self):
        msg = self.fmt(self._query(), [self._listing(), self._listing(title="Prinsengracht 5")])
        self.assertIn("2 new rentals", msg)

    def test_student_filter_shown_when_set(self):
        msg = self.fmt(self._query(student=1), [self._listing()])
        self.assertIn("student", msg.lower())

    def test_multiple_listings_all_present(self):
        listings = [self._listing(title=f"Street {i}", url=f"https://x.com/{i}") for i in range(3)]
        msg = self.fmt(self._query(), listings)
        for i in range(3):
            self.assertIn(f"Street {i}", msg)
            self.assertIn(f"https://x.com/{i}", msg)

    def test_no_rooms_filter_not_shown(self):
        msg = self.fmt(self._query(min_rooms=None), [self._listing()])
        self.assertNotIn("+ rooms", msg)

    def test_funda_source(self):
        msg = self.fmt(self._query(), [self._listing(source="Funda")])
        self.assertIn("Funda", msg)


# ══════════════════════════════════════════════════════════════════════════════
# Scraper — Pararius HTML parsing
# ══════════════════════════════════════════════════════════════════════════════

PARARIUS_HTML = """
<html><body><ul>
  <li class="search-list__item--listing">
    <a class="listing-search-item__link--title"
       href="/huurwoning/amsterdam/aabbccdd/mooie-straat-10"
       aria-label="Mooie Straat 10, Amsterdam">Mooie Straat 10</a>
    <div class="listing-search-item__price">€ 1.750 per maand</div>
    <ul class="illustrated-features">
      <li class="illustrated-features__item illustrated-features__item--surface-area">75 m²</li>
      <li class="illustrated-features__item illustrated-features__item--number-of-rooms">3 kamers</li>
      <li class="illustrated-features__item illustrated-features__item--energy-label">A</li>
    </ul>
    <span class="listing-search-item__broker-name">Makelaardij Centrum</span>
  </li>
  <li class="search-list__item--listing">
    <a class="listing-search-item__link--title"
       href="/huurwoning/amsterdam/11223344/tweede-straat-5"
       aria-label="Tweede Straat 5, Amsterdam">Tweede Straat 5</a>
    <div class="listing-search-item__price">€ 2.200 per maand</div>
    <ul class="illustrated-features">
      <li class="illustrated-features__item illustrated-features__item--surface-area">90 m²</li>
      <li class="illustrated-features__item illustrated-features__item--number-of-rooms">4 kamers</li>
      <li class="illustrated-features__item illustrated-features__item--energy-label">B</li>
    </ul>
  </li>
</ul></body></html>
"""


class TestParariusScraper(unittest.TestCase):

    def _page(self, html: str) -> MagicMock:
        page = MagicMock()
        page.content.return_value = html
        return page

    def _scrape(self, html: str = PARARIUS_HTML, city: str = "amsterdam") -> list:
        import scanner
        return scanner.scrape_pararius(city, self._page(html))

    def test_returns_two_listings(self):
        self.assertEqual(len(self._scrape()), 2)

    def test_id_format(self):
        results = self._scrape()
        self.assertEqual(results[0]["id"], "pararius-aabbccdd")
        self.assertEqual(results[1]["id"], "pararius-11223344")

    def test_title_from_aria_label(self):
        self.assertIn("Mooie Straat 10", self._scrape()[0]["title"])

    def test_price(self):
        self.assertIn("1.750", self._scrape()[0]["price"])

    def test_size(self):
        self.assertEqual(self._scrape()[0]["size"], "75 m²")

    def test_rooms(self):
        self.assertEqual(self._scrape()[0]["rooms"], "3")

    def test_energy(self):
        self.assertEqual(self._scrape()[0]["energy"], "A")

    def test_agency_present(self):
        self.assertEqual(self._scrape()[0]["agency"], "Makelaardij Centrum")

    def test_agency_absent_is_empty_string(self):
        self.assertEqual(self._scrape()[1]["agency"], "")

    def test_url_is_absolute(self):
        url = self._scrape()[0]["url"]
        self.assertTrue(url.startswith("https://www.pararius.nl"))

    def test_source_is_pararius(self):
        self.assertEqual(self._scrape()[0]["source"], "Pararius")

    def test_city_set_correctly(self):
        self.assertEqual(self._scrape(city="utrecht")[0]["city"], "utrecht")

    def test_empty_page_returns_empty_list(self):
        self.assertEqual(self._scrape("<html><body><ul></ul></body></html>"), [])

    def test_timeout_returns_empty_list(self):
        import scanner
        from playwright.sync_api import TimeoutError as PwTimeoutError
        page = MagicMock()
        page.goto.side_effect = PwTimeoutError("timeout")
        self.assertEqual(scanner.scrape_pararius("amsterdam", page), [])

    def test_details_string_contains_size_and_rooms(self):
        details = self._scrape()[0]["details"]
        self.assertIn("75 m²", details)
        self.assertIn("3 bedrooms", details)

    def test_details_string_contains_agency(self):
        details = self._scrape()[0]["details"]
        self.assertIn("Makelaardij Centrum", details)

    def test_second_listing_price(self):
        self.assertIn("2.200", self._scrape()[1]["price"])

    def test_second_listing_energy(self):
        self.assertEqual(self._scrape()[1]["energy"], "B")


# ══════════════════════════════════════════════════════════════════════════════
# Scraper — Funda HTML parsing
# ══════════════════════════════════════════════════════════════════════════════

FUNDA_HTML = """
<html><body><ul>
  <li>
    <a href="/detail/huur/amsterdam/test-straat/12345678/">
      <p>Test Straat 1, Amsterdam</p>
    </a>
    <span>€ 2.100 /mnd</span>
    <span>85 m²</span>
    <span>3 kamers</span>
    <a href="/makelaar/test-makelaardij-amsterdam/">Test Makelaardij</a>
  </li>
  <li>
    <a href="/detail/huur/amsterdam/andere-straat/99887766/">
      <p>Andere Straat 9</p>
    </a>
    <span>€ 1.800 /mnd</span>
    <span>65 m²</span>
    <span>2 kamers</span>
  </li>
</ul></body></html>
"""


class TestFundaScraper(unittest.TestCase):

    def _driver(self, html: str) -> MagicMock:
        driver = MagicMock()
        driver.page_source = html
        return driver

    def _scrape(self, html: str = FUNDA_HTML, cities: list = None):
        import scanner
        cities = cities or ["amsterdam"]
        with patch("scanner.uc.Chrome") as MockChrome, \
             patch("scanner.WebDriverWait") as MockWait, \
             patch("scanner._chrome_major_version", return_value=0), \
             patch("scanner.time.sleep"):
            MockChrome.return_value = self._driver(html)
            MockWait.return_value.until.return_value = True
            return scanner.scrape_funda_all_cities(cities)

    def test_returns_two_listings(self):
        self.assertEqual(len(self._scrape()), 2)

    def test_id_format(self):
        results = self._scrape()
        self.assertEqual(results[0]["id"], "funda-12345678")

    def test_second_listing_id(self):
        results = self._scrape()
        self.assertEqual(results[1]["id"], "funda-99887766")

    def test_price_parsed(self):
        self.assertIn("2.100", self._scrape()[0]["price"])

    def test_size_parsed(self):
        self.assertEqual(self._scrape()[0]["size"], "85 m²")

    def test_rooms_parsed(self):
        self.assertEqual(self._scrape()[0]["rooms"], "3")

    def test_agency_parsed(self):
        self.assertEqual(self._scrape()[0]["agency"], "Test Makelaardij")

    def test_no_agency_is_empty_string(self):
        self.assertEqual(self._scrape()[1]["agency"], "")

    def test_source_is_funda(self):
        self.assertEqual(self._scrape()[0]["source"], "Funda")

    def test_city_set_correctly(self):
        self.assertEqual(self._scrape()[0]["city"], "amsterdam")

    def test_url_is_absolute(self):
        url = self._scrape()[0]["url"]
        self.assertTrue(url.startswith("https://www.funda.nl"))

    def test_deduplication_prevents_duplicate_ids(self):
        dup_html = FUNDA_HTML.replace(
            "</ul>",
            '<li><a href="/detail/huur/amsterdam/test-straat/12345678/"></a></li></ul>',
        )
        results = self._scrape(dup_html)
        ids = [r["id"] for r in results]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate listing IDs found")

    def test_timeout_skips_city_returns_empty(self):
        import scanner
        from selenium.common.exceptions import TimeoutException
        with patch("scanner.uc.Chrome") as MockChrome, \
             patch("scanner.WebDriverWait") as MockWait, \
             patch("scanner._chrome_major_version", return_value=0), \
             patch("scanner.time.sleep"):
            MockChrome.return_value = self._driver("")
            MockWait.return_value.until.side_effect = TimeoutException()
            results = scanner.scrape_funda_all_cities(["amsterdam"])
        self.assertEqual(results, [])

    def test_driver_quit_called_on_success(self):
        import scanner
        driver = self._driver(FUNDA_HTML)
        with patch("scanner.uc.Chrome", return_value=driver), \
             patch("scanner.WebDriverWait") as MockWait, \
             patch("scanner._chrome_major_version", return_value=0), \
             patch("scanner.time.sleep"):
            MockWait.return_value.until.return_value = True
            scanner.scrape_funda_all_cities(["amsterdam"])
        driver.quit.assert_called_once()

    def test_driver_quit_called_on_exception(self):
        import scanner
        driver = self._driver("")
        with patch("scanner.uc.Chrome", return_value=driver), \
             patch("scanner.WebDriverWait") as MockWait, \
             patch("scanner._chrome_major_version", return_value=0), \
             patch("scanner.time.sleep"):
            MockWait.return_value.until.side_effect = Exception("crash")
            scanner.scrape_funda_all_cities(["amsterdam"])
        driver.quit.assert_called_once()

    def test_multiple_cities_scrape_all(self):
        import scanner
        driver = self._driver(FUNDA_HTML)
        with patch("scanner.uc.Chrome", return_value=driver), \
             patch("scanner.WebDriverWait") as MockWait, \
             patch("scanner._chrome_major_version", return_value=0), \
             patch("scanner.time.sleep"):
            MockWait.return_value.until.return_value = True
            results = scanner.scrape_funda_all_cities(["amsterdam", "utrecht"])
        # 2 cities × 2 listings each = 4 (city field differs so no dedup)
        self.assertEqual(len(results), 4)


# ══════════════════════════════════════════════════════════════════════════════
# Flask UI — route tests
# ══════════════════════════════════════════════════════════════════════════════

class TestFlaskRoutes(unittest.TestCase):

    def setUp(self):
        self.db_path = _tmp_db()
        self.db_patcher = patch("db.DB_FILE", self.db_path)
        self.db_patcher.start()
        import db
        db.init_db()
        self.db = db

        import app as flask_app
        flask_app.app.config["TESTING"] = True
        flask_app.app.config["WTF_CSRF_ENABLED"] = False
        self.client = flask_app.app.test_client()

    def tearDown(self):
        self.db_patcher.stop()
        os.unlink(self.db_path)

    # static pages ─────────────────────────────────────────────────────────────

    def test_index_returns_200(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_subscribers_page_returns_200(self):
        self.assertEqual(self.client.get("/subscribers").status_code, 200)

    def test_scanner_status_returns_json(self):
        r = self.client.get("/scanner/status")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn("running", data)
        self.assertIn("next_run_at", data)

    # subscriber management ────────────────────────────────────────────────────

    def test_add_subscriber_creates_record(self):
        self.client.post("/subscribers/add", data={
            "email": "new@example.com", "first_name": "New", "last_name": "User",
        }, follow_redirects=True)
        subs = self.db.get_subscribers_with_queries()
        emails = [s["email"] for s in subs]
        self.assertIn("new@example.com", emails)

    def test_add_subscriber_redirects(self):
        r = self.client.post("/subscribers/add", data={
            "email": "r@example.com", "first_name": "R", "last_name": "Test",
        })
        self.assertEqual(r.status_code, 302)

    def test_add_subscriber_shows_flash_name(self):
        r = self.client.post("/subscribers/add", data={
            "email": "f@example.com", "first_name": "Flash", "last_name": "Gordon",
        }, follow_redirects=True)
        self.assertIn(b"Flash Gordon", r.data)

    def test_remove_subscriber_soft_deletes(self):
        sub_id = self.db.add_subscriber("del@example.com", "Del", "Me")
        self.client.post(f"/subscribers/remove/{sub_id}", follow_redirects=True)
        emails = [s["email"] for s in self.db.get_subscribers_with_queries()]
        self.assertNotIn("del@example.com", emails)

    # query management ─────────────────────────────────────────────────────────

    def _sub_queries(self, sub_id: int) -> list:
        subs = self.db.get_subscribers_with_queries()
        sub = next(s for s in subs if s["id"] == sub_id)
        return sub["queries"]

    def test_add_query_without_student_defaults_to_0(self):
        sub_id = self.db.add_subscriber("q@example.com", "Q", "Test")
        self.client.post(f"/subscribers/{sub_id}/queries/add", data={
            "customer_name": "John", "cities": ["amsterdam"],
            "min_price": "1000", "max_price": "2000", "min_rooms": "2",
        }, follow_redirects=True)
        q = self._sub_queries(sub_id)[0]
        self.assertEqual(q["customer_name"], "John")
        self.assertEqual(q["student"], 0)

    def test_add_query_with_student_checkbox_sets_1(self):
        sub_id = self.db.add_subscriber("s@example.com", "S", "Test")
        self.client.post(f"/subscribers/{sub_id}/queries/add", data={
            "customer_name": "Student Maria", "cities": ["amsterdam"], "student": "1",
        }, follow_redirects=True)
        self.assertEqual(self._sub_queries(sub_id)[0]["student"], 1)

    def test_add_query_student_unchecked_means_0(self):
        """When the checkbox is not ticked, 'student' is absent from POST data."""
        sub_id = self.db.add_subscriber("ns@example.com", "NS", "Test")
        self.client.post(f"/subscribers/{sub_id}/queries/add", data={
            "customer_name": "Normal Bob", "cities": ["amsterdam"],
            # 'student' key intentionally omitted
        }, follow_redirects=True)
        self.assertEqual(self._sub_queries(sub_id)[0]["student"], 0)

    def test_remove_query_soft_deletes(self):
        sub_id = self.db.add_subscriber("rq@example.com", "R", "Q")
        qid = self.db.add_customer_query(sub_id, "Remove Me", ["amsterdam"], None, None, None)
        self.client.post(f"/queries/remove/{qid}", follow_redirects=True)
        self.assertEqual(len(self.db.get_subscribers_with_queries()[0]["queries"]), 0)

    def test_add_query_flash_message(self):
        sub_id = self.db.add_subscriber("fm@example.com", "FM", "Test")
        r = self.client.post(f"/subscribers/{sub_id}/queries/add", data={
            "customer_name": "Flash Query", "cities": ["amsterdam"],
        }, follow_redirects=True)
        self.assertIn(b"Flash Query", r.data)

    # UI content ───────────────────────────────────────────────────────────────

    def test_student_checkbox_present_in_form(self):
        # Need a subscriber so the "Add query" form is rendered
        self.db.add_subscriber("form@example.com", "Form", "Test")
        r = self.client.get("/subscribers")
        self.assertIn(b'name="student"', r.data)

    def test_student_checkbox_label_text(self):
        self.db.add_subscriber("label@example.com", "Label", "Test")
        r = self.client.get("/subscribers")
        self.assertIn(b"Student?", r.data)

    def test_student_pill_shown_for_student_query(self):
        sub_id = self.db.add_subscriber("pill@example.com", "Pill", "Test")
        self.db.add_customer_query(sub_id, "Student Q", ["amsterdam"],
                                   None, None, None, student=True)
        r = self.client.get("/subscribers")
        # The rendered pill has class="pill pill-student" in the HTML body
        self.assertIn(b'class="pill pill-student"', r.data)

    def test_student_pill_not_shown_for_non_student_query(self):
        sub_id = self.db.add_subscriber("nopill@example.com", "No", "Pill")
        self.db.add_customer_query(sub_id, "Normal Q", ["amsterdam"],
                                   None, None, None, student=False)
        r = self.client.get("/subscribers")
        # CSS definition contains "pill-student" — check the rendered class attribute instead
        self.assertNotIn(b'class="pill pill-student"', r.data)

    def test_query_cities_shown_in_ui(self):
        sub_id = self.db.add_subscriber("city@example.com", "City", "Test")
        self.db.add_customer_query(sub_id, "City Q", ["amsterdam", "utrecht"],
                                   None, None, None)
        r = self.client.get("/subscribers")
        self.assertIn(b"Amsterdam", r.data)
        self.assertIn(b"Utrecht", r.data)

    def test_scope_update_returns_200(self):
        r = self.client.post("/scope", data={"cities": ["amsterdam", "rotterdam"]},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)

    def test_run_scanner_endpoint_exists(self):
        # Just check it responds; actual scraping is not triggered in tests
        r = self.client.post("/scanner/run", follow_redirects=False)
        self.assertIn(r.status_code, [200, 302])

    # WhatsApp group ───────────────────────────────────────────────────────────

    def test_set_whatsapp_group_saves_to_db(self):
        sub_id = self.db.add_subscriber("wa@example.com", "WA", "Test")
        self.client.post(f"/subscribers/{sub_id}/whatsapp-group",
                         data={"group_id": "120363043051405349@g.us"},
                         follow_redirects=True)
        subs = self.db.get_subscribers_with_queries()
        sub  = next(s for s in subs if s["id"] == sub_id)
        self.assertEqual(sub["whatsapp_group"], "120363043051405349@g.us")

    def test_clear_whatsapp_group(self):
        sub_id = self.db.add_subscriber("wa2@example.com", "WA2", "Test")
        self.db.set_subscriber_whatsapp_group(sub_id, "120363043051405349@g.us")
        self.client.post(f"/subscribers/{sub_id}/whatsapp-group",
                         data={"group_id": ""},
                         follow_redirects=True)
        subs = self.db.get_subscribers_with_queries()
        sub  = next(s for s in subs if s["id"] == sub_id)
        self.assertEqual(sub["whatsapp_group"], "")

    def test_whatsapp_group_shown_in_ui(self):
        sub_id = self.db.add_subscriber("show@example.com", "Show", "Test")
        self.db.set_subscriber_whatsapp_group(sub_id, "120363043051405349@g.us")
        r = self.client.get("/subscribers")
        self.assertIn(b"120363043051405349@g.us", r.data)

    def test_whatsapp_group_form_in_ui(self):
        self.db.add_subscriber("form@example.com", "Form", "Test")
        r = self.client.get("/subscribers")
        self.assertIn(b"whatsapp-group", r.data)
        self.assertIn(b"group_id", r.data)

    def test_list_whatsapp_groups_proxies_service(self):
        """Route proxies to whatsapp-service GET /groups and returns its JSON."""
        service_cfg = {"notifications": {
            "whatsapp_service_url": "http://localhost:3001",
            "whatsapp_service_token": "",
        }}
        fake_response = MagicMock(status_code=200)
        fake_response.json.return_value = {"groups": [
            {"id": "120363043051405349@g.us", "name": "Test Group", "participants": 2}
        ]}
        with patch("json.load", return_value=service_cfg):
            with patch("builtins.open", mock_open(read_data=json.dumps(service_cfg))):
                with patch("requests.get", return_value=fake_response):
                    r = self.client.get("/whatsapp-groups")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn("groups", data)
        self.assertEqual(data["groups"][0]["id"], "120363043051405349@g.us")

    def test_list_whatsapp_groups_service_unreachable_returns_500(self):
        service_cfg = {"notifications": {
            "whatsapp_service_url": "http://localhost:3001",
            "whatsapp_service_token": "",
        }}
        with patch("json.load", return_value=service_cfg):
            with patch("builtins.open", mock_open(read_data=json.dumps(service_cfg))):
                with patch("requests.get", side_effect=Exception("Connection refused")):
                    r = self.client.get("/whatsapp-groups")
        self.assertEqual(r.status_code, 500)
        data = json.loads(r.data)
        self.assertIn("error", data)


    # ── free-text filter routes ───────────────────────────────────────────────

    def test_add_query_form_has_free_text_filter_field(self):
        self.db.add_subscriber("ftf@example.com", "FTF", "Test")
        r = self.client.get("/subscribers")
        self.assertIn(b"free_text_filter", r.data)

    def test_add_query_saves_free_text_filter(self):
        sub_id = self.db.add_subscriber("ftf2@example.com", "FTF2", "Test")
        self.client.post(f"/subscribers/{sub_id}/queries/add", data={
            "customer_name": "Alice",
            "cities": ["amsterdam"],
            "free_text_filter": "no corner houses",
        })
        subs = self.db.get_subscribers_with_queries()
        sub = next(s for s in subs if s["id"] == sub_id)
        self.assertEqual(sub["queries"][0]["free_text_filter"], "no corner houses")

    def test_update_query_filter_route(self):
        sub_id = self.db.add_subscriber("ftf3@example.com", "FTF3", "Test")
        q_id = self.db.add_customer_query(sub_id, "Bob", ["amsterdam"], None, None, None)
        r = self.client.post(f"/queries/{q_id}/filter", data={"free_text_filter": "no ground floor"})
        self.assertEqual(r.status_code, 302)
        subs = self.db.get_subscribers_with_queries()
        sub = next(s for s in subs if s["id"] == sub_id)
        self.assertEqual(sub["queries"][0]["free_text_filter"], "no ground floor")

    def test_filter_pill_shown_in_ui_when_set(self):
        sub_id = self.db.add_subscriber("ftf4@example.com", "FTF4", "Test")
        self.db.add_customer_query(sub_id, "Carol", ["amsterdam"], None, None, None,
                                   False, "no corner houses")
        r = self.client.get("/subscribers")
        self.assertIn(b"pill-filter", r.data)
        self.assertIn(b"no corner houses", r.data)

    def test_whatsapp_filter_webhook_updates_query(self):
        sub_id = self.db.add_subscriber("ftf5@example.com", "FTF5", "Test")
        self.db.set_subscriber_whatsapp_group(sub_id, "120363407400776027@g.us")
        self.db.add_customer_query(sub_id, "Alice", ["amsterdam"], None, None, None)
        cfg = {"anthropic_api_key": ""}
        with patch("json.load", return_value=cfg):
            with patch("builtins.open", mock_open(read_data=json.dumps(cfg))):
                r = self.client.post("/api/whatsapp-filter", json={
                    "group_id":       "120363407400776027@g.us",
                    "quoted_message": "[Pararius]  Test St 1  —  *Alice*",
                    "filter_text":    "no corner houses",
                })
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIn("acknowledgements", data)
        self.assertEqual(data["acknowledgements"][0]["customer_name"], "Alice")
        # Verify DB updated
        subs = self.db.get_subscribers_with_queries()
        sub = next(s for s in subs if s["id"] == sub_id)
        self.assertIn("corner", sub["queries"][0]["free_text_filter"])

    def test_whatsapp_filter_webhook_missing_filter_text_returns_400(self):
        r = self.client.post("/api/whatsapp-filter", json={
            "group_id": "120363407400776027@g.us",
            "quoted_message": "— *Alice*",
            "filter_text": "",
        })
        self.assertEqual(r.status_code, 400)

    def test_whatsapp_filter_webhook_no_customer_found_returns_400(self):
        r = self.client.post("/api/whatsapp-filter", json={
            "group_id": "120363407400776027@g.us",
            "quoted_message": "A message with no customer name pattern",
            "filter_text": "no corner houses",
        })
        self.assertEqual(r.status_code, 400)

    def test_edit_query_route_updates_all_fields(self):
        sub_id = self.db.add_subscriber("eq1@example.com", "EQ1", "Test")
        q_id = self.db.add_customer_query(sub_id, "Alice", ["amsterdam"], 1000, 2000, 2)
        r = self.client.post(f"/queries/{q_id}/edit", data={
            "customer_name": "Alice Edited",
            "cities": ["rotterdam"],
            "min_price": "1500",
            "max_price": "2500",
            "min_rooms": "3",
            "student": "1",
            "free_text_filter": "no ground floor",
        })
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertTrue(data.get("ok"))
        subs = self.db.get_subscribers_with_queries()
        sub = next(s for s in subs if s["id"] == sub_id)
        q = sub["queries"][0]
        self.assertEqual(q["customer_name"], "Alice Edited")
        self.assertIn("rotterdam", q["cities"])
        self.assertEqual(q["min_price"], 1500.0)
        self.assertEqual(q["student"], 1)
        self.assertEqual(q["free_text_filter"], "no ground floor")

    def test_edit_query_route_missing_name_returns_400(self):
        sub_id = self.db.add_subscriber("eq2@example.com", "EQ2", "Test")
        q_id = self.db.add_customer_query(sub_id, "Bob", ["amsterdam"], None, None, None)
        r = self.client.post(f"/queries/{q_id}/edit", data={
            "customer_name": "",
            "cities": ["amsterdam"],
        })
        self.assertEqual(r.status_code, 400)

    def test_edit_query_shows_edit_button_in_ui(self):
        sub_id = self.db.add_subscriber("eq3@example.com", "EQ3", "Test")
        self.db.add_customer_query(sub_id, "Carol", ["amsterdam"], None, None, None)
        r = self.client.get("/subscribers")
        self.assertIn(b"edit-query-btn", r.data)
        self.assertIn(b"query-edit-", r.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
