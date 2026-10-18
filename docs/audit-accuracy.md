# Audit accuracy and evaluation

The accuracy checks apply to the selected public profile and configured media
paths. Correctness fixes take effect immediately. A new observation/reconciliation
path is opt-in and must pass a labelled evaluation before rollout.

## Correctness fixes

- Exhausted model repairs and failed chunks produce incomplete analysis. Legacy
  artifacts containing those diagnostics are also treated as incomplete when
  read, even if they previously recorded `analysis_status=complete`.
- Consensus considers HIGH candidates discovered in any of its valid runs.
  Disputed candidates remain visible as medium, low-confidence **Needs review**
  findings; their original severity is retained in diagnostics. Unavailable
  confirmation is not confirmation. Clean first passes still do not trigger
  consensus; the optional requirement reviewer provides a separate coverage check.
- Chunk deduplication and replay signatures preserve content and evidence identity.
  Only exact normalized duplicates at the same evidence location collapse.
  Different wording can remain as separate candidates for review.
- Mentioning a promotion does not excuse a wrong promotional amount. The existing
  narrow “promotional price differs from list price” exception remains.
- A page number alone supplies no high-confidence classification.
- A limitation-only video verdict survives final normalization as Needs review.
  Timestamped findings continue to use the video/audio implementation's references.
- More than four video records is an explicit incomplete-input error; the loader
  no longer silently reviews only the first four.
- Chunked audits require review of global coverage until the optional reviewer
  establishes every requirement. A `max_pages` truncation remains review-required.

Schema v8 preserves `pass`, `concerns`, and `fail` and remains readable alongside
older artifacts. `review_status=required` prevents a Compliant verdict. Findings
with `assessment_status=needs_review` are not confirmed HIGH deviations.
HTML, Markdown, review filtering, and CSV expose the review state.

Audits record model/configuration fingerprints, the complete-context hash, policy
hash when configured through the runtime, metadata hash, and source evidence
hashes. These allow controlled comparisons without storing credentials in
provenance. Configuration changes do not trigger an unbounded fleet re-audit.
Use an explicit execution audit or the existing folder `--force` workflow for
selected re-audits.

## Shared evidence-review contract

```text
Original test plan and team policy
              |
    Per-batch image observations + existing timestamped video/audio observations
              |
    Execution-wide requirement reconciliation
              |
    Preserved contradictions + resolved proof gaps + Needs review
              |
    Existing deterministic rules and final verdict
```

The reviewer builds requirements directly from complete expected-result text
(including compound requirements), with an additional team-policy requirement.
Each observation batch must assess every requirement and cite only supplied
image IDs. The final pass sees the full plan, observations, source inventory,
existing findings, and the available video/audio timeline. Missing rows, unknown
references, malformed output, and token-cap stops receive one bounded repair.
Guardrail interventions propagate; no unguarded fallback is introduced.
Observation, reconciliation, and video assessment use the same credential
redaction and guardrail-span wrapping for tester-authored test-plan text.

Only a fully supported requirement can resolve a prior pure evidence gap.
Explicit requirement IDs also identify objective and team-policy gaps. Legacy
findings use their step index when available; ambiguous step-less gaps remain.
Low-confidence observations cannot become confirmed coverage through summarization;
an observed contradiction keeps the requirement unresolved.
Existing contradictions, deterministic findings, disputed candidates, and audio
capture gaps are preserved. New HIGH claims derived from summaries require human
review. Legacy video summaries without a timestamped timeline remain limited inputs
for this path; their summaries do not prove unseen outcomes.

```toml
[auditor]
evidence_review_mode = "off"    # off | shadow | enforce
max_output_tokens = 16000
thinking_mode = "disabled"     # disabled | provider_default
```

- `off` is the default: no additional model calls.
- `shadow` records `_evidence_review` beside the baseline without changing its
  verdict. Shadow parsing and operational failures are recorded separately using
  the error type without provider payloads; guardrail blocks still propagate.
  Shadow inference contributes to recorded costs.
