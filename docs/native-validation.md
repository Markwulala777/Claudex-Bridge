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
| Claude official VS Code follower | Same temporary session, original quota-error text and follower attachment notice visibly rendered | Successful live model output and active-turn history handoff unverified |
| Claude Desktop follower | No usable native UI path verified | Cached tail gate off; renderer navigation and producer compatibility unresolved |
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

The existing Claude Desktop was running unrelated user work and was not restarted
or patched. Its native tail-rendering route remains unresolved. The version
difference also prevents silently using the IDE adapter with the Desktop producer.
The current implementation cannot solve that gap by changing Skill text.

## Next executable checks

1. Establish a supported native Claude Desktop follower route for the existing
   provider, including version negotiation and exact history handoff.
2. Once that provider can execute requests, resume the saved temporary Claude
   session, bind the native viewers, then verify real tool activity and completion.
3. Implement both executor directions on the validated owner adapters; exercise
   deliberate rejection, same-session revision, user feedback and interrupted
   monitoring without duplicate execution.
4. Validate the full workflow on each target OS before publication or installation
   as the replacement for `codex-worker`.

Offline regressions remain separate from these observations. The optional native
TUI test consumes fixtures and makes no model calls. `doctor` remains read-only
and reports `release_ready: false`; passing component checks does not open the
release gate.

The final combined run passed **77 tests**, including the real TUI consuming
offline protocol fixtures:

```sh
CLAUDEX_TEST_NATIVE_TUI=/opt/codex-desktop/resources/codex \
  python3 -B -m unittest discover -s tests
```

Without that explicit environment variable, the native-binary fixture test is
skipped. Skill validation and `git diff --check` also passed.
