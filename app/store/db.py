import sqlite3
from pathlib import Path

from app.config import db_path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def migrate() -> None:
    conn = connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
        )
        applied = {row["name"] for row in conn.execute("SELECT name FROM _migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            if path.name == "001_init.sql":
                # 001 在迁移记账表建立前可能已经落库，存在 orders 表即视为已应用
                exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
                if exists:
                    conn.execute("INSERT INTO _migrations(name) VALUES(?)", (path.name,))
                    continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO _migrations(name) VALUES(?)", (path.name,))
    finally:
        conn.close()
