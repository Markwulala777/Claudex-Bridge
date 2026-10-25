# Claudex Bridge

[简体中文](README.zh-CN.md)

Claude and Codex, with fixed main roles and an implementation–review loop.

**Development status: the full native-client requirement is blocked.** This is a
local development repository, not a released bidirectional bridge. The retained
runner remains available. Linux now has both native owner adapters, shared task
state and evidence-backed review. The real Claude provider still returns a quota
error, so successful implementation/revision, the complete three-surface
experience and cross-platform installation remain unaccepted.

The intended workflow is:

1. Discuss a task with either Claude or Codex.
2. That main agent delegates implementation to the other main agent.
3. The executor implements, tests, and returns the result in the same task session.
4. The initiating main agent reviews all actual changes and test evidence, then
   requests revisions in the same executor session until accepted or blocked.
5. Both conversations remain visible in each product's native terminal, desktop,
   and official VS Code UI, with only one execution per conversation.

Codex executors may use subagents for independent work. Main Codex integrates and
verifies their results; the initiating main agent personally performs final
acceptance. Claude subagents default to off. Roles do not swap during a task.

## What is here

- A revised skill contract and the retained desktop runner, including exact-ID
  recovery, persistent uncertain-submission protection, review records, and stop.
- A read-only compatibility doctor. It reports detected interfaces separately
  from verified behavior and never starts a model or changes client settings.
- A version-pinned Codex native TUI gateway, tested against a running Desktop
  owner, and a Claude official VS Code follower with an isolated-profile launcher.
  The Claude panel displayed a real session and its provider quota error; a
  successful model turn and revision remain unverified.
- A removable Linux Claude Desktop follower copy that displayed actual history,
  an automatically appended peer task and its quota failure. Native lifecycle
  evidence clears its busy indicator; the original client was restored.
- Claude native socket dispatch and task-local hooks, with actual request-to-turn
  correlation and native error completion observed without a second inference.
- A shared bidirectional workflow with fixed roles, requirement versions, complete
  Git content snapshots, actual test records, coverage-bound reviews, same-session
  revision and exact cancellation requests. Lost monitoring never resubmits.
- Native VS Code peer-task rendering that preserves the real body and attribution.
- VS Code reconnect replay deduplication, and separate local-runtime and
  three-platform release gates.
- Offline regression tests and the [implementation gates](docs/implementation.md).
- [Native-client findings](docs/native-clients.md), including the current blockers.
- [Actual validation and remaining work](docs/native-validation.md), separate from
  [native-view development commands](docs/native-views.md).

The installed `codex-worker` remains the compatibility entrypoint. The new skill
has not replaced it. See the [development workflow interface](skills/claudex-bridge/references/workflow.md).
Normal dispatch stays gated. A full installer, successful native bidirectional
acceptance and actual Windows/macOS validation are still required.

## Check this computer

From the repository root, with Python 3.9+:

```sh
python3 skills/claudex-bridge/scripts/bridge.py doctor
python3 skills/claudex-bridge/scripts/bridge.py doctor --json
python3 skills/claudex-bridge/scripts/bridge.py doctor --require-release
python3 -m unittest discover -s tests -v
```

Doctor exits `2` when this computer's native runtime requirements are blocked or
unverified. `--require-release` also requires all target platforms. Both gates
are currently false. Linux development use may begin after its full local
acceptance; publication still waits for all three OSes. See [evidence rules](docs/acceptance-evidence.md).
A detected
socket, a matching protocol version, or a passing offline test is not proof of a
working three-surface session. It does not connect to conversation sockets, run
the model CLIs, or write task state. Settings are read only to report whether a
custom Claude provider is configured; endpoint and credential values are omitted.

Development targets are Linux, Windows, and macOS on independent computers.
Cross-computer synchronization is out of scope. The retained executor currently
supports Linux only. No Windows/macOS end-to-end validation has been performed.

MIT licensed. GitHub publication is deferred until the complete contract passes.
