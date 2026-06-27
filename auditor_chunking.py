"""Chunking, consensus, and per-chunk merging for auditor.run().

Extracted from auditor.py 2026-05-31. Re-exported from `auditor` for
back-compat. invoke_and_parse / invoke_model / build_message_content
imports are deferred (inside-function) to break the circular import.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any, TYPE_CHECKING

import boto3
from botocore.config import Config as BotoConfig

if TYPE_CHECKING:
    from auditor import AuditConfig  # noqa: F401  (used in quoted annotations)

log = logging.getLogger("argus.audit")


# Consensus constants — 3 runs, finding must appear in >=2 to survive.
CONSENSUS_RUNS = 3
CONSENSUS_THRESHOLD = 2


def _finding_key(f: dict[str, Any]) -> tuple:
    """Equivalence key for matching findings across consensus runs.

    Matches on (severity, step_index|page) only — descriptions vary
    even when describing the same issue. When neither step nor page is
    set, identity-key so the finding only matches itself (uncorroborated
    unlocated findings can't survive consensus, which is the safe default).
    """
    sev = f.get("severity")
    step = f.get("step_index")
    page = f.get("page")
    if step is not None:
        return ("step", sev, step)
    if page is not None:
        return ("page", sev, page)
    return ("anon", sev, id(f))


def audit_with_consensus(
    cfg: AuditConfig,
    content: list[dict[str, Any]],
) -> dict[str, Any]:
    """Re-run audits with high-severity findings to filter to >=2/3 consensus.

    Low/medium findings from run 1 pass through. Adds a `_consensus` trace
    key for audit.json. cfg.consensus_enabled=False short-circuits to
    invoke_and_parse.
    """
    # Deferred to break the auditor <-> auditor_chunking import cycle.
    from auditor import invoke_and_parse

    first = invoke_and_parse(cfg, content)

    if not cfg.consensus_enabled:
        return first

    # Parse/validation fallback from round 1 — don't burn calls on garbage.
    if "_raw" in first or "_invalid_original" in first or "_invalid_repair" in first:
        return first

    findings = first.get("findings") or []
    high_findings = [f for f in findings if f.get("severity") == "high"]

    if not high_findings:
        return first  # common case — no consensus needed

    log.info("consensus: %d high finding(s) in run 1, running %d confirmation passes",
             len(high_findings), CONSENSUS_RUNS - 1)

    extra_high_key_sets: list[set] = []
    for run_idx in range(2, CONSENSUS_RUNS + 1):
        extra = invoke_and_parse(cfg, content)
        if "_raw" in extra or "_invalid_original" in extra or "_invalid_repair" in extra:
            # Skipped: don't let one bad response unilaterally kill a high.
            log.warning("consensus run %d produced invalid output; skipping",
                        run_idx)
            continue
        extra_findings = extra.get("findings") or []
        extra_high_key_sets.append(
            {_finding_key(f) for f in extra_findings if f.get("severity") == "high"}
        )

    # Honest denominator — skipped runs don't cast silent "no" votes.
    effective_runs = 1 + len(extra_high_key_sets)
    surviving_high: list[dict[str, Any]] = []
    consensus_trace: list[dict[str, Any]] = []
    for f in high_findings:
        key = _finding_key(f)
        extra_votes = sum(1 for s in extra_high_key_sets if key in s)
        total_votes = 1 + extra_votes
        consensus_trace.append({
            "step_index": f.get("step_index"),
            "page": f.get("page"),
            "description": f.get("description"),
            "votes": total_votes,
            "runs": effective_runs,
            "kept": total_votes >= CONSENSUS_THRESHOLD,
        })
        if total_votes >= CONSENSUS_THRESHOLD:
            surviving_high.append(f)

    # Assemble final findings: preserved low/medium + surviving high.
    non_high = [f for f in findings if f.get("severity") != "high"]
    final_findings = non_high + surviving_high

    # Verdict: any surviving high => fail, else any non-high => concerns,
    # else pass (all highs rejected).
    if surviving_high:
        verdict = "fail"
    elif non_high:
        verdict = "concerns"
    else:
        verdict = "pass"

    summary = first.get("summary", "")
    dropped = len(high_findings) - len(surviving_high)
    if dropped:
        summary = (
            f"{summary}\n\n[Consensus: {dropped} high-severity finding(s) "
            f"rejected as not reproducible across {effective_runs} audit "
            f"run(s)."
            + (f" Note: {CONSENSUS_RUNS - effective_runs} confirmation "
               f"run(s) were skipped due to invalid output."
               if effective_runs < CONSENSUS_RUNS else "")
            + "]"
        ).strip()

    return {
        "overall_verdict": verdict,
        "summary": summary,
        "findings": final_findings,
        "_consensus": consensus_trace,
    }


# Chunk-local page refs the model actually produces:
#   "page 5", "pages 5-7", "pages 5–7", "pages 5 and 7", "pages 5, 6, 7".
# Doesn't touch bare numbers — would misfire on step indices, counts, etc.
_PAGE_REF_RE = re.compile(
    r"(?i)\b(pages?)\s+(\d+(?:\s*(?:[-–—,]|and)\s*\d+)*)"
)


def _rewrite_page_refs(text: str, chunk_start: int, chunk_end: int) -> str:
    """Rewrite chunk-local 'page N' mentions in `text` to global indices.

    chunk_start is 0-indexed; global page = chunk_start + local_page.
    Out-of-range numbers (beyond chunk length) are clamped to chunk_end,
    matching merge_chunk_audits' top-level page clamping behavior.
    """
    if not text or not isinstance(text, str):
        return text

    def _translate_number(match: re.Match) -> str:
        n = int(match.group(0))
        global_n = chunk_start + n
        if global_n > chunk_end:
            global_n = chunk_end
        return str(global_n)

    def _sub(match: re.Match) -> str:
        prefix = match.group(1)  # "page" or "pages"
        numbers_block = match.group(2)
        rewritten = re.sub(r"\d+", _translate_number, numbers_block)
        return f"{prefix} {rewritten}"

    return _PAGE_REF_RE.sub(_sub, text)


def plan_chunks(
    n_pages: int,
    chunk_size: int,
    chunk_overlap: int,
) -> list[tuple[int, int]]:
    """Return a list of (start, end) 0-indexed half-open page ranges.

    Chunks step forward by `chunk_size - chunk_overlap` pages so adjacent
    chunks share `chunk_overlap` pages. The last chunk is truncated at
    n_pages. Guarantees: every page index in [0, n_pages) appears in at
    least one chunk.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be in [0, chunk_size)")
    if n_pages <= 0:
        return []
    step = chunk_size - chunk_overlap
    chunks: list[tuple[int, int]] = []
    start = 0
    while start < n_pages:
        end = min(start + chunk_size, n_pages)
        chunks.append((start, end))
        if end >= n_pages:
            break
        start += step
    return chunks


def _combine_verdicts(verdicts: list[str]) -> str:
    """Worst-case verdict combination: fail > concerns > pass."""
    if "fail" in verdicts:
        return "fail"
    if "concerns" in verdicts:
        return "concerns"
    return "pass"


def merge_chunk_audits(
    chunk_audits: list[dict[str, Any]],
    chunk_ranges: list[tuple[int, int]],
) -> dict[str, Any]:
    """Merge N chunk audits into a single audit dict.

    Rewrites finding.page from chunk-local to global, dedupes via
    _finding_key, worst-case verdict, summary concatenation, preserves
    per-chunk diagnostic keys (_consensus, _invalid_*, _raw).
    """
    if len(chunk_audits) != len(chunk_ranges):
        raise ValueError("chunk_audits and chunk_ranges must be same length")

    merged_findings: list[dict[str, Any]] = []
    seen_keys: set = set()
    verdicts: list[str] = []
    summary_parts: list[str] = []
    evidence_by_step: dict[int, dict[str, Any]] = {}
    # Collect per-chunk diagnostics under per-chunk keys so they survive.
    diagnostics: dict[str, Any] = {}

    for idx, (audit, (start, end)) in enumerate(zip(chunk_audits, chunk_ranges)):
        verdicts.append(audit.get("overall_verdict", "concerns"))
        summary_parts.append(
            f"[Chunk {idx + 1}/{len(chunk_audits)} "
            f"(pages {start + 1}-{end})]: {audit.get('summary', '')}"
        )
        # Surface diagnostics from the fallback paths so operators can see
        # which chunk went wrong without having to re-run.
        for dk in ("_raw", "_invalid_original", "_invalid_repair", "_consensus"):
            if dk in audit:
                diagnostics.setdefault(f"chunk_{idx + 1}", {})[dk] = audit[dk]

        for f in audit.get("findings") or []:
            # Rewrite page from chunk-local to global.
            local_page = f.get("page")
            rewritten = dict(f)
            if isinstance(local_page, int) and local_page >= 1:
                # Chunk-local page 1 = first page of chunk = global (start + 1).
                rewritten["page"] = start + local_page
                # Bound check: don't let a model over-count past its chunk.
                if rewritten["page"] > end:
                    log.warning(
                        "chunk %d: finding page %d exceeds chunk end %d; "
                        "clamping to end",
                        idx + 1, rewritten["page"], end,
                    )
                    rewritten["page"] = end
            # Rewrite chunk-local page refs inside the description so the
            # narrative matches the top-level page field.
            desc = rewritten.get("description")
            if isinstance(desc, str):
                rewritten["description"] = _rewrite_page_refs(desc, start, end)
            # Dedupe by finding key. Use rewritten (global-page) finding so
            # overlap-zone duplicates produce the same key.
            key = _finding_key(rewritten)
            if key[0] == "anon":
                # Anonymous findings (no page, no step) use id() in
                # _finding_key, so each is unique and they never collide.
                # In the MERGE path that means a single global observation
                # ("no environment URL visible anywhere") reported in every
                # overlapping chunk lands N times. Here we can safely dedupe
                # on (severity, description): two genuinely distinct findings
                # have distinct text, so exact-match only collapses true
                # repeats. (We do NOT change _finding_key — the consensus
                # path deliberately wants unlocated findings to match only
                # themselves.)
                key = ("anon-merge", rewritten.get("severity"),
                       (rewritten.get("description") or "").strip())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged_findings.append(rewritten)

        for row in audit.get("evidence_by_step") or []:
            if not isinstance(row, dict) or not isinstance(row.get("step_index"), int):
                continue
            step = row["step_index"]
            merged_row = evidence_by_step.setdefault(step, {
                "step_index": step,
                "pages": set(),
                "confidence": "low",
                "status": "not_assessed",
                "missing_reason": None,
            })
            for local_page in row.get("pages") or []:
                if isinstance(local_page, int) and local_page >= 1:
                    merged_row["pages"].add(min(start + local_page, end))
            confidence = row.get("confidence")
            if confidence in {"high", "medium", "low"}:
                rank = {"low": 0, "medium": 1, "high": 2}
                if rank[confidence] > rank.get(merged_row["confidence"], 0):
                    merged_row["confidence"] = confidence
            status = row.get("status")
            if status in {"issue_found", "supported", "partial", "missing"}:
                rank = {
                    "not_assessed": 0,
                    "missing": 1,
                    "partial": 2,
                    "supported": 3,
                    "issue_found": 4,
                }
                if rank[status] > rank.get(merged_row["status"], 0):
                    merged_row["status"] = status
            if row.get("missing_reason") and not merged_row["missing_reason"]:
                merged_row["missing_reason"] = row.get("missing_reason")

    out = {
        "overall_verdict": _combine_verdicts(verdicts),
        "summary": "\n\n".join(summary_parts),
        "findings": merged_findings,
    }
    if evidence_by_step:
        out["evidence_by_step"] = [
            {
                **{k: v for k, v in row.items() if k != "pages"},
                "pages": sorted(row["pages"]),
            }
            for row in sorted(evidence_by_step.values(),
                              key=lambda r: r["step_index"])
        ]
    if diagnostics:
        out["_chunk_diagnostics"] = diagnostics
    return out


def audit_chunked(
    cfg: AuditConfig,
    steps_text: str,
    pages: list[Path],
    step_attachments: list[tuple[Path, int | None]] | None = None,
) -> dict[str, Any]:
    """Audit a large execution by splitting pages into chunks and merging.

    pages = full (max_pages-capped) list. steps_text is NOT chunked —
    model needs the whole test context. step_attachments are cross-
    cutting; passed to every chunk. After merging, a text-only LLM Runtime
    call synthesizes a coherent summary; on synthesis failure the
    concatenated chunk summary is kept.
    """
    # Deferred to break the auditor <-> auditor_chunking import cycle.
    from auditor import build_message_content

    ranges = plan_chunks(len(pages), cfg.chunk_size, cfg.chunk_overlap)
    log.info("chunking %d pages into %d chunks (size=%d, overlap=%d)",
             len(pages), len(ranges), cfg.chunk_size, cfg.chunk_overlap)

    def _audit_one_chunk(idx: int) -> dict[str, Any]:
        start, end = ranges[idx]
        chunk_pages = pages[start:end]
        log.info("auditing chunk %d/%d (pages %d-%d, %d images)",
                 idx + 1, len(ranges), start + 1, end, len(chunk_pages))
        content = build_message_content(steps_text, chunk_pages,
                                        step_attachments=step_attachments)
        # Tell the model this is one chunk so it doesn't flag "missing
        # screenshots" for pages covered by other chunks.
        content.insert(0, {
            "type": "text",
            "text": (
                f"NOTE: You are auditing CHUNK {idx + 1} of {len(ranges)} "
                f"from a large execution. This chunk contains pages "
                f"{start + 1}-{end} of {len(pages)} total. Only report "
                "findings visible in the screenshots you can see. Do NOT "
                "flag missing screenshots or incomplete coverage — other "
                "chunks cover the rest. Number pages 1-N within this chunk; "
                "we'll renumber to the global index after merging."
            ),
        })
        return audit_with_consensus(cfg, content)

    # Order-preserving slot assignment so chunk_audits[i] corresponds to
    # ranges[i] regardless of completion order (merge_chunk_audits zips
    # them). Per-chunk exceptions propagate via fut.result().
    max_parallel = max(1, min(cfg.chunk_max_parallel, len(ranges)))
    chunk_audits: list[dict[str, Any] | None] = [None] * len(ranges)
    if max_parallel == 1 or len(ranges) == 1:
        # Preserve pre-parallel behaviour exactly when no parallelism is
        # requested. Avoids the ThreadPoolExecutor spin-up cost and keeps
        # `-v` log output in chunk order for single-chunk audits.
        for idx in range(len(ranges)):
            chunk_audits[idx] = _audit_one_chunk(idx)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        log.info("dispatching %d chunks with up to %d in parallel",
                 len(ranges), max_parallel)
        with ThreadPoolExecutor(max_workers=max_parallel,
                                thread_name_prefix="chunk") as ex:
            futures = {ex.submit(_audit_one_chunk, i): i
                       for i in range(len(ranges))}
            for fut in as_completed(futures):
                idx = futures[fut]
                chunk_audits[idx] = fut.result()

    assert all(a is not None for a in chunk_audits), (
        "chunk_audits has unfilled slots; parallel dispatch bug"
    )
    chunk_audits_filled: list[dict[str, Any]] = [
        a for a in chunk_audits if a is not None
    ]

    merged = merge_chunk_audits(chunk_audits_filled, ranges)

    # Synthesize a coherent summary; concatenated original kept as fallback.
    synthesized = synthesize_summary(cfg, merged, len(pages))
    if synthesized:
        merged["_chunk_summary"] = merged["summary"]  # preserve original
        merged["summary"] = synthesized

    return merged


# Synthesis prompt: text-only, no schema, return any non-empty text.
_SYNTHESIS_SYSTEM = (
    "You are consolidating a QA audit that was run in multiple chunks. "
    "Given the per-chunk summaries and the final de-duplicated findings, "
    "write ONE coherent 2-3 sentence summary of the whole execution. Do "
    "NOT mention chunks, chunk numbers, or the audit process itself. Do "
    "NOT list individual findings — those appear separately in the report. "
    "Just describe, in plain language, how the execution went overall. "
    "Respond with ONLY the summary text — no JSON, no markdown, no quotes."
)


def synthesize_summary(
    cfg: AuditConfig,
    merged: dict[str, Any],
    total_pages: int,
) -> str | None:
    """Collapse per-chunk summaries into one paragraph (text-only LLM Runtime call).

    Returns the synthesized text or None on failure (caller keeps the
    concatenated summary). ~2-5s vs the image-bearing audit calls.
    """
    chunk_summary = merged.get("summary") or ""
    findings = merged.get("findings") or []
    verdict = merged.get("overall_verdict", "concerns")

    # Compact finding list for the prompt — enough signal, no image data.
    finding_lines = []
    for f in findings[:20]:  # cap to keep the prompt small
        finding_lines.append(
            f"- [{f.get('severity', '?')}] "
            f"page {f.get('page', '?')}, step {f.get('step_index', '?')}: "
            f"{f.get('description', '')[:300]}"
        )
    findings_block = "\n".join(finding_lines) if finding_lines else "(no findings)"

    user_text = (
        f"Overall verdict: {verdict}\n"
        f"Total pages audited: {total_pages}\n\n"
        f"Per-chunk summaries:\n{chunk_summary}\n\n"
        f"Final findings:\n{findings_block}\n\n"
        "Now write the consolidated 2-3 sentence summary."
    )

    try:
        # Deferred import — auditor.py re-exports from this module.
        from auditor import invoke_model
        raw = invoke_model(
            replace(cfg, system_prompt=_SYNTHESIS_SYSTEM),
            [{"type": "text", "text": user_text}],
        )
    except Exception as e:
        log.warning("summary synthesis failed: %s — keeping concatenated summary", e)
        return None

    # Pull text from the response. Same content-blocks shape as the audit call.
    blocks = raw.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    # Strip surrounding quotes the model sometimes adds despite instructions.
    if text.startswith('"') and text.endswith('"') and len(text) > 1:
        text = text[1:-1].strip()

    if not text:
        log.warning("summary synthesis returned empty text — keeping concatenated summary")
        return None
    return text
