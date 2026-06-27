"""bench.py — thin benchmark wrapper around `argus.py --folder ...`.

Runs argus.py as a subprocess, streams its stderr live to the terminal
(so progress lines appear as they happen), and parses the per-key
progress lines to print a benchmark summary at the end.

Usage:
    CLOUD_PROFILE=llm_runtime .venv/bin/python bench.py --folder "Sprint 47 Regression"

Any extra args after --folder are forwarded to argus.py:
    .venv/bin/python bench.py --folder "Sprint 47 Regression" -v --debug

No code changes to argus/batch/auditor. We rely on the existing progress
line shapes:
    [done/total] <KEY> ok (<verdict>, <N> finding[s], <secs>s)
    [done/total] <KEY> skipped (<reason>)
    [done/total] <KEY> FAILED  <reason>
    [done/total] <KEY> AUTH_FAILED  <reason>
"""
from __future__ import annotations

import argparse
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


# Shapes of batch's _print_progress lines. Kept loose so cosmetic changes
# (extra spaces, punctuation) don't silently break parsing.
_OK_RE = re.compile(
    r"\[(?P<done>\d+)/(?P<total>\d+)\]\s+(?P<key>\S+)\s+ok\s+"
    r"\((?P<verdict>\w+),\s*(?P<findings>\d+)\s+findings?,\s*"
    r"(?P<secs>[\d.]+)s\)"
)
_SKIPPED_RE = re.compile(
    r"\[(?P<done>\d+)/(?P<total>\d+)\]\s+(?P<key>\S+)\s+skipped\s*"
    r"\((?P<reason>.*?)\)"
)
_FAILED_RE = re.compile(
    r"\[(?P<done>\d+)/(?P<total>\d+)\]\s+(?P<key>\S+)\s+"
    r"(?P<status>FAILED|AUTH_FAILED)\s+(?P<reason>.*)"
)
_HEADER_RE = re.compile(
    r"batch:\s+(?P<total>\d+)\s+keys,\s+concurrency=(?P<conc>\d+)\s*"
    r"\(extractor=(?P<ex_conc>\d+)\)"
)


@dataclass
class KeyRecord:
    key: str
    status: str                  # "ok" | "skipped" | "failed" | "auth_failed"
    verdict: str | None = None
    findings: int | None = None
    secs: float | None = None    # per-key elapsed (only populated for "ok")
    reason: str | None = None


def _fmt_hms(secs: float) -> str:
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{int(h)}h{int(m):02d}m{s:04.1f}s"
    if m:
        return f"{int(m)}m{s:04.1f}s"
    return f"{s:.1f}s"


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(int(round(p / 100 * (len(s) - 1))), len(s) - 1))
    return s[idx]


def _parse_line(line: str, records: list[KeyRecord],
                header: dict[str, int]) -> None:
    """Match a single stderr line against the known progress shapes.

    Updates `records` / `header` in place. Silently ignores lines that
    don't match — argus prints plenty of other stuff (folder resolution,
    by-tester breakdown, etc.) that we don't need to parse.
    """
    m = _HEADER_RE.search(line)
    if m:
        header["total"] = int(m.group("total"))
        header["concurrency"] = int(m.group("conc"))
        header["extractor_concurrency"] = int(m.group("ex_conc"))
        return

    m = _OK_RE.search(line)
    if m:
        records.append(KeyRecord(
            key=m.group("key"),
            status="ok",
            verdict=m.group("verdict"),
            findings=int(m.group("findings")),
            secs=float(m.group("secs")),
        ))
        return

    m = _SKIPPED_RE.search(line)
    if m:
        records.append(KeyRecord(
            key=m.group("key"),
            status="skipped",
            reason=m.group("reason").strip(),
        ))
        return

    m = _FAILED_RE.search(line)
    if m:
        status = "auth_failed" if m.group("status") == "AUTH_FAILED" else "failed"
        records.append(KeyRecord(
            key=m.group("key"),
            status=status,
            reason=m.group("reason").strip(),
        ))
        return


