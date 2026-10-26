# Implementation contract and gates

Accepted scope: one main Claude ↔ one main Codex; fixed main roles per task;
automatic implementation, acceptance and revision; both products' conversations
visible live in their own native terminal, desktop and official VS Code panel.
Independent computers; Linux, Windows and macOS; no cross-computer sync.

The main-agent constraint does not prohibit execution subagents. A Codex executor
may delegate independent work, then integrate and verify it. Claude subagents
default off. The initiating main agent personally accepts or rejects delivery.
No recursive bridge calls, role swapping, or executor self-approval.

## 1. Repository and state compatibility

- [x] Create the local Git repository on the user's Desktop.
- [x] Reuse the current installed runner, including its model/effort parameters.
- [x] Correct the installed `codex-worker` and repository contracts; remove
  bootstrap's `--disable multi_agent` / `--disable multi_agent_v2` arguments.
- [x] Keep existing provider configuration, state directory, task IDs and locks.
- [x] Add bilingual documentation, MIT license, and a read-only doctor.
- [x] Provide a gated, self-contained Linux Skill installer; preserve the separately installed old Skill.
- [x] Remove the bundled single-direction entrypoint and its dedicated tests; retain
  the shared native transport and lock-compatibility regression tests.

The old runner's state directory deliberately remains
`$XDG_STATE_HOME/claude-codex-worker` (default `~/.local/state/claude-codex-worker`),
overridable by the existing `CODEX_WORKER_STATE_DIR`. No task-state migration is
performed. New state roots must not bypass its one-main-worker lock.

Current verification (Linux, 2026-10-26): both initiating agents completed actual
persisted dispatch, rejection, same-session revision, independent review and
feedback. Native views, reconnect and foreground stop were observed. The exact
scope and known native history limitation are in the [validation record](native-validation.md).

## 2. Native attachment gate — Linux component checks passed

- [x] Inspect the locally installed clients and protocol versions without model calls.
- [x] Identify exact missing attachment paths and distinguish them from history import.
- [x] Implement a read-only native Codex TUI gateway and verify real owner updates.
- [x] Implement a Claude VS Code follower and verify the actual panel's binding,
  history rendering and initialization without starting a second Claude.
- [x] Add an isolated VS Code profile helper without changing the user's profile.
- [x] Use one message stream in the isolated VS Code view and deduplicate reconnect
  replay in the native session model; preserve native diff metadata.
- [x] Prepare a removable, version-pinned Linux Claude Desktop copy with a read-only
  LocalSessions API, explicit listener acknowledgments and source-failure handling.
- [x] Observe that Desktop copy rendering the bound conversation in its actual UI.
- [x] Claude on Linux: one executing session simultaneously visible in native TUI,
  native Desktop, and official VS Code panel. Existing settings remained unchanged;
  the user authorized the official account route for successful live validation.
- [x] Codex on Linux: one existing executing session simultaneously observed in
  native TUI, native Desktop, and official VS Code panel, using existing login.
  Full dispatch/revision acceptance below subsequently passed.
- [x] Opening or reconnecting another native view did not start a second inference
  in the observed Linux runs; version-pinned followers cannot dispatch execution.
- [x] Native views updated tool activity and results automatically without user refresh.
- [x] Save actual client versions and integration evidence for each working adapter.

See [native-client findings](native-clients.md) and the newer
[live validation record](native-validation.md). Linux development may now exercise
the full persisted workflow. Normal CLI dispatch requires a receipt for this computer, implementation and
client versions. Other platforms remain unverified.

## 3. Bidirectional executor and acceptance loop

- [x] Specify shared role and subagent policy in the new skill.
- [x] Implement both native owner adapters and the shared workflow, with offline integration tests.
- [x] Validate successful execution in both directions through all native attachments.
- [x] Save both main session IDs, project, requirement versions and review evidence.
- [x] Rework and feedback target the original executor session; independent tasks use new sessions.
- [x] Bind review to complete actual diffs, unchanged snapshots and actual test evidence.
- [x] Run the persisted native acceptance cycle in both directions on Linux.
- [x] Each initiating agent reported completion, actual acceptance and test results.

Default revisions continue until acceptance or a concrete blocker. The user may
change the limit or stop. After two attempts on the same issue without new evidence,
progress or a feasible new approach, report the blocker instead of looping.
Automatic acceptance requires the initiating main-agent conversation to remain active.

## 4. Persistence, cancellation and recovery

The shared workflow contains persistent unresolved-submission protection, exact
conversation/turn matching, separate execution/review states, original-turn
cancellation, and baseline capture. Offline regression tests exercise these paths;
they are not evidence of live GUI integration.

- [x] Implement those invariants for both native owners and validate with offline regressions.
- [x] Recover disconnected native views without duplicate task submission.
- [x] Confirm exact-turn foreground cancellation and command termination for both owners.
- [x] Observe quota failure, permission waits and failing acceptance commands; no false acceptance.

Native checks observed Claude quota failure, one-time permission resolution,
failed independent tests, detached monitoring and duplicate dispatch rejection.
Both owners' first stop probes exposed background-command behavior; those failures
remain recorded. Claude now requires foreground-only native tasks; Codex pins
attributed new command PIDs before exact-turn interrupt and verifies cleanup.
Successful corrected probes and same-session continuation were then observed.

A later Codex Desktop window rebuild exposed unsupported native item pagination.
The gateway and exact-turn monitor can use fresh authenticated owner snapshots;
the TUI labels incomplete old history and rejects unavailable older pagination.
No rollout history is imported, and an unobservable turn remains unknown.
See [workflow commands](../skills/claudex-bridge/references/workflow.md).

## 5. Platform integration and installation

Verified execution owners are Codex Desktop and Claude native TUI; other views
are read-only. Opening/creating owners, native trust and first attachment remain
explicit setup steps. Arbitrary input ownership across all native composers and
one-command native onboarding are not implemented.

- [x] Discover supported Linux installs and select version-specific adapters.
- [ ] Implement Windows transports and locks and macOS native attachment.
- [x] Provide gated install/uninstall of self-contained skills without editing provider settings,
  CC Switch, authentication or unrelated rules.
- [x] Keep local state and existing sessions; reject receipts from another computer.

Doctor can run without importing the Linux executor. Its portable Python execution
does not imply cross-platform execution support. The native adapters' Unix sockets,
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

- [x] Linux actual native end-to-end run within the recorded owner/view scope.
- [x] Separate local runtime readiness from the three-platform release gate.
- [ ] Windows actual native end-to-end run.
- [ ] macOS actual native end-to-end run.
- [x] Bilingual installation and troubleshooting instructions reflect actual Linux results.
- [x] The user authorized public Linux development-preview source on 2026-10-26,
  superseding the earlier requirement to wait for all platforms before publishing source.
- [ ] Complete Windows/macOS native acceptance before a supported cross-platform release.

Source repository: https://github.com/Markwulala777/Claudex-Bridge. Local installation
and dispatch still require acceptance bound to the current package and computer.
