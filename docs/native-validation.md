# Native validation record

Date: 2026-10-25. Host: Linux. VS Code: 1.143.1. This records checks actually
performed during development, not an automatic claim about another computer.
Session IDs, transcripts, screenshots, provider endpoints and credentials are
not included in the repository.

| Capability | Observed result | Remaining limit |
| --- | --- | --- |
| Codex Desktop → native TUI | Real history and new assistant/tool output rendered through the read-only gateway | Version-pinned Linux adapter; forms stay with the owner |
| Codex Desktop + TUI + official VS Code | The same existing conversation was observed in all three native surfaces at the same time | Does not establish the complete bridge workflow |
| Claude native TUI | One real temporary-project request used the existing provider and failed with HTTP 403, insufficient quota | No implementation, tests or revision executed |
| Claude official VS Code follower | Same temporary session and quota-error text visibly rendered, including with isolated replay patches; reader termination displayed a native error | Successful live output and native channel restart unverified; deduplication checked offline |
| Claude Desktop follower | Actual version-pinned copy prepared; offline API/reader/listener tests passed | Native quit reported a running background command; quit cancelled, copy not launched; UI/history handoff unverified |
| Bidirectional acceptance loop | Not run | Native Claude path and successful provider calls required |
| Windows/macOS | Not run | Real machines and platform adapters required |

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

## Next executable checks

1. When the original Desktop has no background work to preserve, launch the
   prepared native follower and verify actual rendering, exact history handoff,
   reconnects and single-owner behavior.
2. Once that provider can execute requests, resume the saved temporary Claude
   session, bind the native viewers, then verify real tool activity and completion.
3. Implement both executor directions on the validated owner adapters; exercise
   deliberate rejection, same-session revision, user feedback and interrupted
   monitoring without duplicate execution.
4. Enable the Linux development entrypoint only after complete local acceptance;
   validate Windows/macOS before publication. Keep the installed `codex-worker`
   until the replacement's full local behavior is verified.

Offline regressions remain separate from these observations. The optional native
TUI test consumes fixtures and makes no model calls. `doctor` remains read-only
and reports `runtime_ready: false` and `release_ready: false`; passing component
checks does not open either gate. See [receipt semantics](acceptance-evidence.md).

The combined run after these changes passed **157 tests**, including the real TUI consuming
offline protocol fixtures:

```sh
CLAUDEX_TEST_NATIVE_TUI=/opt/codex-desktop/resources/codex \
  python3 -B -m unittest discover -s tests
```

Without that explicit environment variable, the native-binary fixture test is
skipped. Skill validation and `git diff --check` also passed.

New checks cover the Desktop core, loader, source subprocess and prepared ASAR;
the isolated VS Code history/reconnect patches; and local-runtime versus release
evidence. No complete native acceptance receipt was created.