def _print_report(folder: str,
                  wall_s: float,
                  records: list[KeyRecord],
                  header: dict[str, int],
                  rc: int,
                  out=sys.stderr) -> None:
    p = lambda *a, **k: print(*a, file=out, **k)  # noqa: E731
    p()
    p("=" * 60)
    p("PROJECT THEIA — FOLDER BENCHMARK")
    p("=" * 60)
    p(f"folder:      {folder}")
    p(f"wall time:   {_fmt_hms(wall_s)}")
    p(f"exit code:   {rc}")
    if header:
        p(f"concurrency: {header.get('concurrency', '?')} "
          f"(extractor cap: {header.get('extractor_concurrency', '?')})")
    p()

    if not records:
        p("no per-key progress lines parsed — argus may have exited before "
          "the batch phase (folder not found, auth failure, etc.).")
        return

    ok = [r for r in records if r.status == "ok"]
    skipped = [r for r in records if r.status == "skipped"]
    failed = [r for r in records if r.status == "failed"]
    auth = [r for r in records if r.status == "auth_failed"]

    p("--- RESULTS ---")
    p(f"  processed:    {len(records)}")
    p(f"  ok:           {len(ok)}")
    p(f"  skipped:      {len(skipped)}")
    p(f"  failed:       {len(failed)}")
    p(f"  auth_failed:  {len(auth)}")
    if failed or auth:
        p("  failure details:")
        for r in failed + auth:
            p(f"    - [{r.status}] {r.key}: {r.reason}")
    p()

    if not ok:
        p("(no successful keys — skipping per-key timing stats)")
        return

    secs = [r.secs for r in ok if r.secs is not None]
    if not secs:
        return

    p(f"--- PER-KEY TIMING (ok only, n={len(secs)}) ---")
    p(f"  p50:   {_pct(secs, 50):>7.1f}s")
    p(f"  p90:   {_pct(secs, 90):>7.1f}s")
    p(f"  p99:   {_pct(secs, 99):>7.1f}s")
    p(f"  max:   {max(secs):>7.1f}s")
    p(f"  mean:  {statistics.mean(secs):>7.1f}s")
    p(f"  sum:   {_fmt_hms(sum(secs))}  (summed across workers)")
    p()

    # Verdict + finding breakdown.
    verdicts: dict[str, int] = {}
    for r in ok:
        verdicts[r.verdict or "?"] = verdicts.get(r.verdict or "?", 0) + 1
    total_findings = sum(r.findings or 0 for r in ok)
    p("--- VERDICTS ---")
    for v in ("pass", "concerns", "fail"):
        if v in verdicts:
            p(f"  {v:<10} {verdicts[v]}")
    for v, n in verdicts.items():
        if v not in {"pass", "concerns", "fail"}:
            p(f"  {v:<10} {n}")
    p(f"  total findings: {total_findings}")
    p()

    # Slowest 10.
    p("--- SLOWEST 10 KEYS ---")
    slow = sorted(
        [r for r in records if r.secs is not None],
        key=lambda r: -(r.secs or 0),
    )[:10]
    for r in slow:
        v = r.verdict or r.status
        f_ct = r.findings if r.findings is not None else "-"
        p(f"  {r.key:<22} {_fmt_hms(r.secs or 0):>10}  "
          f"verdict={v}, findings={f_ct}")
    p()

    # Throughput + effective concurrency (over successful keys only).
    rate_min = len(records) * 60 / wall_s if wall_s > 0 else 0.0
    total_work = sum(secs)
    effective_conc = total_work / wall_s if wall_s > 0 else 0.0
    conc = header.get("concurrency")
    utilization = (100 * effective_conc / conc) if conc else 0.0
    p("--- THROUGHPUT ---")
    p(f"  rate:                  {rate_min:.2f} keys/min")
    p(f"  effective concurrency: {effective_conc:.2f}"
      + (f" (configured {conc})" if conc else ""))
    if conc:
        p(f"  utilization:           {utilization:.0f}%")
        if utilization < 70:
            p("  note: utilization <70% — workers spent meaningful time "
              "idle; likely extractor cap or front-load serialisation.")
    p()

    # Projections. Only print if we have the configured concurrency and
    # non-trivial sample size.
    if conc and total_work > 0 and len(ok) >= 5:
        ideal_now = total_work / conc
        overhead = wall_s / ideal_now if ideal_now > 0 else 1.0
        p("--- PROJECTIONS (same workload, different concurrency) ---")
        p(f"  overhead factor vs ideal: {overhead:.2f}x")
        for new_conc in (8, 12, 16, 20, 24):
            projected = (total_work / new_conc) * overhead
            tag = "  (current)" if new_conc == conc else ""
            p(f"  at concurrency={new_conc:2d}: ~{_fmt_hms(projected)}{tag}")
        p()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark wrapper around `argus.py --folder ...`.",
        allow_abbrev=False,
    )
    parser.add_argument("--folder", required=True,
                        help="test-management folder name (forwarded to argus.py)")
    parser.add_argument("--python", default=None,
                        help="python interpreter (default: .venv/bin/python "
                             "if present, else sys.executable)")
    # Any unknown args are forwarded to argus.py unchanged (e.g. -v --debug).
    args, extras = parser.parse_known_args(argv)

    repo = Path(__file__).parent
    argus_script = repo / "argus.py"
    if not argus_script.exists():
        print(f"argus.py not found alongside bench.py at {repo}",
              file=sys.stderr)
        return 1

    # Prefer project venv python when available, for parity with README.
    venv_py = repo / ".venv" / "bin" / "python"
    python_bin = args.python or (
        str(venv_py) if venv_py.exists() else sys.executable
    )

    cmd = [python_bin, str(argus_script), "--folder", args.folder, *extras]
    print(f"[bench] running: {' '.join(cmd)}", file=sys.stderr)

    records: list[KeyRecord] = []
    header: dict[str, int] = {}

    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdout=None,                    # argus.py writes progress to stderr
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,                      # line-buffered
    )
    assert proc.stderr is not None
    for line in proc.stderr:
        # Tee to our stderr so live progress shows up as usual.
        sys.stderr.write(line)
        sys.stderr.flush()
        _parse_line(line, records, header)
    rc = proc.wait()
    wall_s = time.monotonic() - t0

    _print_report(args.folder, wall_s, records, header, rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
