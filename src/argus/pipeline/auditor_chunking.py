"""Chunking, consensus, and per-chunk merging for auditor.run()."""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from argus.domain.finding_identity import finding_identity
from argus.domain.finding_inference import (
    is_contradiction_escalation_candidate, reconcile_verdict, require_review,
)
from argus.pipeline import analysis_status

if TYPE_CHECKING:
    from argus.pipeline.auditor import AuditConfig

log = logging.getLogger("argus.audit")


CONSENSUS_RUNS = 3
CONSENSUS_THRESHOLD = 2


def _is_runtime_invalid(audit: dict[str, Any]) -> bool:
    return analysis_status.model_failure(audit)


def _finding_key(f: dict[str, Any]) -> tuple:
    """Equivalence key for matching findings across consensus runs.

    Matches (severity, step|page) only — descriptions vary even for the
    same issue. With neither anchor set it identity-keys, so an
    uncorroborated unlocated finding can never survive consensus (the
    safe default).
    """
    sev = f.get("severity")
    step = f.get("step_index")
    page = f.get("page")
    if step is not None:
        return ("step", sev, step)
    if page is not None:
        return ("page", sev, page)
    return ("anon", sev, id(f))


def _consensus_finding_key(finding: dict[str, Any]) -> tuple:
    """Use exact page identity for consensus without changing merge dedupe."""
    severity = finding.get("severity")
    step = finding.get("step_index")
    page = finding.get("page")
    if step is not None and page is not None:
        return ("step-page", severity, step, page)
    return _finding_key(finding)


def _classification_key(finding: dict[str, Any]) -> tuple[Any, Any]:
    return finding.get("category"), finding.get("action")


