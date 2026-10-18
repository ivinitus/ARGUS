# Local controls

`argus serve` hosts reports, human review, and team rule editing on loopback.
It is designed for a personal workstation. It does not provide internet-facing
login or delegated operator authentication.

The local service exposes `/session` for the configured process identity,
`/teams/{team}/rules` for rule drafts, previews, publication and history, and
`/teams/{team}/reviews/{folder}` for human review. Requests are limited to mounted
profiles. Mutations require a matching loopback Host/Origin, JSON content,
`X-Argus-Request: 1`, and idempotency keys. Requests from another origin are denied.

Start audits with `argus audit`, schedule them with `argus cycle`, and use
`argus control` for local pause/resume. The optional Slack bot has a separately
configured member allowlist. These commands share the same local locks and state.

See [Human review](human-review.md) and [Team rules](team-rules.md) for workflows.
