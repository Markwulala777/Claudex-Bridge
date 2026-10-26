# Claudex Bridge

[简体中文](README.zh-CN.md)

Claude and Codex delegate implementation to each other. The initiating main agent
reviews the actual changes and tests, requests same-session revisions, and reports
the result. Roles stay fixed throughout a task.

**Linux development integration: both directions passed live acceptance on
2026-10-26.** Windows/macOS are not implemented or validated. Publication remains
blocked by the agreed three-platform release gate.

## Verified workflow

Discuss requirements → dispatch to one executor → implement and test → initiating
agent reviews → revise in the same session until accepted or blocked. User feedback
continues the original task and sessions. The initiator must stay active for this loop.

Both directions were tested in disposable Git projects with deliberate rejection,
same-session repair, independent acceptance tests, feedback, interrupted monitoring,
duplicate-submission protection, native view reconnection and foreground stopping.
Pre-existing tracked edits and untracked files were preserved. A real Claude quota
failure remained a failure. See [the evidence and limits](docs/native-validation.md).

| Product | Executes and accepts input | Simultaneous native views |
| --- | --- | --- |
| Codex | Desktop | Desktop, bundled TUI, official VS Code extension |
| Claude | Native TUI 2.1.295 | TUI, removable Desktop copy, official VS Code extension |

Follower composers are read-only. Native permission prompts stay with the owner.
The installer does not create conversations or automatically attach every window.
Claude Desktop's copy uses its existing profile exclusively, so the original must
be closed normally first. This does not yet support chatting interchangeably from
any frontend. A pinned Codex history-pagination defect can limit a reconnected TUI
to the owner's loaded recent turns; live updates continue and older history is
explicitly unavailable, never imported or presented as complete.

Codex executors may delegate independent subtasks and must integrate their work.
Claude subagents are off by default. Final acceptance belongs to the initiating
main agent, not the executor. No recursive bridge calls or main-role swapping.

## Install on a validated Linux computer

Python 3.9+ with Linux pidfd support, Git and the validated native clients are required.

```sh
python3 -B skills/claudex-bridge/scripts/bridge.py doctor --json
python3 -B install.py check --target both
python3 -B install.py install --target both
```

Invoke `/claudex-bridge` after the client reloads its skills, then follow the
packaged [native setup](skills/claudex-bridge/references/native-setup.md) and
[workflow](skills/claudex-bridge/references/workflow.md). Installation copies a
self-contained Skill to both personal Skill directories, protects existing edits,
and rolls back partial activation. It preserves `codex-worker`, accounts, CC Switch,
providers, global rules and the shared task state.

Doctor makes no model calls. Local readiness requires actual evidence bound to
this computer, package and client versions. Copying the repository to a different
computer does not transfer acceptance. `--require-release` additionally requires
Windows and macOS; that gate remains false. See [receipt rules](docs/acceptance-evidence.md).

[Installation, updates, uninstall and troubleshooting](docs/installation.md) ·
[Implementation checklist](docs/implementation.md) ·
[Historical interface findings](docs/native-clients.md)

## Verification

```sh
CLAUDEX_TEST_NATIVE_TUI=/opt/codex-desktop/resources/codex \
  python3 -B -m unittest discover -s tests
```

The suite uses temporary projects and protocol fixtures, not live model calls.
The optional native-binary test exercises its renderer against fixtures. Actual
native acceptance is recorded separately. Credentials, transcripts, task state,
prepared client copies and local acceptance receipts are excluded from Git.

MIT licensed. Each computer has independent state; cross-computer synchronization
is outside the scope. GitHub publication awaits all three platform gates.
