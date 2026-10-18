"""Atomic policy publication and immutable versions, independent of audit state."""
from __future__ import annotations

import json
import sqlite3
import threading



class RuleConflict(RuntimeError):
    pass




class SqliteRuleStore:
    """Durable policy state with atomic conditional transactions."""
    def __init__(self, path: str):
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS rule_items "
            "(team TEXT, key TEXT, data TEXT, PRIMARY KEY(team,key))",
        )

    def get(self, team: str, key: str) -> dict:
        with self.lock:
            row = self.connection.execute(
                "SELECT data FROM rule_items WHERE team=? AND key=?", (team, key),
            ).fetchone()
            return json.loads(row[0]) if row else {}

    def query(self, team: str, prefix: str) -> list[dict]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT key,data FROM rule_items WHERE team=? ORDER BY key", (team,),
            ).fetchall()
            return [json.loads(data) for key, data in rows if key.startswith(prefix)]

    def put(self, team: str, key: str, data: dict, *, phase: str | None = None) -> None:
        with self.lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.get(team, key)
            if (phase is None and existing) or (phase is not None and existing.get("phase") != phase):
                raise RuleConflict("Rule state changed. Refresh before retrying.")
            self.connection.execute(
                "INSERT OR REPLACE INTO rule_items VALUES (?,?,?)", (team, key, json.dumps(data)),
            )

    def commit(self, team: str, expected: int, head: dict, writes: dict[str, dict]) -> None:
        with self.lock, self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.get(team, "head").get("version", 0) != expected:
                raise RuleConflict("Rule state changed. Refresh before saving.")
            if any(self.get(team, key) for key in writes):
                raise RuleConflict("This immutable revision already exists.")
            for key, data in {"head": head, **writes}.items():
                self.connection.execute(
                    "INSERT OR REPLACE INTO rule_items VALUES (?,?,?)", (team, key, json.dumps(data)),
                )
