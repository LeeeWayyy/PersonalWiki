import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("study_db", ROOT / "scripts" / "study_db.py")
study_db = importlib.util.module_from_spec(SPEC)
sys.modules["study_db"] = study_db
SPEC.loader.exec_module(study_db)


def write_value(db: Path, value: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS cards (value TEXT)")
        conn.execute("DELETE FROM cards")
        conn.execute("INSERT INTO cards VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def read_value(db: Path) -> str:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT value FROM cards").fetchone()
        return row[0]
    finally:
        conn.close()


class StudyDbTests(unittest.TestCase):
    def test_backup_and_restore_round_trip_with_safety_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "study.db"
            backup_dir = root / "backups"
            write_value(db, "before")

            backup = study_db.backup(db, backup_dir)
            self.assertTrue(backup.is_file())
            self.assertEqual(read_value(backup), "before")

            write_value(db, "after")
            safety = study_db.restore(backup, db, backup_dir)

            self.assertIsNotNone(safety)
            self.assertTrue(safety.is_file())
            self.assertEqual(read_value(safety), "after")
            self.assertEqual(read_value(db), "before")

    def test_restore_over_corrupt_db_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "study.db"
            backup_dir = root / "backups"
            write_value(db, "good")
            backup = study_db.backup(db, backup_dir)

            db.write_bytes(b"this is not a sqlite database")  # corrupt the live db
            safety = study_db.restore(backup, db, backup_dir)

            self.assertIsNotNone(safety)
            self.assertTrue(safety.is_file())  # raw-copy fallback still snapshotted it
            self.assertEqual(read_value(db), "good")  # restore succeeded despite corruption

    def test_backup_rejects_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(study_db.StudyDbError, "study db not found"):
                study_db.backup(Path(tmp) / "missing.db", Path(tmp) / "backups")

    def test_restore_rejects_same_file_before_touching_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "study.db"
            backup_dir = root / "backups"
            write_value(db, "live")
            wal = root / "study.db-wal"
            shm = root / "study.db-shm"
            wal.write_text("pending wal", encoding="utf-8")
            shm.write_text("pending shm", encoding="utf-8")

            with self.assertRaisesRegex(study_db.StudyDbError, "from itself"):
                study_db.restore(db, db, backup_dir)

            self.assertTrue(wal.is_file())
            self.assertTrue(shm.is_file())


if __name__ == "__main__":
    unittest.main()
