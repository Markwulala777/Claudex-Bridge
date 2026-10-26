# Native validation record

Updated: 2026-10-26 (Asia/Taipei). Host: Linux. VS Code: 1.143.1. This records checks actually
performed during development, not an automatic claim about another computer.
Session IDs, transcripts, screenshots, provider endpoints and credentials are
not included in the repository.

| Capability | Observed result | Remaining limit |
| --- | --- | --- |
| Codex Desktop + TUI + official VS Code | Same conversation, real live tools/results; initial dispatch, revision and feedback | Desktop owns execution; native TUI/IDE views are read-only |
| Claude TUI + Desktop + official VS Code | Same live conversation, tools/tests and initiating-agent acceptance reports; view reconnection | TUI 2.1.295 owns execution; Desktop is a removable exclusive-profile copy |
| Persisted bidirectional loop | Both initiating main agents rejected, revised, personally reviewed actual diffs/tests and accepted user feedback | Initiator must remain active; first attachment/onboarding is explicit |
| Cancellation | Corrected foreground probes terminated for both owners; exact interrupted turns were not accepted | Does not undo effects or guarantee external deliberately detached jobs |
| Codex window rebuild | Same thread recovered without dispatch; fresh owner snapshots and final result returned | Pinned client's older-history pagination can fail; TUI explicitly shows recent history only |
| Windows/macOS | Not run; user has Linux only | Platform adapters and real machines required before release |

## Codex evidence

Versions: Desktop 26.1016.61520, native TUI 0.163.0-alpha.11.1, official extension
26.1007.21434. The original Desktop owned the existing development conversation.
No temporary worker was dispatched while the installed worker was handling a
different user task; its global lock and task state were preserved.

The native TUI was first checked with explicit offline fixtures, then against the
real owner for 50 seconds and again for 180 seconds. The second connection matched
real history and new assistant output in its PTY, receiving 45 item starts, 45
completions, 16 command-output increments and 3 assistant increments. It sent no
execution, steering or interruption requests. Temporary view processes/listeners
were closed without stopping the owner.

A further 60-second check used the user's default Codex configuration. It exposed
the native TUI's `serviceTier: priority` resume hint. The adapter now accepts that
hint only when it exactly matches the owner's value, and does not forward it.
The actual default-config run matched 16 historical assistant snippets, both new
command increments and both new assistant increments in the PTY. The user's
`config.toml` bytes remained unchanged.

During that run, at 14:17:09.8 UTC, both the actual Desktop window and the official
VS Code panel visibly showed the same current conversation, latest commentary and
tool progress while the native TUI remained connected. The IDE used the exact-ID
native URI in an isolated profile. Desktop navigation was restored afterward.
No prompt was entered into a follower. See [protocol evidence](codex-tui-protocol.md)
for exact supported fields and display differences.

## Claude evidence

Versions: official VS Code extension and its native CLI 2.1.295; Desktop's active
CLI 2.1.308. A disposable Git project contained an unimplemented integer-sum
function and a pre-existing note. Exactly one model request was submitted from
the native interactive TUI using the existing provider. It returned a quota error;
the function and note remained byte-for-byte unchanged. The CLI's local cost view
reported zero input/output tokens for that attempt. No provider or login was changed,
and no second model request was used to retry the failure.

With that owner idle, the exact complete transcript boundary was bound to the
proxy. An isolated official VS Code profile launched the proxy with the same
session's `--resume`. The real host processed initialize and then get_settings,
producing an atomic readiness receipt. An auth-status probe was explicitly refused;
the official `claudeCode.disableLoginPrompt` setting allowed the read-only native
session view without claiming an authenticated account.

The native panel visibly rendered the original user request, original 403/quota
error, and attributed follower notice. It did not show a successful result or
duplicate the existing assistant row. The proxy never executes the supplied CLI.
This proves native history rendering and attachment initialization. Since quota
prevented a successful model turn, it does **not** prove live successful output,
automatic revision, completion handling or the final acceptance loop.

