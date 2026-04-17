"""
listings.db — SQLite store for all seen rental listings.

Schema
------
listings
  id         TEXT  PRIMARY KEY        e.g. "funda-89761197" / "pararius-abc123"
  source     TEXT                     "Funda" | "Pararius"
  title      TEXT                     full address
  price      TEXT                     display string  e.g. "€ 2.500 per maand"
  price_num  REAL                     numeric euro/month  e.g. 2500.0
  size       TEXT                     e.g. "75 m²"
  size_num   REAL                     e.g. 75.0
  rooms      TEXT                     e.g. "3"
  rooms_num  INTEGER                  e.g. 3
  energy     TEXT                     e.g. "A+++"
  agency     TEXT                     e.g. "Huizenbalie.nl"
  phone      TEXT                     e.g. "0201234567"
  url        TEXT
  first_seen TEXT  ISO-8601 UTC       when first detected
  last_seen  TEXT  ISO-8601 UTC       updated every scan it appears

Agents can query this with plain SQL, e.g.:
  SELECT * FROM listings WHERE source='Funda' ORDER BY first_seen DESC LIMIT 20;
  SELECT * FROM listings WHERE rooms_num >= 2 AND price_num <= 2000;
  SELECT * FROM listings WHERE agency LIKE '%Sotheby%';
"""

import os
import re
import sqlite3
from datetime import datetime, timezone

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "listings.db")


# ── Connection ─────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    return c


# ── Schema ─────────────────────────────────────────────────────────────────────

