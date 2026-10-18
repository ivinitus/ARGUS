"""Dedicated review persistence; source audit storage is never modified."""
from __future__ import annotations

import json



class ReviewConflict(Exception):
    """The caller must refresh before changing this review."""




class SqliteReviewStore:
    """Durable review state with atomic conditional writes."""
    def __init__(self, path: str):
        import sqlite3

        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS review_items "
            "(scope TEXT, key TEXT, data TEXT, PRIMARY KEY(scope,key))"
        )
        import threading

        self.lock = threading.RLock()

    def get(self, scope: str, key: str) -> dict:
        with self.lock:
            row = self.connection.execute(
                "SELECT data FROM review_items WHERE scope=? AND key=?", (scope, key),
            ).fetchone()
            return json.loads(row[0]) if row else {}

    def query(self, scope: str, prefix: str) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT key,data FROM review_items WHERE scope=? ORDER BY key", (scope,),
            )
            return [json.loads(data) for key, data in rows if key.startswith(prefix)]

    def put(self, scope: str, key: str, data: dict) -> None:
        with self.lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO review_items VALUES (?,?,?)", (scope, key, json.dumps(data)),
            )

    def commit(self, scope: str, expected: int, head: dict, writes: dict[str, dict]) -> None:
        with self.lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.get(scope, "head").get("version", 0) != expected:
                raise ReviewConflict("Review changed. Refresh before saving.")
            for key, data in {"head": head, **writes}.items():
                self.connection.execute(
                    "INSERT OR REPLACE INTO review_items VALUES (?,?,?)",
                    (scope, key, json.dumps(data)),
                )
