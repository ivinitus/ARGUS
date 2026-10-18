"""R12 self-heal: detect testers who added evidence after a "no evidence" audit."""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from argus import config as argus_config
from argus.core import exec_paths
from argus.core import paths
from argus.pipeline import extractor
from argus import runtime

log = logging.getLogger("argus.r12_heal")

_INCOMPLETE_EVIDENCE_VARIANTS = frozenset({
    "no-evidence",
    "unsupported-evidence",
})


def _is_evidence_attachment(attachment: dict) -> bool:
    """True iff this attachment uses a format the extractor can audit."""
    return extractor.is_supported_evidence_name(attachment.get("name"))


def find_r12_audits(out_dir: Path) -> list[tuple[str, Path]]:
    """Return [(execution_key, audit_json_path), ...] for every no-evidence audit under out_dir."""
    out: list[tuple[str, Path]] = []
    if not out_dir.exists():
        return out
    for audit_path in exec_paths.iter_audit_jsons(out_dir):
        try:
            audit = json.loads(audit_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if audit.get("variant_name") in _INCOMPLETE_EVIDENCE_VARIANTS:
            out.append((audit_path.parent.name, audit_path))
    return out


def has_new_evidence(
    session,
    base_url: str,
    execution_key: str,
) -> bool | None:
    """Return True when the execution now has any auditable attachment."""
    url = f"{base_url}/rest/tests/1.0/testresult/{execution_key}"
    try:
        resp = session.get(
            url,
            params={"fields": "id,key,attachments,"
                              "testScriptResults(attachments)"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.debug("attachment probe for %s returned HTTP %s",
                      execution_key, resp.status_code)
            return None
        data = resp.json()
    except Exception as e:
        log.debug("attachment probe for %s failed: %s: %s",
                  execution_key, type(e).__name__, e)
        return None

    for a in data.get("attachments") or []:
        if _is_evidence_attachment(a):
            return True
    for step in data.get("testScriptResults") or []:
        for a in step.get("attachments") or []:
            if _is_evidence_attachment(a):
                return True
    return False


def reaudit_key(execution_key: str, project_root: Path) -> int:
    """Run `argus audit --key <execution_key>` synchronously and return rc."""
    log_path = Path.home() / ".argus" / "cron.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        *paths.argus_argv(),
        "audit",
        "--key",
        execution_key,
    ]
    with log_path.open("ab") as logf:
        logf.write(
            f"\n==== r12_heal re-audit {execution_key} ====\n"
            .encode("utf-8")
        )
        logf.flush()
        proc = subprocess.run(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            cwd=str(project_root),
        )
    return proc.returncode


def _resolve_active_folder_dir(out_dir: Path,
                               settings: argus_config.ArgusConfig
                               ) -> Path | None:
    """Return the per-folder subdir cron would audit on this tick, or None."""
    if not settings.folders.auto_folder_enabled:
        return None
    try:
        session = extractor.build_session(settings.extractor)
        folder = extractor.find_latest_active_folder(
            session,
            settings.extractor.base_url,
            settings.extractor.project_id,
            name_prefix=settings.folders.auto_name_prefix,
            max_age_days=settings.folders.auto_max_age_days,
        )
    except Exception as e:
        log.debug("active-folder lookup failed: %s: %s",
                  type(e).__name__, e)
        return None
    if not folder:
        return None
    name = folder.get("name")
    if not name:
        return None
    candidate = out_dir / extractor.slugify(name)
    return candidate if candidate.exists() else None


def heal(out_dir: Path,
         project_root: Path | None = None,
         dry_run: bool = False,
         settings: argus_config.ArgusConfig | None = None,
         scope: str = "active") -> dict[str, int]:
    """Walk R12 audits, probe for new evidence, re-audit healed keys."""
    counters = {
        "r12_total": 0,
        "probed": 0,
        "healed": 0,
        "reaudited_ok": 0,
        "reaudited_failed": 0,
        "probe_errors": 0,
    }
    if settings is None:
        settings = argus_config.load()
    project_root = project_root or paths.project_root()

    if scope == "active":
        scan_dir = _resolve_active_folder_dir(out_dir, settings)
        if scan_dir is None:
            log.info("r12_heal: no active folder to scope to; skipping")
            return counters
        log.info("r12_heal: scoped to active folder %s", scan_dir.name)
    else:
        scan_dir = out_dir
        log.info("r12_heal: scanning ALL folders under %s "
                 "(scope='all'; use --scope active for cron)",
                 scan_dir)

    r12_audits = find_r12_audits(scan_dir)
    counters["r12_total"] = len(r12_audits)
    if not r12_audits:
        return counters

    log.info(
        "r12_heal: probing %d incomplete-evidence audit(s) "
        "for new attachments",
             len(r12_audits))

    session = extractor.build_session(settings.extractor)
    healed_keys: list[str] = []
    for execution_key, _audit_path in r12_audits:
        counters["probed"] += 1
        result = has_new_evidence(session, settings.extractor.base_url,
                                  execution_key)
        if result is None:
            counters["probe_errors"] += 1
        elif result:
            healed_keys.append(execution_key)
            counters["healed"] += 1

    if not healed_keys:
        if counters["probe_errors"]:
            log.warning(
                "r12_heal: no keys healed, but %d of %d probe(s) errored — "
                "cannot distinguish outage from unhealed audits this tick",
                counters["probe_errors"], counters["probed"])
        else:
            log.info("r12_heal: no testers added evidence yet")
        return counters

    log.info("r12_heal: %d key(s) self-healed: %s",
             len(healed_keys), ", ".join(healed_keys))

    if dry_run:
        return counters

    try:
        from argus.operations import notify
        body = (f"folder: {scan_dir.name}\n"
                f"keys: {', '.join(healed_keys[:10])}"
                + (f" (+{len(healed_keys) - 10} more)"
                   if len(healed_keys) > 10 else ""))
        notify.post(
            "info",
            f"R12 self-heal — {len(healed_keys)} re-audit(s) queued",
            body, settings=settings,
        )
    except Exception as e:
        log.warning("slack notify failed: %s: %s", type(e).__name__, e)

    for key in healed_keys:
        rc = reaudit_key(key, project_root)
        if rc == 0:
            counters["reaudited_ok"] += 1
        else:
            counters["reaudited_failed"] += 1
            log.warning("r12_heal: re-audit failed for %s (rc=%d)", key, rc)

    return counters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect R12 (no-evidence) audits whose tester has now "
                    "attached evidence, and re-audit them with the real "
                    "pipeline.")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="output dir root (default: from config.toml). "
                             "The actual scan target is determined by --scope.")
    parser.add_argument(
        "--scope", choices=["active", "all"], default="active",
        help="'active' (default): only the latest 'Targeted regression' "
             "folder within 7 days, matching cron's auto-folder pick. "
             "'all': scan every folder under --out-dir (manual sweeps / "
             "backfill).")
    parser.add_argument("--dry-run", action="store_true",
                        help="probe and report; do not re-audit.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    settings = argus_config.load(args.config)
    runtime.configure(settings)
    out_dir = (Path(args.out_dir).resolve() if args.out_dir
               else paths.output_root(settings))
    extractor.configure_logging(args.verbose)

    print(f"r12_heal: scope={args.scope}  out_dir={out_dir}")
    counters = heal(out_dir, dry_run=args.dry_run,
                    settings=settings, scope=args.scope)
    print(f"\nr12_total:        {counters['r12_total']}")
    print(f"probed:           {counters['probed']}")
    print(f"healed:           {counters['healed']}")
    print(f"probe_errors:     {counters['probe_errors']}")
    if not args.dry_run:
        print(f"reaudited_ok:     {counters['reaudited_ok']}")
        print(f"reaudited_failed: {counters['reaudited_failed']}")
    else:
        print("(dry-run: no re-audits attempted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