def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id         TEXT PRIMARY KEY,
                source     TEXT,
                title      TEXT,
                price      TEXT,
                price_num  REAL,
                size       TEXT,
                size_num   REAL,
                rooms      TEXT,
                rooms_num  INTEGER,
                energy     TEXT,
                agency     TEXT,
                phone      TEXT,
                url        TEXT,
                first_seen TEXT,
                last_seen  TEXT,
                student    INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_source     ON listings(source)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_price_num  ON listings(price_num)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_rooms_num  ON listings(rooms_num)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_first_seen ON listings(first_seen)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS scan_runs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at    TEXT NOT NULL,
                new_count INTEGER NOT NULL DEFAULT 0,
                new_ids   TEXT NOT NULL DEFAULT '[]'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                email            TEXT NOT NULL,
                first_name       TEXT NOT NULL DEFAULT '',
                last_name        TEXT NOT NULL DEFAULT '',
                whatsapp_group   TEXT NOT NULL DEFAULT '',
                created_at       TEXT NOT NULL,
                active           INTEGER NOT NULL DEFAULT 1
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sub_email ON subscribers(email)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS customer_queries (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                subscriber_id INTEGER NOT NULL REFERENCES subscribers(id),
                customer_name TEXT NOT NULL DEFAULT '',
                cities        TEXT NOT NULL DEFAULT '[]',
                min_price     REAL,
                max_price     REAL,
                min_rooms     INTEGER,
                student       INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                active        INTEGER NOT NULL DEFAULT 1
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cq_sub ON customer_queries(subscriber_id)")
        # migrations
        sub_cols = {row[1] for row in c.execute("PRAGMA table_info(subscribers)")}
        if "first_name" not in sub_cols:
            c.execute("ALTER TABLE subscribers ADD COLUMN first_name TEXT NOT NULL DEFAULT ''")
        if "last_name" not in sub_cols:
            c.execute("ALTER TABLE subscribers ADD COLUMN last_name TEXT NOT NULL DEFAULT ''")
        if "whatsapp_group" not in sub_cols:
            c.execute("ALTER TABLE subscribers ADD COLUMN whatsapp_group TEXT NOT NULL DEFAULT ''")
        lst_cols = {row[1] for row in c.execute("PRAGMA table_info(listings)")}
        if "city" not in lst_cols:
            c.execute("ALTER TABLE listings ADD COLUMN city TEXT NOT NULL DEFAULT ''")
        if "student" not in lst_cols:
            c.execute("ALTER TABLE listings ADD COLUMN student INTEGER NOT NULL DEFAULT 0")
        cq_cols = {row[1] for row in c.execute("PRAGMA table_info(customer_queries)")}
        if "student" not in cq_cols:
            c.execute("ALTER TABLE customer_queries ADD COLUMN student INTEGER NOT NULL DEFAULT 0")


# ── Read ───────────────────────────────────────────────────────────────────────

def get_seen_ids() -> set:
    """Return the set of all known listing IDs (used for deduplication)."""
    init_db()
    with _conn() as c:
        return {row[0] for row in c.execute("SELECT id FROM listings")}


# ── Write ──────────────────────────────────────────────────────────────────────

def upsert_listings(listings: list):
    """
    Insert new listings; if already present, only update last_seen.
    Each listing dict should have: id, source, title, price, size, rooms,
    energy, agency, phone, url.  Missing fields default to empty string.
    """
    if not listings:
        return
    init_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _conn() as c:
        for l in listings:
            c.execute("""
                INSERT INTO listings
                    (id, source, city, title, price, price_num,
                     size, size_num, rooms, rooms_num,
                     energy, agency, phone, url, first_seen, last_seen, student)
                VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    source    = excluded.source,
                    city      = CASE WHEN excluded.city     != '' THEN excluded.city     ELSE listings.city     END,
                    title     = CASE WHEN excluded.title    != '' THEN excluded.title    ELSE listings.title    END,
                    price     = CASE WHEN excluded.price    != '' THEN excluded.price    ELSE listings.price    END,
                    price_num = CASE WHEN excluded.price_num IS NOT NULL THEN excluded.price_num ELSE listings.price_num END,
                    size      = CASE WHEN excluded.size     != '' THEN excluded.size     ELSE listings.size     END,
                    size_num  = CASE WHEN excluded.size_num  IS NOT NULL THEN excluded.size_num  ELSE listings.size_num  END,
                    rooms     = CASE WHEN excluded.rooms    != '' THEN excluded.rooms    ELSE listings.rooms    END,
                    rooms_num = CASE WHEN excluded.rooms_num IS NOT NULL THEN excluded.rooms_num ELSE listings.rooms_num END,
                    energy    = CASE WHEN excluded.energy   != '' THEN excluded.energy   ELSE listings.energy   END,
                    agency    = CASE WHEN excluded.agency   != '' THEN excluded.agency   ELSE listings.agency   END,
                    phone     = CASE WHEN excluded.phone    != '' THEN excluded.phone    ELSE listings.phone    END,
                    url       = CASE WHEN excluded.url      != '' THEN excluded.url      ELSE listings.url      END,
                    last_seen = excluded.last_seen,
                    student   = CASE WHEN excluded.student = 1 THEN 1 ELSE listings.student END
            """, (
                l.get("id", ""),
                l.get("source", ""),
                l.get("city", ""),
                l.get("title", ""),
                l.get("price", ""),       _parse_price(l.get("price", "")),
                l.get("size", ""),        _parse_size(l.get("size", "")),
                l.get("rooms", ""),       _parse_rooms(l.get("rooms", "")),
                l.get("energy", ""),
                l.get("agency", ""),
                l.get("phone", ""),
                l.get("url", ""),
                now, now,
                1 if l.get("student") else 0,
            ))


def add_subscriber(email: str, first_name: str, last_name: str,
                   whatsapp_group: str = "") -> int:
    init_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO subscribers (email, first_name, last_name, whatsapp_group, created_at) "
            "VALUES (?,?,?,?,?)",
            (email.strip().lower(), first_name.strip(), last_name.strip(),
             whatsapp_group.strip(), now),
        )
        return cur.lastrowid


def set_subscriber_whatsapp_group(subscriber_id: int, group_id: str):
    """Set or clear the WhatsApp group chat ID for a subscriber."""
    init_db()
    with _conn() as c:
        c.execute(
            "UPDATE subscribers SET whatsapp_group=? WHERE id=?",
            (group_id.strip(), subscriber_id),
        )


