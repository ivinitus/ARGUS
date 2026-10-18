# Public repository migration

The public edition was created as a new repository from the application files,
including current working-tree improvements. The original workspace is retained
separately. Its Git history, remotes, local configuration, evidence, audit output,
credentials, deployment packages, and investigation reports were not copied.

| Previous integration | Public replacement | Reason |
| --- | --- | --- |
| Private build plugin and package registry | Standard Hatchling and public Python dependencies | Anyone can install and build the package. |
| Internal build/promotion definitions | GitHub Actions test, lint, and package workflow | Verification runs outside the original build fleet. |
| Corporate container registry | Official Python image with non-root runtime | No private registry credentials are needed. |
| Cloud model SDK and account profiles | Configurable public Messages API | Model access uses a user-provided API key. |
| Hosted video inference and object storage | Local video decoding and timestamped image sampling | Video analysis does not require cloud storage. |
| Cloud databases, leases, and secrets | Existing local files/SQLite and environment or PAT file credentials | Personal operation is self-contained. |
| Corporate login, tunnels, and authorization services | Local loopback access and explicit Slack member allowlists | The public app has no corporate identity dependency. |
| Company team profiles, project keys, users, domains, prompts | Generic profiles and invented test fixtures | Organization-specific policy/data is not distributed. |
| Bundled corporate fonts and vendor dashboard styles | System fonts and standalone CSS | The presentation assets are independent. |
| Cloud billing queries | Saved token usage plus explicitly configured rates | Costs remain transparent without account access. |

Deterministic audit rules, evidence scope, chunking, finding identities, report
filtering, and saved-audit formats are preserved where independent of the removed
infrastructure. Profiles intentionally omit private policies and exceptions;
configure your own before auditing real evidence.

Public API outputs are not guaranteed to match a hosted provider byte for byte.
The visual model family is retained, while video now observes sampled frames;
events between frames are unverified. Audio requires an explicitly configured
provider. Missing or failed analysis is reported rather than treated as a pass.

The repository is local and has no configured publishing remote. The public-content
check examines source and built archives for private endpoints, credentials, and
excluded deployment artifacts. It complements the regression suite; it is not a
claim that a live Jira/model integration was exercised without credentials.
