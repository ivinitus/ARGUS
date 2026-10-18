# Team rules

**Team rules** uses the same navigation, typography, controls, and light/dark
appearance as ARGUS reports. Rule publications are stored separately from the
worker image; after the initial deployment, publishing does not deploy code.
Rules, Policies, Test changes, History, and the rule form share compact section
headers and controls. Test selection, comparisons, and publication are grouped
in one panel; history shows each version's publication date and author together.

## Editing

The **Rules** table supports search, type/state filters, and changed-only filtering.
Related checks are grouped under their parent. Partially enabled groups have a
mixed checkbox. Protected engine-health checks cannot be disabled or weakened.

**Use rule default** removes an explicit severity override. A check first inherits
its parent's override, if present, then uses its own evaluator default. The table
shows the effective severity and its source. Conditional defaults explain when
severity changes rather than displaying an incorrect fixed value.

**Create rule** opens one form for conditions, requirements, evidence, and result
settings. Optional case/run restrictions are collapsed. Severity and advisory
mode are separate settings. **Save draft** saves the complete working policy;
**Cancel** discards only edits in the current form. Request failures retain edits.

**Policies** uses one compact panel with grouped environment evidence, execution,
and completion controls. Switches show On/Off; related host, marketplace, defect,
and status settings are grouped in expandable sections. The completion rate is
displayed as a percentage (`90` means 90%) and stored as the original fraction
(`0.9`); the minimum assignment count remains a separate setting.
**Allow empty placeholder steps without a status** retains its exception
semantics: On ignores unused blank rows, while Off includes them in the normal
step checks. Written steps and recorded results remain subject to checks.
**Test-account check mode** offers **Disabled** (no R22 findings), **Advisory**
(informational findings), and **Enforced** (findings can affect the verdict).
The mode and R22's enabled checkbox stay in sync. Re-enabling R22 after choosing
Disabled selects Advisory; choose Enforced explicitly when needed.
The full evidence-policy text remains available under an advanced section.
**View changes** compares the working policy with the published version.

## Testing and publishing

1. Save a draft.
2. Open **Test changes**, choose a saved report, and select 1–10 tests.
3. Choose **Test changes**. Review current-versus-draft verdicts, added/removed/
   changed findings, and separate folder-coverage changes.
4. Review the rule-setting changes, enter a change summary, and select **Publish rules**.

Publication requires a completed test of the exact draft against the current
published version. Editing the draft invalidates its previous test. Concurrent
changes return a conflict and preserve unsaved browser edits.

The report picker uses a protected execution catalog published independently of
human-review data. Each selection identifies the report revision and the input
fingerprint of each test. The worker locates that saved copy and verifies the
fingerprint after copying it. A newer copy of the same execution in another
report cannot silently replace the selection. Missing, changed, or incomplete
inputs prevent publication. Older reports need to be regenerated to create their
selection catalogs.

Tests use isolated output, captured execution inputs, and a frozen history index.
Changes to coverage require a complete saved roster (`policy_items`). The worker
can make Model API calls and occupies the team's normal worker slot. Existing
reports are not modified by testing or publishing.

**History** lists published versions, authors, dates, and change summaries. A
version can be inspected, compared with the current publication, and restored as
a draft. Restoration requires testing and publication before it becomes active.
Published versions remain immutable.

## Local operation

Open **Team rules** from `argus serve`. The process owner (or configured
`ARGUS_OPERATOR_NAME`) edits local rules. SQLite state is stored at
`general.state_dir/rules.sqlite`; `ARGUS_RULE_DATABASE` can select another local
file shared by the editor and its workers. Audits automatically load the published
policy when that file exists.

The first editor read initializes the packaged baseline as version 1. Workers
pin one immutable policy snapshot per run. Preview subprocesses use copied saved
evidence and never replace live reports. Preview failures cannot authorize
publication. Storage failures do not silently fall back to another policy.

The API is local only. It rejects foreign Host/Origin headers and requires JSON,
`X-Argus-Request: 1`, and a 32-character lowercase hexadecimal idempotency key
for changes. Shared public hosting requires a separate authenticated application.

`argus rules-demo` starts a local example workspace for trying the editor without
using production evidence.