def add_customer_query(subscriber_id: int, customer_name: str, cities: list,
                       min_price, max_price, min_rooms, student: bool = False) -> int:
    import json as _json
    init_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cities_json = _json.dumps([c.strip().lower() for c in cities if c.strip()])
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO customer_queries
               (subscriber_id, customer_name, cities, min_price, max_price, min_rooms, student, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (subscriber_id, customer_name.strip(), cities_json,
             min_price or None, max_price or None, min_rooms or None, 1 if student else 0, now),
        )
        return cur.lastrowid


def get_subscribers_with_queries() -> list:
    import json as _json
    init_db()
    with _conn() as c:
        subs = [dict(r) for r in c.execute(
            "SELECT * FROM subscribers WHERE active=1 ORDER BY created_at DESC"
        ).fetchall()]
        for sub in subs:
            queries = []
            for r in c.execute(
                "SELECT * FROM customer_queries WHERE subscriber_id=? AND active=1 ORDER BY created_at",
                (sub["id"],),
            ).fetchall():
                q = dict(r)
                try:
                    q["cities"] = _json.loads(q.get("cities", "[]"))
                except Exception:
                    q["cities"] = []
                queries.append(q)
            sub["queries"] = queries
    return subs


def remove_subscriber(subscriber_id: int):
    init_db()
    with _conn() as c:
        c.execute("UPDATE subscribers SET active=0 WHERE id=?", (subscriber_id,))
        c.execute("UPDATE customer_queries SET active=0 WHERE subscriber_id=?", (subscriber_id,))


def remove_customer_query(query_id: int):
    init_db()
    with _conn() as c:
        c.execute("UPDATE customer_queries SET active=0 WHERE id=?", (query_id,))


def record_scan_run(new_listing_ids: list):
    """Save a scan run result to scan_runs."""
    init_db()
    import json as _json
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _conn() as c:
        c.execute(
            "INSERT INTO scan_runs (ran_at, new_count, new_ids) VALUES (?, ?, ?)",
            (now, len(new_listing_ids), _json.dumps(new_listing_ids)),
        )


def get_scan_runs(limit: int = 50) -> list:
    """Return recent scan runs with their new listings populated."""
    import json as _json
    init_db()
    with _conn() as c:
        runs = c.execute(
            "SELECT id, ran_at, new_count, new_ids FROM scan_runs ORDER BY ran_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for run in runs:
            ids = _json.loads(run["new_ids"])
            listings = []
            if ids:
                placeholders = ",".join("?" * len(ids))
                listings = [
                    dict(row)
                    for row in c.execute(
                        f"SELECT * FROM listings WHERE id IN ({placeholders})", ids
                    ).fetchall()
                ]
                # preserve order
                order = {lid: i for i, lid in enumerate(ids)}
                listings.sort(key=lambda l: order.get(l["id"], 999))
            result.append({
                "id": run["id"],
                "ran_at": run["ran_at"],
                "new_count": run["new_count"],
                "listings": listings,
            })
        return result


def migrate_from_json(json_ids: list):
    """
    Seed the DB from the old seen_listings.json (list of ID strings).
    Inserts stub rows so deduplication still works; details will be
    backfilled naturally on future scans.
    """
    if not json_ids:
        return
    init_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _conn() as c:
        for lid in json_ids:
            src = "Funda" if lid.startswith("funda-") else "Pararius"
            c.execute("""
                INSERT OR IGNORE INTO listings (id, source, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
            """, (lid, src, now, now))


# ── Numeric parsers ────────────────────────────────────────────────────────────

def _parse_price(s: str):
    """'€ 2.500 per maand' or '€2,500 /month' → 2500.0"""
    digits = re.sub(r"[^\d]", "", s)   # strip everything but digits
    # Dutch: €2.500 → "2500"; English: €2,500 → "2500"
    # Both work since we remove all non-digits.
    # Guard against run-together strings; rent is always 3–5 digits.
    if 3 <= len(digits) <= 6:
        try:
            return float(digits)
        except ValueError:
            pass
    return None


def _parse_size(s: str):
    """'75 m²' → 75.0"""
    m = re.search(r"(\d+)\s*m", s)
    return float(m.group(1)) if m else None


def _parse_rooms(s: str):
    """'3' or '3 bedrooms' → 3"""
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None
