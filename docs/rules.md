# Rules and Prompt

ARGUS combines model findings with deterministic execution and folder rules.
Runtime implementations are the source of truth:

- `src/argus/domain/workflow_rules.py`: metadata and history rules
- `src/argus/domain/folder_coverage.py`: folder coverage rules
- `src/argus/pipeline/env_check.py`: environment classification
- `src/argus/pipeline/device_check.py`: device-family classification
- `src/argus/pipeline/auditor_prompts.py`: canonical and replay prompts

## Execution Rules

| ID | Purpose |
|---|---|
| R0 | Report unresolved status IDs that degrade rule evaluation. |
| R1 | Flag an unexplained step left In Progress. |
| R2 | Flag Pass overall with a failed step. |
| R2b | Flag Pass overall when every step is Not Executed. |
| R3 | Deprecated; retained only for historical compatibility. |
| R4 | Flag Fail overall without a failed or blocked step. |
| R5 | Surface a Pass step whose comment describes an issue. |
| R6 | Require evidence or explanation for Passed With Issue. |
| R7 | Require a failed step for Passed With Issue. |
| R8 | Require a blocked step for Blocked overall. |
| R9 | Detect production/preproduction environment mismatches. |
| R10 | Surface defect references pasted as comments instead of trace links. |
| R11 | Require defect documentation for failed or PWI steps. |
| R12 | Flag an executed item with no auditable evidence. |
| R13 | Detect assigned-marketplace and observed-marketplace mismatch. |
| R14 | Detect a status break from the execution's recent history (emitted as `R14a` after a pass streak and `R14b` after a fail streak). |
| R16 | Require documentation for Fail or Blocked overall. |
| R17 | Detect a case that ran previously but is now non-executed. |
| R18 | Surface Not Applicable as a first-class audited state. |
| R19 | Detect an assigned and observed device-family mismatch. |
| R20 | Surface Blocked, escalating recurring or undocumented blockers. |
| R21 | Surface unfinished In Progress executions. |
| R22 | Remind executed tests to record the test account. |

R9 also emits `R9-amb` for missing environment evidence and `R9-susp`
for uncertain hostname readings. Hover and bottom status URLs remain
diagnostic data; they cannot prove active production navigation.

Environment and marketplace checks use the selected profile's configured hosts
and expected markers. Only source-linked active address-bar readings establish
navigation. Hover/status links, page-body markers, and unconfirmed readings cannot
prove a marketplace switch. Missing or ambiguous evidence remains a review concern.

Profiles can disable environment checks for non-web applications, permit genuinely
empty placeholder steps, and configure the R22 test-account check as `disabled`,
`advisory`, or `enforced`. The profile templates contain no private test-case
exceptions or organization-specific production allowances. Use explicit policy
settings when your own test plan requires an exception.

External defect hosts and linked-evidence policies are configurable. Recognized
external documents are marked for human review rather than downloaded implicitly.
They do not erase independent visible contradictions.

`argus backfill-reader-policy` previews a local migration of existing reader
results; `--apply` makes a backed-up update without rerunning models. Use
`argus rerender --team reader` to regenerate affected reports.

## Folder Rules

Folder findings are written to `_coverage.json` rather than an execution's
`audit.json`.

| ID | Purpose |
|---|---|
| R15a | Planned item was not executed. |
| R15b | A case ran in some required marketplaces but is missing in others. |
| R15c | Assigned tester completion is below the policy threshold. |

## Verdict Reconciliation

Deterministic findings and model findings share one schema. During merge:

- A high-severity finding forces `fail`.
- A medium-severity finding upgrades `pass` to `concerns`.
- A low-severity finding is advisory.
- Existing verdicts are never demoted.

Synthetic R12, R18, R20, and R21 audits skip the model when the state can be
represented deterministically.

## Configuration

`[policy].enabled_rules` is an allowlist when non-empty. An empty list enables
all registered rules. `disabled_rules` and `rule_severities` accept either an
exact sub-rule (`R14a`) or a family id (`R14`), which then applies to every
sub-rule of that family; R0 keeps its fixed HIGH severity. Team profiles also configure defect-key prefixes,
marketplace suffixes, and storefront matching.

Prompt changes must be made in `auditor_prompts.py` and evaluated against the
operator's local datasets under `calibration/`. Those datasets are not tracked
because they contain real execution identifiers; prompt text is intentionally
not duplicated in documentation.