def _select_consensus_representative(
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select the majority classification, preserving defects on a tie."""
    counts: dict[tuple[Any, Any], int] = {}
    for finding in findings:
        key = _classification_key(finding)
        counts[key] = counts.get(key, 0) + 1
    highest = max(counts.values())
    evidence_only = ("insufficient_evidence", "add_evidence")
    winner = next(
        (
            key
            for finding in findings
            if (key := _classification_key(finding)) != evidence_only
            and counts[key] == highest
        ),
        evidence_only,
    )
    return next(
        finding
        for finding in findings
        if _classification_key(finding) == winner
    )


def _finding_terms(finding: dict[str, Any]) -> set[str]:
    text = " ".join(
        str(finding.get(field) or "")
        for field in ("title", "description")
    ).lower()
    stop_words = {
        "and",
        "for",
        "from",
        "not",
        "page",
        "shown",
        "shows",
        "step",
        "that",
        "the",
        "this",
        "with",
    }
    return set(re.findall(r"[a-z0-9]{3,}", text)) - stop_words


def _assign_confirmation_findings(
    references: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Find a polynomial max-cardinality, then semantic-weight assignment."""
    semantic_weights: dict[tuple[int, int], int] = {}
    for reference_idx, reference in enumerate(references):
        reference_terms = _finding_terms(reference)
        for candidate_idx, candidate in enumerate(candidates):
            candidate_terms = _finding_terms(candidate)
            overlap = len(reference_terms & candidate_terms)
            if overlap == 0:
                continue
            union = len(reference_terms | candidate_terms)
            same_class = int(
                _classification_key(reference)
                == _classification_key(candidate)
            )
            semantic_weights[(reference_idx, candidate_idx)] = (
                overlap * 100_000 // max(union, 1)
                + overlap * 100
                + same_class
            )

    # Fully reworded findings can still match when the model preserves the
    # structured identity: exact page, title, and classification.
    if (
        not semantic_weights
        and len(references) == 1
        and len(candidates) == 1
        and isinstance(references[0].get("page"), int)
        and references[0].get("page") == candidates[0].get("page")
        and str(references[0].get("title") or "").strip().casefold()
        == str(candidates[0].get("title") or "").strip().casefold()
        and bool(str(references[0].get("title") or "").strip())
        and _classification_key(references[0])
        == _classification_key(candidates[0])
    ):
        semantic_weights[(0, 0)] = 1

    if not semantic_weights:
        return {}

    real_rows = len(references)
    real_columns = len(candidates)
    size = real_rows + real_columns
    max_semantic = max(semantic_weights.values())
    cardinality_base = (
        (max_semantic + 1) * (min(real_rows, real_columns) + 1)
    )
    invalid_weight = -cardinality_base * (size + 1)
    matrix = [[0] * size for _ in range(size)]
    for reference_idx in range(real_rows):
        for candidate_idx in range(real_columns):
            semantic = semantic_weights.get(
                (reference_idx, candidate_idx)
            )
            matrix[reference_idx][candidate_idx] = (
                cardinality_base + semantic
                if semantic is not None
                else invalid_weight
            )

    # Hungarian algorithm for maximum-weight square assignment, O(n^3).
    row_potential = [0] * (size + 1)
    column_potential = [0] * (size + 1)
    column_match = [0] * (size + 1)
    previous_column = [0] * (size + 1)
    infinity = 10 ** 30
    for row in range(1, size + 1):
        column_match[0] = row
        current_column = 0
        min_cost = [infinity] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[current_column] = True
            current_row = column_match[current_column]
            delta = infinity
            next_column = 0
            for column in range(1, size + 1):
                if used[column]:
                    continue
                cost = (
                    -matrix[current_row - 1][column - 1]
                    - row_potential[current_row]
                    - column_potential[column]
                )
                if cost < min_cost[column]:
                    min_cost[column] = cost
                    previous_column[column] = current_column
                if min_cost[column] < delta:
                    delta = min_cost[column]
                    next_column = column
            for column in range(size + 1):
                if used[column]:
                    row_potential[column_match[column]] += delta
                    column_potential[column] -= delta
                else:
                    min_cost[column] -= delta
            current_column = next_column
            if column_match[current_column] == 0:
                break
        while True:
            prior = previous_column[current_column]
            column_match[current_column] = column_match[prior]
            current_column = prior
            if current_column == 0:
                break

    pairs = [
        (column_match[column] - 1, column - 1)
        for column in range(1, size + 1)
        if column_match[column] > 0
    ]

    return {
        reference_idx: candidates[candidate_idx]
        for reference_idx, candidate_idx in pairs
        if (
            reference_idx < real_rows
            and candidate_idx < real_columns
            and (reference_idx, candidate_idx) in semantic_weights
        )
    }


def _map_confirmation_runs(
    run_one: Callable[[int], dict[str, Any]],
    run_indices: list[int],
) -> dict[int, dict[str, Any]]:
    """Run the consensus confirmation passes concurrently, keyed by run index.

    Each pass is an independent audit of the same content, so they are
    dispatched together (the process-wide model API in-flight limiter bounds
    the fan-out). Results are keyed by run index so the caller's merge
    order — and therefore its output — is identical to a sequential walk.
    The first failure is re-raised after every pass has finished.
    """
    if len(run_indices) <= 1:
        return {run_idx: run_one(run_idx) for run_idx in run_indices}
    with ThreadPoolExecutor(
        max_workers=len(run_indices),
        thread_name_prefix="consensus",
    ) as executor:
        futures = {
            run_idx: executor.submit(run_one, run_idx)
            for run_idx in run_indices
        }
        return {run_idx: futures[run_idx].result() for run_idx in run_indices}


def audit_with_consensus(
    cfg: AuditConfig,
    content: list[dict[str, Any]],
    *,
    n_pages: int | None = None,
    max_step_index: int | None = None,
) -> dict[str, Any]:
    """Re-run audits with high-severity findings to filter to >=2/3 consensus.

    Each run's findings are grounded BEFORE voting so runs that
    hallucinate the same out-of-range anchor cannot corroborate each
    other.
    """
    from argus.pipeline.auditor import ground_findings, invoke_and_parse

    def _grounded(audit: dict[str, Any]) -> dict[str, Any]:
        if _is_runtime_invalid(audit):
            return audit
        return ground_findings(
            audit, n_pages=n_pages, max_step_index=max_step_index)

    first = _grounded(invoke_and_parse(cfg, content))

    if not cfg.consensus_enabled:
        return first

    if _is_runtime_invalid(first):
        return first

    findings = first.get("findings") or []
    high_findings = [f for f in findings if f.get("severity") == "high"]

    if not high_findings:
        return first

    log.info("consensus: %d high finding(s) in run 1, running %d confirmation passes",
             len(high_findings), CONSENSUS_RUNS - 1)

    confirmation_runs = list(range(2, CONSENSUS_RUNS + 1))
    extra_by_run = _map_confirmation_runs(
        lambda _run_idx: _grounded(invoke_and_parse(cfg, content)),
        confirmation_runs,
    )

    # A discovery in a later run is as eligible as a discovery in run 1.
    # Assignment is one-to-one within each run, so duplicate claims cannot vote
    # twice and unrelated problems on the same step remain separate.
    clusters: list[list[tuple[int, dict[str, Any]]]] = [
        [(1, finding)] for finding in high_findings
    ]
    valid_runs = 1
    non_high = [f for f in findings if f.get("severity") != "high"]
    non_high_seen = {finding_identity(f) for f in non_high}
    for run_idx in confirmation_runs:
        extra = extra_by_run[run_idx]
        if _is_runtime_invalid(extra):
            log.warning("consensus run %d produced invalid output; skipping",
                        run_idx)
            continue
        valid_runs += 1
        extra_findings = extra.get("findings") or []
        by_key: dict[tuple, list[dict[str, Any]]] = {}
        for finding in extra_findings:
            if finding.get("severity") == "high":
                by_key.setdefault(_consensus_finding_key(finding), []).append(finding)
            elif finding_identity(finding) not in non_high_seen:
                non_high.append(finding)
                non_high_seen.add(finding_identity(finding))
        for key, candidates in by_key.items():
            indices = [
                idx for idx, cluster in enumerate(clusters)
                if _consensus_finding_key(cluster[0][1]) == key
            ]
            assignments = _assign_confirmation_findings(
                [clusters[idx][0][1] for idx in indices], candidates,
            )
            assigned = {id(candidate) for candidate in assignments.values()}
            for local_idx, candidate in assignments.items():
                clusters[indices[local_idx]].append((run_idx, candidate))
            clusters.extend(
                [(run_idx, candidate)] for candidate in candidates
                if id(candidate) not in assigned
            )

    # A later text heuristic must not turn a non-HIGH vote into a confirmed
    # deviation after consensus has finished.
    final_findings = [
        require_review(
            finding, "The model did not classify this contradiction as HIGH; "
            "human review is required before escalating it.",
        ) if is_contradiction_escalation_candidate(finding) else finding
        for finding in non_high
    ]
    consensus_trace: list[dict[str, Any]] = []
    pending = 0
    for cluster in clusters:
        matching = [finding for _, finding in cluster]
        f = _select_consensus_representative(matching)
        total_votes = len(cluster)
        kept = total_votes >= CONSENSUS_THRESHOLD
        trace = {
            "step_index": f.get("step_index"),
            "page": f.get("page"),
            "description": f.get("description"),
            "votes": total_votes,
            "runs": valid_runs,
            "kept": kept,
            "discovered_in_run": cluster[0][0],
        }
        if valid_runs == 1:
            trace["unavailable"] = True
        if kept:
            trace["selected_category"] = f.get("category")
            trace["selected_action"] = f.get("action")
            final_findings.append(f)
        else:
            pending += 1
            final_findings.append(require_review(
                f, f"Confirmed in {total_votes} of {valid_runs} valid audit runs; "
                "human review is required before treating this as a deviation.",
            ))
        consensus_trace.append(trace)

    summary = first.get("summary", "")
    if pending:
        summary = (
            f"{summary}\n\n[Consensus: {pending} candidate(s) retained for "
            f"human review after {valid_runs} valid audit run(s).]"
        ).strip()

    result = dict(first)
    result["summary"] = summary
    result["findings"] = final_findings
    result["_consensus"] = consensus_trace
    return reconcile_verdict(result)


_PAGE_REF_RE = re.compile(
    r"(?i)\b(pages?)\s+(\d+(?:\s*(?:[-–—,]|and)\s*\d+)*)"
)


def _rewrite_page_refs(text: str, chunk_start: int, chunk_end: int) -> str:
    """Rewrite chunk-local 'page N' mentions in `text` to global indices."""
    if not text or not isinstance(text, str):
        return text

    def _translate_number(match: re.Match) -> str:
        n = int(match.group(0))
        global_n = chunk_start + n
        if global_n > chunk_end:
            global_n = chunk_end
        return str(global_n)

    def _sub(match: re.Match) -> str:
        prefix = match.group(1)
        numbers_block = match.group(2)
        rewritten = re.sub(r"\d+", _translate_number, numbers_block)
        return f"{prefix} {rewritten}"

    return _PAGE_REF_RE.sub(_sub, text)


def plan_chunks(
    n_pages: int,
    chunk_size: int,
    chunk_overlap: int,
) -> list[tuple[int, int]]:
    """Return a list of (start, end) 0-indexed half-open page ranges."""
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


def plan_chunks_bytes(
    page_bytes: list[int],
    chunk_size: int,
    chunk_overlap: int,
    byte_budget: int,
) -> list[tuple[int, int]]:
    """Plan overlapping chunks within page-count and byte limits."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be in [0, chunk_size)")
    if byte_budget <= 0:
        raise ValueError("byte_budget must be positive")
    n_pages = len(page_bytes)
    if n_pages <= 0:
        return []
    chunks: list[tuple[int, int]] = []
    start = 0
    while start < n_pages:
        end = start
        running = 0
        while end < n_pages and (end - start) < chunk_size:
            size = page_bytes[end]
            if end > start and running + size > byte_budget:
                break
            running += size
            end += 1
        chunks.append((start, end))
        if end >= n_pages:
            break
        # Scale the overlap with the chunk actually taken: a byte-limited
        # short chunk keeps the configured overlap *ratio* rather than the
        # absolute page count, which used to degrade into one-page advances
        # that resent every page many times over.
        length = end - start
        effective_overlap = min(chunk_overlap, (length * chunk_overlap) // chunk_size)
        start += max(length - effective_overlap, 1)
    return chunks


def _combine_verdicts(verdicts: list[str]) -> str:
    """Worst-case verdict combination: fail > concerns > pass."""
    if "fail" in verdicts:
        return "fail"
    if "concerns" in verdicts:
        return "concerns"
    return "pass"


def _is_chunk_absence_finding(finding: dict[str, Any]) -> bool:
    """Return True for a model-classified partial-evidence-only finding."""
    if finding.get("source") in {"rule", "env_check", "device_check"}:
        return False
    return (
        finding.get("category") == "insufficient_evidence"
        and finding.get("action") == "add_evidence"
    )


def _verdict_from_merged_findings(
    findings: list[dict[str, Any]],
    *,
    has_invalid_chunk: bool,
) -> str:
    """Recompute the global verdict after chunk findings are adjudicated."""
    if any(finding.get("severity") == "high" for finding in findings):
        return "fail"
    if findings or has_invalid_chunk:
        return "concerns"
    return "pass"


def merge_chunk_audits(
    chunk_audits: list[dict[str, Any]],
    chunk_ranges: list[tuple[int, int]],
) -> dict[str, Any]:
    """Merge N chunk audits into a single audit dict."""
    if len(chunk_audits) != len(chunk_ranges):
        raise ValueError("chunk_audits and chunk_ranges must be same length")

    merged_findings: list[dict[str, Any]] = []
    seen_keys: set = set()
    summary_parts: list[str] = []
    evidence_by_step: dict[int, dict[str, Any]] = {}
    diagnostics: dict[str, Any] = {}
    dropped_chunk_absence = 0
    unexplained_verdict_chunks: list[int] = []

    for idx, (audit, (start, end)) in enumerate(zip(chunk_audits, chunk_ranges, strict=False)):
        summary_parts.append(
            f"[Chunk {idx + 1}/{len(chunk_audits)} "
            f"(pages {start + 1}-{end})]: {audit.get('summary', '')}"
        )
        for dk in (*analysis_status.MODEL_FAILURE_KEYS, "_consensus"):
            if dk in audit:
                diagnostics.setdefault(f"chunk_{idx + 1}", {})[dk] = audit[dk]

        chunk_findings = audit.get("findings") or []
        chunk_nonpass = audit.get("overall_verdict") in {"fail", "concerns"}
        if chunk_nonpass and not chunk_findings:
            unexplained_verdict_chunks.append(idx + 1)
            diagnostics.setdefault(f"chunk_{idx + 1}", {})[
                "_verdict_without_findings"
            ] = audit.get("overall_verdict")

        chunk_keys: set = set()
        chunk_dropped_absence = 0
        chunk_retainable_findings = 0

        for f in chunk_findings:
            if _is_chunk_absence_finding(f):
                dropped_chunk_absence += 1
                chunk_dropped_absence += 1
                continue
            chunk_retainable_findings += 1
            local_page = f.get("page")
            rewritten = dict(f)
            if isinstance(local_page, int) and local_page >= 1:
                if start == end:
                    rewritten["page"] = None
                else:
                    rewritten["page"] = start + local_page
                    if rewritten["page"] > end:
                        log.warning(
                            "chunk %d: finding page %d exceeds chunk end "
                            "%d; clamping to end",
                            idx + 1, rewritten["page"], end,
                        )
                        rewritten["page"] = end
            desc = rewritten.get("description")
            if isinstance(desc, str):
                rewritten["description"] = _rewrite_page_refs(desc, start, end)
            key = finding_identity(rewritten)
            if key in seen_keys and key not in chunk_keys:
                continue
            seen_keys.add(key)
            chunk_keys.add(key)
            merged_findings.append(rewritten)

        if (
            chunk_nonpass
            and chunk_findings
            and chunk_retainable_findings == 0
            and chunk_dropped_absence
        ):
            unexplained_verdict_chunks.append(idx + 1)
            diagnostics.setdefault(f"chunk_{idx + 1}", {})[
                "_non_pass_after_absence_filter"
            ] = {
                "verdict": audit.get("overall_verdict"),
                "dropped_findings": chunk_dropped_absence,
            }

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
                    if start == end:
                        continue
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

    if unexplained_verdict_chunks:
        chunk_labels = ", ".join(
            str(chunk) for chunk in unexplained_verdict_chunks
        )
        merged_findings.append({
            "severity": "medium",
            "page": None,
            "step_index": None,
            "title": "Chunk verdict lacked retainable findings",
            "description": (
                f"ARGUS audit chunk(s) {chunk_labels} returned a non-pass "
                "verdict without findings that can safely support a global "
                "verdict. Retry the audit before treating this execution as "
                "clean."
            ),
            "source": "runtime",
            "rule": "R-runtime",
            "category": "policy_exception",
            "confidence": "high",
            "action": "review_manually",
            "advisory": False,
        })

    has_invalid_chunk = any(
        analysis_status.model_failure(row) or any(
            key in row
            for key in (
                "_verdict_without_findings",
                "_non_pass_after_absence_filter",
            )
        )
        for row in diagnostics.values()
    )
    out: dict[str, Any] = {
        "overall_verdict": _verdict_from_merged_findings(
            merged_findings,
            has_invalid_chunk=has_invalid_chunk,
        ),
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
    if dropped_chunk_absence:
        out["_dropped_chunk_absence_findings"] = dropped_chunk_absence
    return reconcile_verdict(out)


def audit_chunked(
    cfg: AuditConfig,
    steps_text: str,
    pages: list[Path],
    step_attachments: list[tuple[Path, int | None]] | None = None,
    max_step_index: int | None = None,
) -> dict[str, Any]:
    """Audit a large execution by splitting images into chunks and merging."""
    from argus.pipeline.auditor import build_message_content

    step_attachments = step_attachments or []

    bytes_per_mb = 1024 * 1024
    page_byte_budget = cfg.chunk_page_byte_budget_mb * bytes_per_mb
    attachment_byte_budget = (
        cfg.chunk_attachment_byte_budget_mb * bytes_per_mb)

    def _size_of(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    ranges = plan_chunks_bytes(
        [_size_of(p) for p in pages],
        cfg.chunk_size, cfg.chunk_overlap, page_byte_budget)

    def _pack_attachments_by_bytes(items: list) -> list[list]:
        if not items:
            return []
        batches: list[list] = []
        current: list = []
        running = 0
        for pair in items:
            path = pair[0]
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if current and running + size > attachment_byte_budget:
                batches.append(current)
                current, running = [], 0
            current.append(pair)
            running += size
        if current:
            batches.append(current)
        return batches

    attach_batches = _pack_attachments_by_bytes(step_attachments)

    while len(ranges) < len(attach_batches):
        tail = len(pages)
        ranges.append((tail, tail))
    if not ranges:
        ranges = [(0, 0)]

    def _attachments_for(idx: int) -> list:
        return attach_batches[idx] if idx < len(attach_batches) else []

    log.info("chunking %d pages + %d attachments into %d chunks "
             "(%d attachment batch(es), size=%d, overlap=%d)",
             len(pages), len(step_attachments), len(ranges),
             len(attach_batches), cfg.chunk_size, cfg.chunk_overlap)

    def _audit_one_chunk(idx: int) -> dict[str, Any]:
        start, end = ranges[idx]
        chunk_pages = pages[start:end]
        chunk_attachments = _attachments_for(idx)
        log.info("auditing chunk %d/%d (pages %d-%d, %d page img + "
                 "%d attachment img)",
                 idx + 1, len(ranges), start + 1, end, len(chunk_pages),
                 len(chunk_attachments))
        content = build_message_content(steps_text, chunk_pages,
                                        step_attachments=chunk_attachments)
        if end > start and chunk_attachments:
            coverage = (f"pages {start + 1}-{end} of {len(pages)} total "
                        f"plus {len(chunk_attachments)} tester-attached "
                        "image(s)")
        elif end > start:
            coverage = f"pages {start + 1}-{end} of {len(pages)} total"
        else:
            coverage = (f"{len(chunk_attachments)} tester-attached image(s) "
                        "(no PDF pages in this chunk)")
        content.insert(0, {
            "type": "text",
            "text": (
                f"NOTE: You are auditing CHUNK {idx + 1} of {len(ranges)} "
                f"from a large execution. This chunk contains {coverage}. "
                "Report only a contradiction visibly present in supplied "
                "evidence. Do NOT report missing, absent, unproven, or "
                "incomplete evidence, and do not infer that a required "
                "surface is absent from the full execution — other chunks "
                "cover the rest. Every finding MUST include category, action, "
                "and confidence. Use category=insufficient_evidence with "
                "action=add_evidence only for a pure evidence-gap finding; "
                "never use that pair when the same finding reports a visible "
                "defect or contradiction. Number pages 1-N "
                "within this chunk; we'll renumber to the global index "
                "after merging."
            ),
        })
        return audit_with_consensus(
            cfg, content,
            n_pages=end - start,
            max_step_index=max_step_index,
        )

    max_parallel = max(1, min(cfg.chunk_max_parallel, len(ranges)))
    chunk_audits: list[dict[str, Any] | None] = [None] * len(ranges)
    if max_parallel == 1 or len(ranges) == 1:
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
    if len(ranges) > 1:
        # Contradiction-only chunk audits cannot prove global coverage. The
        # optional full-evidence reviewer can resolve this flag after checking
        # every requirement; otherwise it remains visible to a human reviewer.
        merged["review_status"] = "required"
        merged["_coverage_review_required"] = True
        merged = reconcile_verdict(merged)

    synthesized = synthesize_summary(cfg, merged, len(pages))
    if synthesized:
        merged["_chunk_summary"] = merged["summary"]
        merged["summary"] = synthesized

    return merged


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
    """Collapse per-chunk summaries into one paragraph (text-only model API call)."""
    chunk_summary = merged.get("summary") or ""
    findings = merged.get("findings") or []
    verdict = merged.get("overall_verdict", "concerns")

    finding_lines = []
    from argus.pipeline import model_client

    for f in findings[:20]:
        finding_lines.append(
            f"- [{model_client.guard_content(str(f.get('severity', '?')))}] "
            f"page {f.get('page', '?')}, step {f.get('step_index', '?')}: "
            f"{model_client.guard_content(str(f.get('description', ''))[:300])}"
        )
    findings_block = "\n".join(finding_lines) if finding_lines else "(no findings)"

    user_text = (
        f"Overall verdict: {model_client.guard_content(str(verdict))}\n"
        f"Total pages audited: {total_pages}\n\n"
        f"Per-chunk summaries:\n{model_client.guard_content(str(chunk_summary))}\n\n"
        f"Final findings:\n{findings_block}\n\n"
        "Now write the consolidated 2-3 sentence summary."
    )

    try:
        from argus.pipeline.auditor import (_make_model_client, _apply_temperature,
                             _apply_thinking)
        client = _make_model_client(
            cfg, cache_key="summary")
        body = _apply_thinking(_apply_temperature({

            "max_tokens": 512,
            "system": _SYNTHESIS_SYSTEM,
            "messages": [{"role": "user",
                          "content": [{"type": "text", "text": user_text}]}],
        }, cfg), cfg)
        resp = model_client.invoke_model(
            client,
            model_id=cfg.model_id,
            body=json.dumps(body),
            content_type="application/json",
            accept="application/json",
            limit_inflight=True,
        )
        raw = json.loads(resp["body"].read())
        cfg.usage_tracker.record_response(
            raw, model_id=cfg.model_id, operation="summary")
    except Exception as e:
        log.warning("summary synthesis failed: %s — keeping concatenated summary", e)
        return None

    blocks = raw.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()

    if text.startswith('"') and text.endswith('"') and len(text) > 1:
        text = text[1:-1].strip()

    if not text:
        log.warning("summary synthesis returned empty text — keeping concatenated summary")
        return None
    return text
