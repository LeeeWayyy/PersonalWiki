#!/usr/bin/env python3
"""Re-seed the study database from the committed Japanese grammar-card bank."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from app import db  # noqa: E402

JSON_PATH = ROOT / "pipeline" / "data" / "grammar-cards.ja.json"


def upsert(conn, source_id: str, card: dict) -> None:
    conn.execute(
        """INSERT INTO items(kind,norm_key,lemma,reading,pos,gloss,example,source_id,anchor,created,status,state,due)
           VALUES('grammar',?,?,NULL,?,?,?,?,NULL,?,'known',1,NULL)
           ON CONFLICT(kind,norm_key) DO UPDATE SET
             pos=COALESCE(NULLIF(excluded.pos,''),items.pos),
             gloss=COALESCE(NULLIF(excluded.gloss,''),items.gloss),
             example=COALESCE(NULLIF(excluded.example,''),items.example),
             source_id=COALESCE(NULLIF(excluded.source_id,''),items.source_id)""",
        (
            db.normalize_key("grammar", card["lemma"]),
            card["lemma"],
            card.get("pos"),
            card.get("gloss"),
            card.get("example"),
            source_id,
            db.now_iso(),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=JSON_PATH)
    args = parser.parse_args()
    data = json.loads(args.source.read_text(encoding="utf-8"))
    cards = data.get("cards") if isinstance(data, dict) else None
    source_id = data.get("source_id") if isinstance(data, dict) else None
    if not source_id or not isinstance(cards, list) or not all(
        isinstance(card, dict) and isinstance(card.get("lemma"), str) for card in cards
    ):
        raise SystemExit("grammar bank must contain source_id and a cards list with lemmas")

    conn = db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM items WHERE kind='grammar'").fetchone()[0]
        for card in cards:
            upsert(conn, source_id, card)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM items WHERE kind='grammar'").fetchone()[0]
    finally:
        conn.close()
    print(f"study.db grammar cards: {before} -> {after} (+{after - before} new)")


if __name__ == "__main__":
    main()
