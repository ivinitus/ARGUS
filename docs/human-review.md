# Human review and assignments

Start `argus serve` and open **Review cases** in a report's navigation. The local
server uses the process owner's username; set `ARGUS_OPERATOR_NAME` before
starting it to use your own Jira username. This is a single-user localhost app,
not a remotely authenticated multiuser service.

In **Manage reviewers and assignments**, enter Jira usernames. **Preview
allocation** resolves active identities through Jira and shows the proposed
split. Executors cannot review their own executions. **Save these assignments**
activates the exact preview. Unknown executor identities remain unassigned.
Allocation balances counts while preserving started and completed work where
possible. Removing a reviewer redistributes unfinished work with its notes and
history; completed reviews retain their original reviewer.

Filter by reviewer, run, verdict, or state. Choose an execution, **Start review**,
inspect the steps and evidence, then select Confirm, Reject, or Needs investigation
for each finding. Rejected findings require a reason. Completion requires complete
analysis, steps/evidence, and resolved decisions. Only the configured local actor
can save cases assigned to that actor. Completion records a human decision; it
never changes the automated verdict or Jira.

Records and immutable evidence snapshots live below `general.state_dir`. SQLite
transactions protect review versions, history, and idempotent retries. Changes to
reviewed evidence reopen the affected execution; unchanged reviewed content keeps
its decisions. Keep this state directory when updating the application.

## Local API

Routes are under `/teams/{team}/reviews/{folder}`:

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `queue` | Filtered queue, loads, and versions |
| GET | `items/{execution}` | Evidence, current decisions, history |
| POST | `preview` | Validate reviewers and propose assignments |
| POST | `allocate` | Activate the exact allocation preview |
| POST | `transfer` | Transfer unfinished work with a reason |
| POST | `items/{execution}` | Save progress or completed review |

POST requests require the exact local Host and Origin, JSON content,
`X-Argus-Request: 1`, and a 32-character lowercase hexadecimal `Idempotency-Key`.
The server derives the actor from its local configuration, never request JSON.
Version conflicts return 409 so clients can reconcile current state.
