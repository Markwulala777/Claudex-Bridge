# Claudex Bridge

## Contract

The accepted product contract and implementation gates are in
`docs/implementation.md`. The current evidence is in `docs/native-validation.md`.
Do not describe a planned capability as implemented or a static observation as
an end-to-end test. Do not publish this repository until the release gate passes.

There are two main agents with fixed roles per task. The initiating main agent
owns requirements, final acceptance, and reporting. The other main agent owns
implementation and its delivery. A Codex executor may delegate independent work
to Codex subagents and must integrate and verify it. Claude subagents are off by
default to conserve quota. Do not recursively call the bridge from an executor
or subagent, swap main roles mid-task, or let an executor approve itself.

## Implementation

Keep existing user changes, authentication, providers, CC Switch, and global
rules. Native simultaneous views of the same running conversation are required;
history sharing, a second inference process, and a separate log viewer do not
meet that requirement. Internal adapters must check their protocol version and
fail closed. Reconnect by saved conversation and turn IDs; never use `--last`.

`skills/claudex-bridge/scripts/worker.py` and `desktop_bridge.py` are the retained
Linux Claude-to-Codex desktop implementation. They intentionally retain the
old state directory and lock so two names cannot start competing main workers.
They are not the finished bidirectional, three-surface runner. Keep the installed
`codex-worker` usable while the new native adapters are blocked.

`workflow.py`, `task_state.py`, `git_evidence.py` and the native owner adapters
provide the new Linux development workflow. Normal CLI dispatch remains
acceptance-gated. Command/evidence semantics are maintained in
`skills/claudex-bridge/references/workflow.md`. On 2026-10-26 one Claude TUI owner
implemented, revised and continued feedback with real initiating-agent review;
Desktop and official VS Code displayed its live tools/results and reconnected.
The full persisted Linux workflow subsequently passed in both directions,
including rejection, revision, initiating-agent review, feedback and reconnection.
Other OSes and creation/onboarding from arbitrary native composers remain outside
that verified scope. Installer copies require the locally bound acceptance receipt.
Claude owners must launch with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`: without
it, native turn abort moves running shell commands to the background. Dispatch
checks the owner's environment; do not remove this requirement based on an idle
registry value. The native foreground cancellation regression stopped the command
and its child process, with all three surfaces retaining the interrupted result.

Codex exact-turn native interruption alone leaves some commands alive. Keep the
Linux process-attribution baseline and pidfd cleanup in `codex_processes.py`:
preserve pre-existing/other-session processes, verify PID birth before pinning,
and keep unconfirmed cleanup unresolved. Never use a second unscoped interrupt.

## Validation

Use Python 3.9+ and the standard library. Run:

```sh
python3 -m unittest discover -s tests -v
python3 skills/claudex-bridge/scripts/bridge.py doctor --json
```

Doctor is read-only, makes no model calls, and exits 2 while this computer's
runtime requirements are unmet. `doctor --require-release` additionally requires
all three platforms. Linux may be enabled after its full acceptance without
claiming Windows/macOS support or publishing. Offline tests use temporary files
and protocol doubles, never a real model. Live integration is authorized for temporary projects in this development
task; do not relabel it as verified before observing the actual native surfaces.
Record evidence separately from expectations. Windows/macOS require real checks
on those systems before claiming support. Do not commit local transcripts,
credentials, diagnostic reports, or task state.

Claude Desktop's removable copy uses the existing profile exclusively; never
launch it alongside the normal client. A registry `idle` value does not prove
background commands have stopped. Honor the native quit confirmation and preserve
unrelated work. Preparing a copy or passing offline tests does not establish native
UI attachment or successful live execution; consult the actual validation record.
