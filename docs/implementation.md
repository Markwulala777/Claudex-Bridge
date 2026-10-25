# Implementation contract and gates

Accepted scope: one main Claude ↔ one main Codex; fixed main roles per task;
automatic implementation, acceptance and revision; both products' conversations
visible live in their own native terminal, desktop and official VS Code panel.
Independent computers; Linux, Windows and macOS; no cross-computer sync.

The main-agent constraint does not prohibit execution subagents. A Codex executor
may delegate independent work, then integrate and verify it. Claude subagents
default off. The initiating main agent personally accepts or rejects delivery.
No recursive bridge calls, role swapping, or executor self-approval.

## 1. Repository and compatibility entrypoint

- [x] Create the local Git repository on the user's Desktop.
- [x] Reuse the current installed runner, including its model/effort parameters.
- [x] Correct the installed `codex-worker` and repository contracts; remove
  bootstrap's `--disable multi_agent` / `--disable multi_agent_v2` arguments.
- [x] Keep existing provider configuration, state directory, task IDs and locks.
- [x] Add bilingual documentation, MIT license, and a read-only doctor.
- [ ] Install the new entrypoint only after its complete behavior can be supported.

The old runner's state directory deliberately remains
`$XDG_STATE_HOME/claude-codex-worker` (default `~/.local/state/claude-codex-worker`),
overridable by the existing `CODEX_WORKER_STATE_DIR`. No task-state migration is
performed. New state roots must not bypass its one-main-worker lock.

Current verification (Linux, 2026-10-25): `python3 -m unittest discover -s tests -v`
passed all 36 offline tests. These cover state recovery, uncertain submission,
revision, review gates, exact-turn cancellation, protocol framing/ownership,
bootstrap identity persistence, and diagnostic output. Skill validation and Python
syntax checks passed. The installed runner scripts match the retained repository
copies. No real Claude/Codex model call or native three-surface trial was performed.

## 2. Native attachment gate — currently blocked

- [x] Inspect the locally installed clients and protocol versions without model calls.
- [x] Identify exact missing attachment paths and distinguish them from history import.
- [ ] Claude: one executing session simultaneously visible in native TUI,
  native Desktop, and official VS Code panel, with the existing provider retained.
- [ ] Codex: one executing session simultaneously visible in native TUI,
  native Desktop, and official VS Code panel, using existing login.
- [ ] Opening or reconnecting another native view never starts a second inference.
- [ ] Native views update tool activity and results automatically without user refresh.
- [ ] Save actual client versions and integration evidence for each working adapter.

See [native-client findings](native-clients.md). The full bridge must not dispatch
until this gate passes. The old desktop leg is retained as implementation input,
not relabeled as a complete bridge. Current blockers cannot be solved just by
changing Skill wording.

## 3. Bidirectional executor and acceptance loop

- [x] Specify shared role and subagent policy in the new skill.
- [ ] Implement both directions using validated native attachments.
- [ ] Save both main session IDs, project, requirements and review evidence.
- [ ] Rework targets the original executor session; new requirements create new tasks.
- [ ] Run full acceptance on actual diffs and test evidence; partial reviews do not pass.
- [ ] Show final completion, acceptance, tests and real remaining issues to the user.

Default revisions continue until acceptance or a concrete blocker. The user may
change the limit or stop. After two attempts on the same issue without new evidence,
progress or a feasible new approach, report the blocker instead of looping.
Automatic acceptance requires the initiating main-agent conversation to remain active.

## 4. Persistence, cancellation and recovery

The retained desktop runner already contains persistent unresolved-submission
protection, exact conversation/turn matching, separate execution/review states,
original-turn cancellation, and baseline capture. Offline regression tests exercise
these paths; they are not evidence of live GUI integration.

- [ ] Port and validate those invariants for the reverse adapter and native shared owner.
- [ ] Recover a disconnected view without duplicate task submission.
- [ ] Confirm actual terminal/GUI stopping behavior on the original execution.
- [ ] Verify quota, permission and command failures never become accepted results.

## 5. Platform integration and installation

- [ ] Discover supported native installs and select version-specific adapters.
- [ ] Implement Windows transports and locks and macOS native attachment.
- [ ] Install/uninstall portable self-contained skills without editing provider settings,
  CC Switch, authentication or unrelated rules.
- [ ] Keep each computer's independent local state and existing sessions.

Doctor can run without importing the Linux executor. Its portable Python execution
does not imply cross-platform execution support. The old executor's Unix sockets,
`/proc`, `fcntl`, and `xdg-open` are explicitly Linux-specific.

## 6. Release acceptance

Use a disposable Git project on each OS with an existing user edit and untracked
file. For both initiating agents, record evidence that:

1. A brief reaches exactly one executor and all three native surfaces show it live.
2. A deliberately rejected first delivery is revised in the same session.
3. The initiating main agent inspects actual changes and tests and reports acceptance.
4. User feedback continues the task with its context intact.
5. Interrupted monitoring, uncertain submission, quota failure and stop do not cause
   duplicate execution or false completion.
6. Codex may delegate independent subtasks without changing the two main roles;
   existing project changes remain preserved and the initiating main agent does
   final acceptance.
7. No endpoint, login, CC Switch or unrelated configuration changes are needed.

- [ ] Linux actual native end-to-end run.
- [ ] Windows actual native end-to-end run.
- [ ] macOS actual native end-to-end run.
- [ ] Full bilingual installation and troubleshooting instructions reflect the results.
- [ ] Only after every gate passes, create the public `Markwulala777/claudex-bridge`
  repository and publish. No GitHub repository has been created for this work.
