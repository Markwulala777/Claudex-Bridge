# Native-client findings

Historical investigation collected on Linux, 2026-10-25, with subsequent component
notes. Statements below about pending success/workflows describe that earlier
stage. Current results and remaining limits are in [actual native validation](native-validation.md). Private
interfaces may change; versions alone are not acceptance.

| Component | Observed installed version |
| --- | --- |
| Codex Desktop | 26.1016.61520 |
| Desktop-bundled Codex CLI | 0.163.0-alpha.11.1 |
| PATH Codex CLI | 0.154.0 |
| Codex official VS Code extension | 26.1007.21434 |
| Extension-bundled Codex CLI | 0.162.0-alpha.17.2 |
| Claude Desktop | 3.6556.1 |
| Claude Desktop executing CLI | 2.1.308 |
| Claude Code VS Code extension/bundled CLI | 2.1.295 |

## Codex: native Desktop ↔ IDE and the TUI gateway

Desktop's `app.asar/.vite/build/src-*.js` and the official extension's
`out/extension.js` share these generic IPC versions:

| Message | Version |
| --- | --- |
| `thread-owner-discovery` | 1 |
| `thread-follower-start-turn` | 2 |
| `thread-follower-load-complete-history` | 1 |
| `thread-follower-interrupt-turn` | 4 |
| `thread-stream-state-changed` | 11 |
| `thread-stream-following-changed` | 1 |
| `thread-stream-following-status-requested` | 1 |

Both clients implement owner discovery, follower requests and real-time snapshot /
patch broadcasts. Actual dual-window rendering was subsequently observed on the
current development conversation; the full task workflow remains unverified.

The native TUI's `--remote` uses the **app-server WebSocket protocol**, not this
length-prefixed generic IPC. The current running Desktop and IDE each own a stdio
app-server. The generic IPC socket exists, but the shared app-server daemon socket
does not. Passing `ipc/ipc.sock` to `--remote` will not join the Desktop owner.

A candidate Desktop daemon branch in
`application-network-startup-BG13czUt.js` requires all of:

- A non-Windows local host with no additional config overrides.
- `CODEX_APP_SERVER_USE_LOCAL_DAEMON=1`.
- No `CODEX_APP_SERVER_FORCE_CLI=1`, `CODEX_CLI_PATH`, host CLI command override,
  or bundled-Git override from `Xs(resourcesPath)`.
- An accepted daemon version (the observed minimum is `0.141.0`).

The branch uses `$CODEX_HOME/app-server-control/app-server-control.sock` with
WebSocket path `/rpc`. Windows is explicitly excluded. The bundled Git check also
excludes the usual macOS branch when that override is present. A Linux candidate
needs a planned restart/startup configuration; this work has **not** restarted the
user's current Desktop or migrated its active conversation.

The official extension registers a URI handler and `/local/:conversationId` route.
`vscode://openai.chatgpt/local/<UUID>` opened the current development conversation
in an isolated official IDE panel. It displayed actual new commentary and tool
activity from the existing Desktop owner. This does not itself establish a full
implementation/review/revision workflow.

`codex_tui_gateway.py` now translates read-only owner snapshots into the native
app-server WebSocket protocol without restarting Desktop or starting another
executor. Real history, tool output and assistant increments were observed in
the bundled native TUI. See [protocol and limitations](codex-tui-protocol.md).

### Subagents

Desktop's turn preprocessing currently sets `multiAgentMode=explicitRequestOnly`.
Sending `proactive` through the follower adapter is overwritten. This does not
disable explicitly requested delegation: the main worker's actual user prompt now
authorizes Codex subagents for independent tasks. Bootstrap no longer passes
`--disable multi_agent` or `--disable multi_agent_v2`; it preserves installed feature
defaults rather than forcing v2. Actual child-tool use still needs a live test.