The isolated replay path was then checked with the native host-history response
patch and native webview reconnect patch. The actual panel again displayed each
original row once. Stopping only its Python follower produced the native process
terminated error while preserving history. No new prompt or model call occurred.
Both temporary VS Code profiles/processes and screenshots were cleaned up.

The Desktop preparation command created a copy from the actual installed build,
with a sealed binding to this same temporary session. The opener verified its
component hashes and refused while the original client was running. After user
authorization to quit idle sessions, the live registry reported three interactive
sessions idle. However, Desktop's own quit confirmation reported a background
command in one session and warned that quitting would stop it. Quit was cancelled.
The original client remained running; the copy was not launched and no original
app bundle, provider configuration or feature gate was changed. Temporary
screenshots were removed. The prepared copy remains local for the later check.

The adapter uses a TUI transcript producer pinned to 2.1.295; Desktop's own
2.1.308 producer is still not accepted by the VS Code follower. Listener registration
acknowledgments do not yet establish native transcript hydration. The current
implementation cannot solve these gaps by changing Skill text.

## 2026-10-26 continuation

The original Desktop quit normally this time. Its removable copy opened using
the existing profile and visibly displayed the exact temporary TUI conversation.
Before the owner resumed, the copy ran one Python reader and no Claude inference
process. Resuming the exact original TUI UUID restored its existing history.

A task-local native hooks module was validated with the installed CLI and loaded
with `--plugin-dir`. A committed ready event proved the actual session/version.
One new authorized brief was sent through the native peer socket. A unique request
marker matched a real `turn.start.turnId`, followed by `turn.complete` with
`reason:error`, `isAborted:false` and an empty answer. The provider response was
again HTTP 403, insufficient quota. No automatic retry or provider change followed.
The integer-sum implementation and existing user note were still unchanged.

The Desktop automatically appended the peer brief and the real error. VS Code
automatically appended the error but initially hid the peer brief because its
native synthetic-message filter expected a different envelope format. The isolated,
hash-pinned webview now routes only verified bound-session peer rows through its
native peer component. A newly opened actual panel displayed the full original
brief and native peer wrapper under `Message from @peer`; ordinary human messages,
subagent rows, other sessions and original installed files were not reclassified.

The Desktop source can now bind the native hook directory. Reopening the copy
with that binding showed the same failed turn without a lingering running spinner.
The native owner process count was one. The copy then closed normally and the
original installed Desktop executable and its normal home UI were observed again.
These are real failure-path and view observations, not a successful coding run.

The shared workflow records fixed main sessions, requirement versions, exact
execution evidence, complete baseline-to-final Git contents, real test commands
and review coverage. Offline integration exercised both directions, deliberate
rejection followed by same-session rework, user feedback, monitoring loss, unknown
submission, failure, cancellation requests and stale acceptance. No offline
fixture was used to create a native acceptance receipt.

## Account route follow-up

The user authorized using their Claude subscription for the remaining live
checks. With the existing user settings loaded, the pinned CLI reported an
`oauth_token` login, but those settings contain a custom provider URL and auth
token. That status alone therefore did not establish a Claude subscription login.
The same read-only check with `--setting-sources project,local` reported
`loggedIn: false` and `authMethod: none`.

The original login wait exited before authorization. A replacement official login
was completed after the user manually passed browser verification. The browser
tool then failed because its extension needed an update; the user explicitly
authorized reading the copied one-time code locally and submitting it only to
that login process. It exited successfully. A fresh read-only check reported
`loggedIn: true`, `authMethod: claude.ai`, `apiProvider: firstParty` and a Pro
subscription. No code, token or credential is stored in this repository.

Desktop authentication, browser authentication and CLI authentication must be
checked independently. For this authorized disposable-project check, the
owner launch excludes user settings only for that process and explicitly chooses
`sonnet`; the original user settings stay on disk. Excluding user settings also
omits their permissions/hooks, so this is a development launch choice, not the
production skill's automatic authentication policy.

While waiting, native dispatch was bound to the fixed task project as well as the
session ID. Claude checks the live owner process's working directory, and Codex
checks the Desktop snapshot's `cwd` (observed in the actual installed protocol).
Both recheck before sending. Regression tests verify that a wrong/missing project
and a directory change after preflight create no native prompt or owner submission
record.

