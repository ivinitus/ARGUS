"""SQLite index over output/ audit artifacts."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from argus.core import exec_paths
from argus.domain import finding_inference
from argus import config as argus_config, runtime

DB_FILENAME = "_index.sqlite"
_journal_mode = "WAL"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    path            TEXT PRIMARY KEY,   -- exec dir, relative to out_dir
    key             TEXT NOT NULL,      -- E-key (dir name)
    folder          TEXT NOT NULL,      -- top-level folder slug
    verdict         TEXT,
    tester          TEXT,
    tester_status   TEXT,
    test_case       TEXT,
    marketplace     TEXT,
    execution_date  TEXT,
    findings        INTEGER NOT NULL DEFAULT 0,
    high            INTEGER NOT NULL DEFAULT 0,
    medium          INTEGER NOT NULL DEFAULT 0,
    low             INTEGER NOT NULL DEFAULT 0,
    rules           TEXT NOT NULL DEFAULT '',  -- '|'-joined rule ids
    schema_version  INTEGER,
    dismissed       INTEGER NOT NULL DEFAULT 0,
    mtime           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audits_key      ON audits(key);
CREATE INDEX IF NOT EXISTS idx_audits_folder   ON audits(folder);
CREATE INDEX IF NOT EXISTS idx_audits_verdict  ON audits(verdict);
CREATE INDEX IF NOT EXISTS idx_audits_tester   ON audits(tester);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def configure(settings) -> None:
    global _journal_mode
    _journal_mode = "WAL"


def _connect(out_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(out_dir / DB_FILENAME)
    conn.execute(f"PRAGMA journal_mode={_journal_mode}")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(_SCHEMA)
    return conn


def _row_from_audit(audit_path: Path, out_dir: Path) -> tuple | None:
    """Digest one audit.json (+ sibling metadata.json) into a table row."""
    exec_dir = audit_path.parent
    try:
        audit = json.loads(audit_path.read_text())
        mtime = audit_path.stat().st_mtime
    except (OSError, json.JSONDecodeError):
        return None
    try:
        metadata = json.loads((exec_dir / "metadata.json").read_text())
    except (OSError, json.JSONDecodeError):
        metadata = {}

    try:
        rel = exec_dir.relative_to(out_dir)
    except ValueError:
        rel = exec_dir
    rel_str = str(rel).replace("\\", "/")
    folder = rel_str.split("/", 1)[0] if "/" in rel_str else ""

    audit = dict(audit)
    audit["findings"] = finding_inference.normalise_findings_for_schema(
        list(audit.get("findings") or []))
    audit = finding_inference.reconcile_verdict(audit)

    findings = [f for f in (audit.get("findings") or [])
                if isinstance(f, dict)]
    sev = {"high": 0, "medium": 0, "low": 0}
    rules: list[str] = []
    for f in findings:
        s = f.get("severity")
        if s in sev:
            sev[s] += 1
        if f.get("source") in ("rule", "env_check", "device_check") \
                and f.get("rule"):
            rules.append(str(f["rule"]))

    tr_list = metadata.get("test_results") or []
    tr = tr_list[0] if tr_list else {}
    tc_key = tr.get("test_case_key")
    tc_name = tr.get("test_case_name")
    test_case = (f"{tc_key} — {tc_name}" if tc_key and tc_name
                 else tc_key or tc_name)

    dismissed = any(
        a.get("type") == "dismissed"
        for a in (audit.get("acknowledgments") or [])
        if isinstance(a, dict)
    )

    return (
        rel_str,
        exec_dir.name,
        folder,
        audit.get("overall_verdict"),
        tr.get("executed_by_name") or tr.get("executed_by"),
        tr.get("status"),
        test_case,
        tr.get("marketplace"),
        tr.get("execution_date"),
        len(findings),
        sev["high"], sev["medium"], sev["low"],
        "|".join(rules),
        audit.get("schema_version"),
        1 if dismissed else 0,
        mtime,
    )


def sync(out_dir: Path, *, force: bool = False) -> dict[str, int]:
    """Mirror canonical audits; force also refreshes timestamp-preserving edits."""
    out_dir = out_dir.resolve()
    conn = _connect(out_dir)
    try:
        known: dict[str, float] = dict(
            conn.execute("SELECT path, mtime FROM audits"))
        seen: set[str] = set()
        added = updated = unchanged = 0
        for audit_path in exec_paths.iter_audit_jsons(out_dir):
            exec_dir = audit_path.parent
            try:
                rel = str(exec_dir.relative_to(out_dir)).replace("\\", "/")
                mtime = audit_path.stat().st_mtime
            except (OSError, ValueError):
                continue
            seen.add(rel)
            if not force and rel in known and known[rel] == mtime:
                unchanged += 1
                continue
            row = _row_from_audit(audit_path, out_dir)
            if row is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO audits VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            if rel in known:
                updated += 1
            else:
                added += 1
        removed = 0
        gone = set(known) - seen
        if gone:
            conn.executemany("DELETE FROM audits WHERE path = ?",
                             [(p,) for p in gone])
            removed = len(gone)
        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES ('last_sync', ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S"),))
        conn.commit()
        return {"added": added, "updated": updated,
                "removed": removed, "unchanged": unchanged}
    finally:
        conn.close()


def _cmd_stats(out_dir: Path) -> int:
    conn = _connect(out_dir)
    try:
        total = conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0]
        print(f"{total} audits indexed in {out_dir / DB_FILENAME}")
        print("\nverdicts:")
        for verdict, n in conn.execute(
                "SELECT COALESCE(verdict,'?'), COUNT(*) FROM audits "
                "GROUP BY verdict ORDER BY COUNT(*) DESC"):
            print(f"  {verdict:10s} {n}")
        print("\nrule hits (top 15):")
        rule_counts: dict[str, int] = {}
        for (rules,) in conn.execute(
                "SELECT rules FROM audits WHERE rules != ''"):
            for r in rules.split("|"):
                rule_counts[r] = rule_counts.get(r, 0) + 1
        for r, n in sorted(rule_counts.items(), key=lambda x: -x[1])[:15]:
            print(f"  {r:8s} {n}")
        print("\ntesters with deviations (top 10):")
        for tester, n in conn.execute(
                "SELECT COALESCE(tester,'?'), COUNT(*) FROM audits "
                "WHERE verdict='fail' AND dismissed=0 "
                "GROUP BY tester ORDER BY COUNT(*) DESC LIMIT 10"):
            print(f"  {tester:30s} {n}")
        return 0
    finally:
        conn.close()


def _cmd_query(out_dir: Path, args: argparse.Namespace) -> int:
    conn = _connect(out_dir)
    try:
        clauses, params = [], []
        if args.verdict:
            clauses.append("verdict = ?")
            params.append(args.verdict)
        if args.tester:
            clauses.append("tester LIKE ?")
            params.append(f"%{args.tester}%")
        if args.rule:
            clauses.append("('|' || rules || '|') LIKE ?")
            params.append(f"%|{args.rule}|%")
        if args.folder:
            clauses.append("folder LIKE ?")
            params.append(f"%{args.folder}%")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (f"SELECT key, folder, verdict, tester, findings, high, rules "
               f"FROM audits {where} "
               f"ORDER BY folder, key LIMIT ?")
        params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
        for key, folder, verdict, tester, findings, high, rules in rows:
            print(f"{key:22s} {verdict or '?':9s} {tester or '?':25s} "
                  f"{findings:3d}f/{high}h  {rules:20s} {folder}")
        print(f"({len(rows)} row(s))", file=sys.stderr)
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SQLite index over output/ audit artifacts.")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="incrementally (re)build the index")
    sub.add_parser("stats", help="verdict / rule / tester rollups")
    q = sub.add_parser("query", help="filter indexed audits")
    q.add_argument("--verdict")
    q.add_argument("--tester")
    q.add_argument("--rule")
    q.add_argument("--folder")
    q.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    settings = argus_config.load(args.config)
    runtime.configure(settings)
    out_dir = (args.out_dir or settings.extractor.out_dir).resolve()

    if args.cmd == "sync":
        t0 = time.monotonic()
        counts = sync(out_dir)
        print(f"sync: {counts['added']} added, {counts['updated']} updated, "
              f"{counts['removed']} removed, {counts['unchanged']} unchanged "
              f"({time.monotonic() - t0:.1f}s)")
        return 0
    if args.cmd == "stats":
        return _cmd_stats(out_dir)
    return _cmd_query(out_dir, args)


if __name__ == "__main__":
    sys.exit(main())
