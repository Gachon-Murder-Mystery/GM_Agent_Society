"""SQLite state and an engine outbox, committed in the same transaction."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path=":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.lock = threading.RLock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS states (
                namespace TEXT NOT NULL, game_id TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(namespace, game_id));
            CREATE TABLE IF NOT EXISTS events (
                game_id TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(game_id, sequence));
        """)
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    @staticmethod
    def get(db, namespace, game_id):
        row = db.execute("SELECT body FROM states WHERE namespace=? AND game_id=?", (namespace, game_id)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def put(db, namespace, game_id, value):
        db.execute("INSERT OR REPLACE INTO states VALUES (?,?,?)", (namespace, game_id, json.dumps(value, ensure_ascii=False)))

    def read(self, namespace, game_id):
        with self.lock:
            return self.get(self.db, namespace, game_id)

    def close(self):
        with self.lock:
            self.db.close()