Official references:
[shared Desktop/IDE context](https://learn.chatgpt.com/docs/developer-settings),
[CLI reference](https://learn.chatgpt.com/docs/cli/reference),
[App Server](https://learn.chatgpt.com/docs/app-server).

## Claude: useful interfaces, incomplete simultaneous attachment

### Desktop

The native preload exposes `LocalSessions.listCliSessions`, `readCliTranscript`,
`openCliTail` and `closeCliTail`. `CliSessionDiscovery` watches transcripts with
`fs.watch`, 1.5-second polling and 250-millisecond coalescing. It emits
`cli_transcript_appended`, `cli_transcript_resync` and `cli_transcript_closed`.
Reading this stream does not itself run another model.

In the observed ASAR these paths are in `index.chunk-B4ghMTmI.js`,
`index.chunk-CbirgK7f.js`, `index.chunk-DqktEifE.js` and `index.chunk-CDJ3tx_n.js`.
The discovery and tail methods require feature gate `1985233154`. This computer's
disk cache reports it off. The cache is not the running process or account state.
Doctor reads only this gate's boolean and age and never changes it. Setting
`CLAUDE_DEV_FORCE_GATES` is not a proven workaround: the production parser returns
an empty set in the observed build.

The actual chat renderer is served from `claude.ai`. A real native renderer caller
and automatic navigation to this tail view have **not** been established. Therefore
the existence of the preload method is not proof that the required UI works.

There is also a lifecycle mismatch: Desktop counts `interactive` sessions as live
for tail shutdown, while `claude --bg` registers `kind: bg`. A background session
cannot be assumed to keep this native tail open.

The current Desktop producer is Claude Code 2.1.308, while the official IDE
adapter below is pinned to 2.1.295. The production Desktop constructor references
`CLAUDE_CODE_LOCAL_BINARY` but does not invoke its local-binary initializer in the
observed build. Its process wrapper comes from managed settings; user/environment
settings are filtered. Neither setting is a verified user-level Desktop adapter.
No managed policy, installed app bundle or feature gate was changed.

A new removable Linux copy replaces the exact native LocalSessions API factory
before it initializes the original manager. It serves one bound transcript via a
read-only Python reader and the native Desktop event/schema path. The preload
acknowledges actual navigation and event listener registrations separately. This
does not use the gated CLI-tail path, adopt ownership, or launch another Claude.
Its native route is `/epitaxy/local_<exact CLI session UUID>`.

The copy preserves the normal profile/login and refuses to run while the original
Desktop is open. On 2026-10-25 its quit dialog reported a background command and
quit was cancelled. On 2026-10-26 normal quit succeeded: the copy displayed the
bound history and automatically appended a real native peer task and provider
quota failure. A lifecycle-bound view then displayed that turn as no longer
running. The original client was restored. Successful active-turn hydration still
needs validation. See [Desktop adapter contract](claude-desktop-protocol.md).

### Official VS Code panel

`get_session_request` reads the saved transcript once. The webview's
`loadFromServer()` then calls `launchClaude()` and caches its loading promise.
`sessionHeldElsewhere` displays a warning and hides input; observed live-session
registry updates affect the session list, not a transcript subscription. There is
no verified passive live follow route in this version.

The documented, machine-scoped `claudeCode.claudeProcessWrapper` is now used by
`claude_vscode_proxy.py`. It receives the original binary and stream-JSON arguments
but never executes that binary. It implements exact source/session/version checks,
initialization, a coordinated history boundary, faithful events, and explicit
refusal of execution controls. The official `disableLoginPrompt` option lets the
read-only profile show its session without inventing an authenticated account.
An isolated profile displayed the real temporary session, its quota failure and
the attachment notice. The user's normal profile is unchanged. See
[protocol evidence](claude-vscode-protocol.md) and [actual validation](native-validation.md).

The isolated replay profile now clears only the bound session's native history
response and receives history plus appends through one proxy stream. A pinned
webview patch retains message UUID/content identities across channel reconnects.
Its offline native receive-loop regression no longer doubles messages on replay;
the patched panel also displayed the real failed session once. The host still
reads native history for diff metadata, and transcript-only observation still
lacks faithful token, permission and turn-completion events. A successful active
owner and full native reconnect workflow remain to be observed. The exact-session
webview patch also preserves and displays legacy TUI peer envelopes through the
native peer component; the actual panel displayed the full real dispatch brief.

### Native terminal and provider

`claude attach` targets a background session's PTY and can wake/resume a task;
it is not a general passive follower for an SDK process. `--sdk-url` is limited to
print/stream-JSON mode. `--desktop` transfers execution toward Desktop and is not
combinable with `--bg` in the observed CLI.

The implementation uses an interactive TUI as the single executor, the VS Code
stdio proxy, and the removable Desktop read-only API adapter. Native peer dispatch
and task-local function hooks correlate the request to the real turn and its
completion reason. One actual error path has passed. Successful tool activity,
active-history handoff and the complete acceptance loop remain unverified.

The current Claude setup has a base-URL override. Remote Control's official-endpoint
requirements do not establish compatibility with this setup. No endpoint or login
was replaced. Resuming history or downloading a cloud-session copy is not native
simultaneous synchronization.

Official references:
[VS Code](https://code.claude.com/docs/en/vs-code),
[Remote Control](https://code.claude.com/docs/en/remote-control),
[Agent View](https://code.claude.com/docs/en/agent-view),
[Desktop](https://code.claude.com/docs/en/desktop).

## Consequence for the release gate

The full requirement cannot be marked supported by these view components. Complete
the native Claude Desktop path and successful owner/follower checks, then perform
both acceptance loops on each OS. Do not substitute logs, custom panels, duplicated
inference or history imports for these checks.
