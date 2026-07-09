"""SQLite store for the personal word/grammar bank + SRS state.

This is your evolving study state — deliberately SEPARATE from the vault git
history. Back up the single .db file; there is a CSV/Anki export endpoint too.
"""
from __future__ import annotations
import os
import sqlite3
import datetime as dt
from pathlib import Path

DATA_DIR = Path(os.environ.get("PW_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "study.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT 'word',        -- word | grammar
  norm_key TEXT NOT NULL,                    -- normalized cross-source lemma key
  lemma TEXT NOT NULL,
  reading TEXT,
  pos TEXT,
  gloss TEXT,
  example TEXT,
  source_id TEXT,
  anchor TEXT,
  status TEXT NOT NULL DEFAULT 'new',        -- new | learning | known
  stability REAL NOT NULL DEFAULT 0,
  difficulty REAL NOT NULL DEFAULT 0,
  state INTEGER NOT NULL DEFAULT 0,          -- 0 new, 1 review
  reps INTEGER NOT NULL DEFAULT 0,
  lapses INTEGER NOT NULL DEFAULT 0,
  due TEXT,                                  -- ISO date; NULL = due now (new)
  last_review TEXT,
  created TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_key ON items(kind, norm_key);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL,
  grade INTEGER NOT NULL,
  reviewed TEXT NOT NULL,
  interval INTEGER,
  FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS translations (
  text_hash TEXT PRIMARY KEY,
  lang TEXT,
  translation TEXT,
  prompt_version TEXT,
  llm_provider TEXT,
  llm_model TEXT,
  created TEXT
);
CREATE TABLE IF NOT EXISTS annotations (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  block_id TEXT, section_id TEXT,
  prev_block_id TEXT, next_block_id TEXT,
  quote TEXT, prefix TEXT, suffix TEXT,
  sel_start INTEGER, sel_end INTEGER,
  region TEXT,                    -- JSON {x,y,w,h} normalized 0..1 for image blocks
  body TEXT, color TEXT,
  tags TEXT, links TEXT,          -- JSON arrays
  created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_annotations_source ON annotations(source_id);
CREATE INDEX IF NOT EXISTS idx_items_state_due ON items(state, due);
CREATE INDEX IF NOT EXISTS idx_reviews_item ON reviews(item_id);
"""


def normalize_key(kind: str, lemma: str) -> str:
    # Cross-source identity: strip whitespace + lowercase the ASCII portion.
    # Homograph glosses are a documented tokenizer limitation (plan §3a).
    return f"{kind}:{(lemma or '').strip().lower()}"


def now_iso() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> dt.date:
    return dt.datetime.utcnow().date()


# Additive migrations for tables that predate a column. SQLite has no
# "ADD COLUMN IF NOT EXISTS", so we attempt and swallow the duplicate-column error.
_MIGRATIONS = [
    "ALTER TABLE annotations ADD COLUMN region TEXT",  # P4: image/region selector (JSON)
    "ALTER TABLE translations ADD COLUMN prompt_version TEXT",
    "ALTER TABLE translations ADD COLUMN llm_provider TEXT",
    "ALTER TABLE translations ADD COLUMN llm_model TEXT",
]


def _ensure_reviews_foreign_key(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA foreign_key_list(reviews)").fetchone():
        return
    conn.execute("DROP INDEX IF EXISTS idx_reviews_item")
    conn.execute("ALTER TABLE reviews RENAME TO reviews_legacy")
    conn.execute(
        """CREATE TABLE reviews (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          item_id INTEGER NOT NULL,
          grade INTEGER NOT NULL,
          reviewed TEXT NOT NULL,
          interval INTEGER,
          FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
        )"""
    )
    conn.execute(
        """INSERT INTO reviews(id,item_id,grade,reviewed,interval)
           SELECT r.id,r.item_id,r.grade,r.reviewed,r.interval
           FROM reviews_legacy r
           WHERE EXISTS (SELECT 1 FROM items i WHERE i.id=r.item_id)"""
    )
    conn.execute("DROP TABLE reviews_legacy")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_item ON reviews(item_id)")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise sqlite3.OperationalError(f"migration failed: {stmt}: {e}") from e
    _ensure_reviews_foreign_key(conn)
    conn.commit()
    return conn