- `enforce` applies the requirement assessment and preserves unresolved outcomes
  for human review. A failed required reviewer makes the audit incomplete.
- `provider_default` omits the explicit disabled-thinking setting where the
  existing model adapter normally sends it. No reasoning behavior or deterministic
  output is assumed. Compare it against the disabled baseline.

Generic prompt assets remain available for replay. The new stage contracts share
classification, confidence, reference, and uncertainty instructions across teams
while retaining the selected team policy. Forced tool output remains a separate
experiment; this reviewer uses bounded JSON validation.

## Human-labelled benchmark

Target 210 executions: 30 per profile, including Web, Desktop, TABLET,
Mobile, Reader, TV, and PORTAL. Use 20 per profile for development and 10 for
holdout, with test-case families isolated between splits. Previously calibrated
examples belong in development. Include real contradictions, clean cases,
documented defects, localization, evidence gaps, long runs, mixed media, and
audio limitations.

Labels must be supplied by QA reviewers. The implementation does not fabricate
human labels or claim that this benchmark has already been run.

Manifest shape:

```json
{
  "version": 1,
  "configurations": {
    "baseline": {"evidence_review_mode": "off"},
    "candidate": {"evidence_review_mode": "enforce"},
    "reasoning_experiment": {
      "evidence_review_mode": "enforce",
      "thinking_mode": "provider_default"
    }
  },
  "cases": [{
    "id": "checkout-price-control",
    "profile": "mobile",
    "case_family": "checkout-price",
    "split": "development",
    "execution_dir": "/path/to/already-extracted-execution",
    "reviewed_by": [],
    "expected_findings": [{
      "id": "wrong-total",
      "severity": "high",
      "category": "product_issue",
      "step_index": 0,
      "description_pattern": "(?i)(wrong (price|total)|price mismatch)",
      "evidence_refs": ["p1"]
    }]
  }]
}
```

The empty reviewer list deliberately fails validation. Replace the example with
reviewed cases; `expected_findings=[]` explicitly labels a clean case.
Labels cover the final audit output, including deterministic rule, environment,
and device findings as well as model/video findings. Incorrect deterministic
findings count as false positives too; runtime diagnostics are scored separately.
Description patterns and optional step/category/requirement constraints define
human-reviewed matching criteria. Patterns need review for paraphrase coverage;
they are not a general semantic judge. Labels are never sent to the audit model.

```bash
argus evaluate validate benchmark.json
argus evaluate run benchmark.json --split development \
  --repeats 3 --output-dir /path/to/new-development-results
argus evaluate run benchmark.json --split holdout \
  --repeats 3 --output-dir /path/to/new-holdout-results
```

`run` invokes the configured Model API models and incurs inference cost. It writes
to a new directory outside all source executions. Each repeat/configuration has
independent usage tracking. Original audits and evidence remain untouched.
The existing `--config` mechanism can select a local evaluation configuration.
Guardrail failures stop the comparison before another model/configuration runs.

`results.json` retains per-case scores, usage, latency, and provenance;
`metrics.json` reports per-profile and overall precision/recall, HIGH precision,
review-inclusive recall, citation accuracy where labelled, false-clean results,
incomplete runs, p95 latency, repeat instability, and unpriced calls.
Token counts and estimated costs are aggregated separately from missing usage
and unpriced calls, so partial pricing is visible.
One-to-one matching prevents duplicate predictions from inflating recall.

Promotion requires at least ten held-out cases per evaluated profile, three
baseline and candidate repeats, at least 95% confirmed-HIGH precision, no recall
decline, and no false-clean or incomplete results. The baseline must complete the
same cases. Missing provenance or changed source evidence/context invalidates
comparisons. Profiles pass independently; small compatibility samples
cannot qualify. These are pilot gates, not a fleet-wide accuracy guarantee.

No live evaluation, model-weight training, model-default change, or deployment
is implied by installing these changes. Weight fine-tuning remains contingent
on residual errors demonstrated by a labelled benchmark.
