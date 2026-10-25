# Native workflow development interface

Status: Linux implementation, offline regressions, and one real Claude failure
path. Successful bidirectional native acceptance has not passed. `task-dispatch`
checks `doctor.runtime_ready` and currently refuses execution. These instructions
describe the implemented interface, not permission to bypass that gate.

The initiating main agent drives the loop in its existing conversation. There is
no background reviewer, extra Claude model, or automatic self-approval. The
initiator must remain active to inspect delivery and decide the next step.

## Bind the two existing sessions

Use exact session IDs, never `--last`. Both native views and the single owner must
already be attached and verified. Creating sessions, completing native onboarding,
and opening every surface automatically are not yet packaged as an installer.

- Codex executes in its Desktop owner. Transport JSON contains exactly
  `{"kind":"codex-desktop","rollout_path":"/absolute/original-rollout.jsonl"}`.
- Claude executes in its native 2.1.295 TUI with task-local hooks. Transport JSON
  contains exactly `{"kind":"claude-native","owner_state_dir":"/absolute/private-owner"}`.

Prepare Claude's hooks before starting or normally resuming that idle TUI:

```sh
python3 <skill>/scripts/bridge.py prepare-claude-owner \
  --directory /absolute/new-private-owner --session-id EXACT_CLAUDE_UUID
```

This returns `plugin_dir` and `launch_args`; it does not start Claude. Validate the
plugin with the supported native CLI's `plugin validate`, then use the returned
`--plugin-dir` when starting the one owner with `--resume EXACT_CLAUDE_UUID`.
Do not start a second owner for an already-running session. Native plugin trust,
managed policy and feature gates still apply. A committed native `ready` event
must prove the exact session and version before dispatch. It does not prove a
completed task. The hook observes `turn.start`/`turn.complete` and permits only
exact-turn cancellation; it does not change authentication or provider settings.

For the Claude Desktop viewer, add `owner_state_dir` to its transcript binding to
display state from those native events. Without this field, state stays unknown.

## Create and dispatch

All commands below use the actual installed Skill path in place of `<skill>`.
The state root is the retained `$CODEX_WORKER_STATE_DIR`, otherwise
`$XDG_STATE_HOME/claude-codex-worker` or `~/.local/state/claude-codex-worker`.
Do not override it to evade an unresolved old task or the common `worker.lock`.

```sh
python3 <skill>/scripts/bridge.py task-create \
  --task TASK_ID --project /absolute/project \
  --initiator claude --initiator-session EXACT_CLAUDE_UUID \
  --executor-session EXACT_CODEX_UUID \
  --requirements-file /absolute/brief.txt --transport-file /absolute/transport.json

python3 <skill>/scripts/bridge.py task-dispatch \
  --task TASK_ID --initiator claude --initiator-session EXACT_CLAUDE_UUID --wait 45
```

For the reverse direction, use `--initiator codex`, its exact session ID, the
Claude executor ID, and the `claude-native` transport. The project, main roles,
executor session and transport cannot change on a saved task.

Creation captures actual tracked and nonignored untracked contents, modes, HEAD,
index and status outside the project. Later snapshots also retain earlier paths
even if a new `.gitignore` hides them. Committing changes does not hide the net
diff. Binary data, symlinks, deletes and renames retain blob references. Unsupported
submodule/special-file contents, capture races and size limits block complete
acceptance rather than silently dropping files.

Dispatch persists a unique client message ID and `unresolved:true` before sending.
A timeout, interruption or lost acknowledgment never permits resubmission.

```sh
python3 <skill>/scripts/bridge.py task-watch --task TASK_ID --wait 45
python3 <skill>/scripts/bridge.py task-status --task TASK_ID
```

`task-watch` reconnects and observes only the original request/turn. Repeat watch
while work is running. Waiting for permissions, unknown submission and provider
failure are reported explicitly. Resolve native permissions in the owner without
changing provider configuration or manufacturing a terminal event.

## Inspect, test and accept or revise

`needs_review` means the exact executor turn successfully ended, not that the
delivery passed. Read `workflow.delivery_packet.path`: its `comparison.changes`
contains the complete baseline-to-final diff and content references. Read the
actual files and affected context. The executor's summary is evidence to inspect,
never sufficient grounds for acceptance.

Record appropriate verification against this exact snapshot:

```sh
python3 <skill>/scripts/bridge.py task-test \
  --task TASK_ID --initiator claude --initiator-session EXACT_CLAUDE_UUID \
  --timeout 45 -- python3 -B -m unittest -v

python3 <skill>/scripts/bridge.py task-review \
  --task TASK_ID --initiator claude --initiator-session EXACT_CLAUDE_UUID \
  --decision pass --reviewed-path changed_file.py --reviewed-path test_changed_file.py \
  --notes-file /absolute/review.txt
```

Tests run as argv, without a shell. Output, exit code, timeout, requirement version,
attempt and snapshot are saved. Passing requires successful recorded tests,
coverage of all final changed paths, and an unchanged worktree. Test-created
untracked files can change the snapshot: clean only disposable outputs, or use
`task-snapshot` with the same initiator arguments to capture deliberate changes,
then inspect and test again. Refreshing invalidates earlier acceptance and tests.

Use `--decision revise` with concrete file/behavior/fix criteria in the notes.
The next `task-dispatch` automatically includes the latest revision review and
continues the same executor session. The initiator repeats review after each
delivery, normally until pass or a concrete blocker; stop repeated attempts on
the same issue after two rounds without new evidence, progress or a feasible
approach. Record `--decision blocked` for a successful delivery whose review
cannot proceed. An execution failure is already failed and cannot be accepted.

For user feedback, save the complete updated requirements:

```sh
python3 <skill>/scripts/bridge.py task-feedback \
  --task TASK_ID --initiator claude --initiator-session EXACT_CLAUDE_UUID \
  --requirements-file /absolute/updated-brief.txt
```

Then dispatch again. This creates a requirement version, invalidates prior pass,
and preserves the original session and review history. An independent new task
uses a new task ID and its own executor conversation. Report completed changes,
the initiating agent's acceptance decision, actual tests and remaining issues.

## Stop and recovery limits

`task-stop --task TASK_ID` queues cancellation for the exact saved native turn.
If a monitor is active it consumes the request; otherwise run `task-watch` to
send it. Stop requested does not mean stopped: only the original owner's terminal
event resolves execution. Unknown turn identity cannot be cancelled by guessing.

Claude matches a unique request marker to a real native `turnId`; queue mixing
that prevents exact association remains unknown. Native request refusal before a
turn exists remains unresolved instead of inventing a completion. A lost monitor
does not reopen its original reply socket, so late permission receipts can be
unavailable; native lifecycle files and the original owner remain the recovery
sources. Never solve ambiguity with a second submission.

`accepted` is a historical decision bound to specific bytes. `task-status` reports
`review_stale` if those bytes later change. Identity fields and hashes establish
association and integrity; they do not prove that a human or agent understood the
diff, and are not an authentication boundary against another same-user process.