## Successful account execution and native views

The saved native TUI conversation implemented the disposable sum function and
four basic tests. The initiating Codex read its complete actual diff and ran
independent acceptance checks: bool and float inputs were incorrectly accepted.
The delivery was rejected with those concrete failures. Rework in the same native
session added type validation; all five test methods and both independent checks
passed. Existing project rules and the pre-existing note were unchanged.

A third request continued that task with indexed error messages and tuple/generator
coverage. The initiating Codex inspected every added/changed line, reran all ten
test methods and independently checked the exact error index, empty tuple and
single-consumption generator behavior. The final snapshot remained unchanged.

During this feedback run, the actual Desktop copy and official VS Code panel
automatically showed the peer request, Bash activity, test results and final answer
from the one TUI owner. Desktop was normally closed and reopened while its command
was still running; it hydrated the current activity without another submission.
VS Code was reopened and recovered the latest result. The native session registry
still contained exactly one matching owner PID. The original Desktop was restored
between checks. Native X11 inspection/control was explicitly authorized by the user.
The existing user settings file's bytes remained unchanged.

The first cancellation check exposed a real limitation: default `$.turn.abort`
ended the model turn but backgrounded its Bash command, which ran to completion
and caused another native model turn. This was not recorded as successful stopping.
The pinned CLI's implementation confirmed that disabling native background tasks
keeps cancellation on its process-termination path. Owner preparation now returns
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` as a process-local launch requirement;
dispatch checks the actual owner's environment before sending any prompt.

After normally resuming the same UUID with that environment and fresh hooks, an
exact-turn stop interrupted a real 60-second command after about eight seconds.
At the native aborted terminal event its Python process was already gone; six
consecutive checks found it absent and the owner had no remaining children.
Desktop displayed “已停止”; the official panel displayed the actual interrupted
tool result. No project bytes changed. This validates foreground native commands,
not shell-detached external jobs or undoing earlier side effects.

Permission hooks now record both request and resolution, with separate identities
even for parallel calls sharing a native dialog. The real run changed from waiting
to running after one-time approval and then to aborted. Offline regressions cover
parallel resolutions, unmatched events, missing/ambiguous launch environment and
rechecking it after preflight. No permission policy was broadened.

## Complete persisted Linux workflows

The account checks above were followed by two actual workflow runs in disposable
Git projects. Private task ledgers, native owner events, real transcripts,
screenshots, complete Git delivery packets and test logs retain exact IDs.
Development used the authorized internal workflow harness while the normal CLI
remained acceptance-gated. Final reviews came from the actual initiating agent.

**Codex → Claude:** the first integer-accumulation delivery passed 14 executor
tests but failed the initiator's strict `start`-type check. Codex read the complete
actual changes and rejected it. The same Claude session fixed it (15 tests).
User feedback then made `start` keyword-only and preserved generator non-consumption
on invalid input; 18 tests plus independent edge checks passed. Three attempts,
two requirement versions and reviews `revise`, `pass`, `pass` ended accepted.
Both the initiating and executing sessions stayed fixed.

**Claude → Codex:** the first unique-values implementation used sorted set output.
Claude read the complete diff and wrote independent acceptance tests: seven of
nine checks failed. It rejected the delivery with concrete criteria. The same
Codex session corrected strict built-in integer types, original error indexes and
stable single-pass de-duplication; nine project tests and nine independent checks
passed. Claude personally recorded acceptance and reported in its native TUI,
Desktop follower and official VS Code follower.

After the stop regressions below, Claude saved user feedback as requirements v4
and dispatched attempt 6 to that same Codex. It read the complete baseline-to-final
packet and actual files, then independently ran 13 project tests, the nine original
acceptance checks and 38 new boundary checks. All passed on the same verified
snapshot. The added keyword-only `reverse` validates built-in bool before consuming
input, reverses the de-duplicated result and preserves strict element checks.
Claude recorded `pass`, `accepted`, `acceptance_current=true` and a final Chinese
report. Its Desktop and VS Code displayed that report; Codex's Desktop, TUI and
VS Code displayed the executor's final actual delivery.

Both projects retained their original tracked user edit, untracked notes and
project rules. The two main roles stayed fixed. No Claude subagent or extra
inference owner was started. Codex delegation remains allowed by policy but no
subtask was needed, so a real Codex child delegation is not claimed as tested.

Monitoring was intentionally detached and resumed on exact IDs; another dispatch
was rejected while unresolved. Claude's real quota failure remained failed.
Actual permission prompts were individually approved without broadening policy.
The official account login was explicitly authorized earlier; provider settings,
CC Switch and global rules were not rewritten by the workflow or installer.

## Codex foreground stop and window recovery

The first exact-turn stop probe exposed a native limitation: Desktop reported the
turn interrupted but its 60-second command continued. That failed check is retained;
it was never accepted as successful stopping. A diagnostic continuation only read
the old process result and was not treated as a new successful stop test.

The corrected adapter snapshots pre-existing user processes before dispatch,
attributes new commands by exact Codex session markers and observed descendants,
checks PID birth identity and pins Linux pidfds before interrupting the exact turn.
It then terminates only those pinned processes and verifies no attributed command
remains. It does not issue a second unscoped interrupt. Existing unrelated or
pre-existing processes are excluded; unresolved cleanup blocks completion.

A fresh 60-second probe in the same session was terminated about five seconds
after it began. Six subsequent checks confirmed it absent (the observation record
including those checks spans about eight seconds). The attempt remained interrupted.
User feedback then completed as attempt 6 and was independently accepted by Claude.
The official Codex VS Code window was closed and reopened during that running
feedback turn; it showed ongoing work without another dispatch.

A later Desktop window rebuild disconnected the gateway. Reopening the exact
thread exposed a pinned-client defect: complete-history hydration requested
`thread/items/list`, which its backend reported unsupported. The corrected reader
accepts only that specific failure as a reason to request fresh snapshots from the
same authenticated original owner. It preserves their incomplete-history metadata.
The native TUI reconnect displayed the actual final delivery, and exact-turn
monitoring recovered the same completed attempt without resubmission. Its title
marks `[recent history only]`; older pagination reports unavailable. Incomplete
individual turn contents and other owner/protocol errors still fail closed.
This is a limit on older history, not evidence of complete history restoration.

## Installation and remaining scope

The standard-library installer preflights both personal Skill destinations,
refuses unmanaged or edited files, preserves executable modes and rolls back
partial activation. Copies include the Skill, scripts and references; they do not
copy accounts, native app bundles, task state or acceptance from another machine.
Old `codex-worker` remains available. See [installation](installation.md).

Only the observed Linux integration may use a receipt bound to this computer,
current package and client versions. The read-only doctor validates its integrity;
it does not infer success from offline tests. `release_ready` remains false because
Windows/macOS adapters and actual native acceptance are outstanding. Starting
from arbitrary native composers and automatic first-time onboarding are also not
implemented. No GitHub repository was created.

The final run passed **292 tests** in 23.614 seconds, including the optional native
renderer against fixtures. Skill validation and `git diff --check` also passed.
Reproduce with:

```sh
CLAUDEX_TEST_NATIVE_TUI=/opt/codex-desktop/resources/codex \
  python3 -B -m unittest discover -s tests
```

The suite's native renderer test uses offline protocol fixtures and makes no model
calls. Separate live observations above establish the workflow behavior. Source,
Skill validation and `git diff --check` are checked before installation. Private
transcripts, screenshots and local receipts are excluded from Git.

After validation, test owners and follower windows exited normally and the
original `/usr/lib/claude-desktop/claude-desktop` home UI was observed again.
Claude settings matched their recorded hash. Native client use added one
`service_tier = "default"` line to Codex's configuration; removing only that line
restored the exact recorded full-file hash. The setup instructions now isolate
the TUI viewer's preference directory. No other configuration bytes were restored
or overwritten. Unrelated VS Code work remained open.
