# Native-client findings

Evidence collected on Linux, 2026-10-25. These are read-only installation and
protocol observations. No new live three-surface session has been verified by this
development work. Private interfaces may change; versions alone are not acceptance.

| Component | Observed installed version |
| --- | --- |
| Codex Desktop | 26.1016.61520 |
| Desktop-bundled Codex CLI | 0.163.0-alpha.11.1 |
| PATH Codex CLI | 0.154.0 |
| Codex official VS Code extension | 26.1007.21434 |
| Extension-bundled Codex CLI | 0.162.0-alpha.17.2 |
| Claude Desktop | 3.6556.1 |
| Claude Code VS Code extension/bundled CLI | 2.1.295 |

## Codex: native Desktop ↔ IDE exists; native TUI needs another transport

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
patch broadcasts. This supports retaining the existing desktop adapter, but actual
dual-window rendering still requires a temporary task and observation.

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
`vscode://openai.chatgpt/local/<UUID>` is a source-backed candidate for opening the
native panel, not a tested activation guarantee.

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

### Official VS Code panel

`get_session_request` reads the saved transcript once. The webview's
`loadFromServer()` then calls `launchClaude()` and caches its loading promise.
`sessionHeldElsewhere` displays a warning and hides input; observed live-session
registry updates affect the session list, not a transcript subscription. There is
no verified passive live follow route in this version.

The documented `claudeCode.claudeProcessWrapper` is a possible adapter entrypoint:
the wrapper receives the original binary and stdio stream-JSON arguments. A real
implementation would need initialization/control responses, history replay,
deduplication, event translation, disconnection and single-owner semantics. Merely
enabling the wrapper does not provide those features. No wrapper is installed.

### Native terminal and provider

`claude attach` targets a background session's PTY and can wake/resume a task;
it is not a general passive follower for an SDK process. `--sdk-url` is limited to
print/stream-JSON mode. `--desktop` transfers execution toward Desktop and is not
combinable with `--bg` in the observed CLI.

A candidate uses an interactive TUI as the single executor, a VS Code stdio proxy,
and Desktop's native transcript tail. It still depends on the unavailable or
unverified Desktop UI and an unimplemented VS Code proxy. It is a research path,
not the implemented architecture.

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

The full requirement cannot be marked supported by the retained runner or Skill
changes. First verify a usable native Claude Desktop tail view, implement and test
the VS Code proxy against the same owner, and establish Codex's native TUI shared
owner. Then perform both acceptance loops on each OS. Do not substitute logs,
custom panels, duplicated inference or history imports for these checks.
