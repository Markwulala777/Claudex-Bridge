# Claude VS Code read-only stdio follower

`claude_vscode_proxy.py` implements a version-pinned follower transport for the
official Claude Code VS Code extension **2.1.295**, whose bundled SDK is
**0.3.295**. It reads one explicitly bound, existing source and never executes
the Claude binary, starts inference, resumes an owner, or changes provider
settings. This is an adapter component, not evidence that all three native
surfaces are integrated.

## Binding and launch contract

The extension's machine-scoped `claudeCode.claudeProcessWrapper` setting takes an
executable path. Its `yS()` implementation prepends the original bundled CLI path
to the SDK arguments. The proxy's shebang uses Python 3; its executable bit must
be preserved during installation. No installed extension or existing user
configuration was changed while developing this component.

The launch environment must contain
`CLAUDEX_BRIDGE_CLAUDE_BINDING=/absolute/path/binding.json`. A binding is:

```json
{
  "schema": 1,
  "session_id": "11111111-1111-4111-8111-111111111111",
  "source": "/absolute/path/owner.jsonl",
  "source_format": "transcript",
  "claude_code_version": "2.1.295",
  "history": "native",
  "start_offset": 1234,
  "ready_file": "/absolute/path/new-attachment-ready.json"
}
```

The example UUID and offset are placeholders. `source_format` can also be
`stream-json`, meaning an append-only recording of the actual owner's SDK output.
`ready_file` is optional and must not exist. Its parent directory must exist.
It is a one-shot handshake probe: omit it for ordinary reconnectable views.
Reusing a binding with an existing receipt is intentionally refused.
The producer version, version found in the source, and official extension
package version must all be 2.1.295. In particular, a Desktop-owned 2.1.308
producer is **not** silently treated as compatible.

The actual process invocation is:

```text
claude_vscode_proxy.py ORIGINAL_BUNDLED_CLAUDE --resume EXACT_UUID --input-format stream-json --output-format stream-json [SDK arguments]
```

The original executable is inspected only to locate the adjacent official
extension package metadata. It is never executed. `--resume` must exactly match
the binding; alternate selectors, `--continue`, and forks are rejected. A missing
file, an empty/unproven source, another session, or unsupported version fails
with exit code 2. The adapter never creates a replacement conversation.

There is one non-conversation invocation: the native extension's
`spawnConfigProbe()` sets `CLAUDE_CODE_CONFIG_PROBE=1` and supplies SDK transport
arguments without `--resume`. That exact mode receives the follower's metadata
initialization after the existing binding/source have been verified. It cannot
select a conversation, replay or follow messages, or write a readiness receipt.
An ordinary no-resume invocation still fails. The probe also never executes the
original CLI.

`history: "native"` requires an explicit `start_offset` at a complete JSONL
boundary. All records after that boundary, including records appended before
the wrapper finishes initializing, are delivered. There is no implicit
"start at current EOF" behavior. The caller must obtain the boundary matching
the history already loaded by the native host. A new viewer with no history can
use zero. `history: "replay"` emits the existing source after initialization and
is appropriate when the host has not already displayed that history. The isolated
profile helper now enforces that condition for the exact bound session through
the two pinned patches described below.

The follower validates the whole existing source before responding to initialize,
then polls it every 100 ms. Partial UTF-8/JSON lines are retained until complete.
Identical event UUIDs are deduplicated; changed UUID payloads, another session,
truncation, replacement, in-place changes to consumed bytes, and version changes
stop the follower. Consumed bytes are rehashed on each poll, including at EOF;
this development implementation is not optimized for large histories. A source EOF
does not mean the owner's turn has completed.

## Actual SDK protocol and native consumers

The evidence below comes from the installed official 2.1.295 files. Minified
function names and character offsets identify this specific build; they are not
cross-version API guarantees.

| Evidence | Native behavior used by this adapter |
| --- | --- |
| `extension.js`, `yS`, around character 3315495 | Reads `claudeProcessWrapper`; uses it as executable and the bundled CLI path as the first argument. |
| `extension.js`, SDK transport, around 2564636 | Uses `--output-format stream-json --verbose --input-format stream-json`; supports `--resume`. |
| `extension.js`, `buildInitializeRequest`, around 2593240 | Sends `type: control_request`, `request_id`, `request.subtype: initialize`, with optional hooks, plugins, prompts, and host capabilities. |
| `extension.js`, SDK request/response transport, around 2603197 | Correlates `control_response.response.request_id`; success contains `response`, error contains `error` and optionally `error_code`. |
| Bundled CLI, search `case"initialize":{try{let D=zre` | Its outbound-only native REPL connection acknowledges initialize with empty commands/agents/models, normal output style, account object and PID. Other controls are rejected in outbound-only mode. |
| Bundled CLI, schemas `VF`, `VY`, and handler `I.request.subtype==="get_settings"` | Initialize has required command/model/account metadata; `get_settings` returns `effective`, `sources`, and optionally runtime `applied` values. |
| `extension.js`, `X1.getSettings().then`, around 2979676 | After initialization resolves, the host automatically requests `get_settings`. Failure is caught and logged without stopping the message loop. |
| `extension.js`, `spawnConfigProbe`, around 3005053 | A 500 ms config fallback launches a no-channel SDK query with `CLAUDE_CODE_CONFIG_PROBE: "1"`, reads initialize and settings, then retires it. This is distinct from an attached viewer. |
| `extension.js`, `sb4`, around 1387072 | Maps native transcript user/assistant rows to SDK messages, preserving message content/UUID/timestamp and changing `sessionId` to `session_id`, with `parent_tool_use_id: null`. |
| Bundled CLI, schema `O0` and producer `tJe` | A real `system/init` contains owner model/tools/permission mode/version and other runtime fields. The follower does not synthesize this event from a transcript. |
| `webview/index.js`, `QH`, around 3281140; `VO`, around 3518674 | Renders actual user/assistant blocks and associates user `tool_result` blocks with prior `tool_use` IDs. |

The proxy acknowledges initialize using the native outbound-only shape. Its
empty command/model lists describe the follower's own available controls, not
an invented owner configuration. Owner model/permission values are included
only when observed in a real `system/init` source event. Requested hooks and
plugins are reported as unapplied; trust is not recorded. All other controls,
including `get_settings`, return a correlated `read_only_follower` error. There
is no owner RPC to retrieve current settings yet, so the proxy does not invent
an empty effective-settings snapshot.

The wire envelopes are:

```json
{"type":"control_request","request_id":"request-1","request":{"subtype":"initialize"}}
{"type":"control_response","response":{"subtype":"success","request_id":"request-1","response":{"commands":[],"agents":[],"output_style":"normal","available_output_styles":["normal"],"models":[],"account":{},"pid":123}}}
{"type":"control_response","response":{"subtype":"error","request_id":"request-2","error":"Read-only follower: interrupt is unavailable; use the executing owner","error_code":"read_only_follower"}}
```

The adapter may emit a clearly attributed native `system/informational` notice
about follower attachment. It never manufactures assistant text, tool results,
usage, `system/init`, or successful/failed turn results. Existing SDK view events
are passed through without changing their content. Owner control requests are
not forwarded to the second SDK host: doing that could execute permissions or
hooks twice. Inbound user messages stop this read-only attachment with an explicit
stderr error and nonzero exit; they do not become an owner prompt.

For `transcript` sources the supported live content is main-session user and
assistant messages, including tool calls and results. Sidechains and team rows
are excluded. Multiple assistant blocks belonging to one API message are
supported. Branch rewrites and compaction need the native history reconstruction
or the actual SDK event stream; the transcript follower fails rather than
silently displaying a false linear conversation. Transcript-only observation
does not provide reliable turn-completion or permission-wait state.

## Native login display gate

For an isolated read-only viewer profile, the official supported setting is:

```json
{
  "claudeCode.disableLoginPrompt": true
}
```

The installed `package.json` declares it as a boolean, default false, for cases
where authentication is handled externally. It does not create credentials or
make a provider request succeed. Configure it before opening the follower
webview; the extension passes it to the `h7` authentication manager constructor.

The actual native path is:

| Evidence | Effect |
| --- | --- |
| `extension.js`, `h7.isAuthLoginDisabled()`, around 1349335 | Exposes this setting through `loginPromptDisabled` in host initialization/state. |
| `webview/index.js`, `FC1`, around 3335430 | Allows a session view when `loginPromptDisabled` is true, even with null auth status. |
| `webview/index.js`, `DC1`, around 3335678 | Suppresses automatic login prompts for `authentication_failed` messages while that setting is true. |
| `extension.js`, `refreshCliAuthStatus`, around 2999735 | Separately invokes `ORIGINAL_CLI auth status --json` through the wrapper; this is not the SDK initialize request. |
| `webview/index.js`, login component, around 4925336 | Displays `cliStartError` inside the login component; it is not a condition in `FC1`. |

The follower intentionally rejects `auth status --json`: it neither launches the
owner CLI to inspect credentials nor fabricates a logged-in account. The native
host logs that the status check did not return parseable JSON. When provider
configuration is available only inside CLI settings, that failed probe also
prevents the host from learning the CLI's provider verdict. With the default
login setting this can leave the webview at its welcome/login page before the
conversation channel is launched.

`disableLoginPrompt` is the native display control for this externally owned
authentication case. It lets the host show the existing session without a
manufactured `loggedIn` answer. It does not resolve the owner's quota error.
The separate config-probe support above prevents the welcome screen's metadata
fallback from being mistaken for a new conversation or a ready viewer.

## Readiness and history handoff

If `ready_file` was provided, the proxy writes it atomically with mode 0600 after
it has sent initialization output **and received the host's following
`get_settings` request**. The receipt contains `schema: 1`,
`stage: "host_initialize_observed"`,
`evidence: "get_settings_after_initialize"`, exact `session_id`, proxy `pid`,
producer version, source device/inode, start offset, and a timestamp. It never
overwrites an existing file. Use a fresh path for every attachment.

This proves that the SDK host has progressed past initialization, not that the
webview has rendered pixels or that the owner is alive. The receipt remains as
a handshake record after exit and is not a health check. The controller must
also track the attachment process and source identity.

The history boundary matters because the native `loadFromMessages()` first
rebuilds history through `VO(..., false)`, then calls `launchClaude()`. In `VO`,
assistant UUID deduplication only occurs in the branch where
`hasStreamingMessages` is true. The ordinary branch appends the message. User
replay has separate UUID handling; assistant transcript replay does not. Replaying
the full transcript blindly can therefore duplicate assistant rows. Injecting a
fake streaming event to enable that branch would not be faithful observation.

For a controlled `history: "native"` attach check, keep the owner idle, capture the exact
complete transcript EOF, open the native viewer using that boundary, wait for
the readiness receipt, and only then submit the next owner prompt. A general
running-session attach in that mode still needs a native history cursor
acknowledgment. The wrapper cannot infer what an unpatched webview displayed.

### Isolated replay profile

For `history: "replay"`, `native_view.py` writes local copies of `extension.js`
and `webview/index.js` in a new profile's extension directory. It never writes
through the links to installed resources. `claude_vscode_history.py` checks both
original hashes and unique anchors; preparation and reopening refuse changed or
unknown builds.

For the exact bound session, the host response supplies an empty message history.
The proxy then provides the only displayed history and live-message stream from
one descriptor. The host still performs its native history/diff reads and returns
diff metadata; this patch does not remove those reads or bypass their errors.
Other sessions retain their original host response but cannot execute through the
bound wrapper.

The native webview's receive loop keeps successful message UUID/content identities
on its session model across follower-channel restarts. Identical user, assistant
and streaming frames do not render twice. Real state frames such as init, status,
task updates and results still reach the native reducer after reconnect. Changed
content under an existing UUID closes only the follower channel and displays an
error. It never interrupts or resumes the actual executing owner.

An offline test using the installed receive-loop and render methods reproduced
two rows becoming four on reconnect; the patched path stays at two. A new actual
native panel displayed the real failed session once with both patches applied.
Terminating just its reader displayed the native SIGTERM error. Successful live
owner progress and an actual same-model channel restart remain separate checks.

## Verification performed and work still required

On 2026-10-25 the real bundled CLI was started once in a temporary directory with
temporary HOME/config directories, no setting sources, empty strict MCP config,
no credentials copied, and nonessential traffic disabled. Only `initialize` and
`get_settings` control requests were supplied; no user prompt was supplied.
It exited 0 and returned two successful control responses. Initialize contained
the documented command/model/account/output-style/PID fields, and settings had
`effective`, `sources`, `applied`. There were no assistant/result/system-init
events and no stderr. This checked the actual handshake without an inference
turn or connection to an existing user conversation. No transcript from that
probe is saved in this repository.

A separate read-only check used one newly created temporary native-TUI session
whose owner received HTTP 403 with a quota error. Its actual 2.1.295 transcript
was accepted by the follower. Replay preserved the original error text and
`<synthetic>` assistant model marker; native history mode at the captured EOF
did not resend it. Neither mode invented a result or interpreted EOF as success.
This checked conversion of a real failure record, not successful model work or
rendering in the VS Code webview. The source and session identity were not copied
into the repository.

The offline suite uses temporary sources and a Python subprocess running this
proxy. The original fake CLI is an executable sentinel and remains unexecuted.
It covers fragmented/coalesced records, exact-session/version checks, inode
replacement/truncation, history boundaries and attach-time appends, tool content,
raw SDK events, control isolation/refusal, actual-source completion only, and
readiness. Run:

```sh
python3 -m unittest discover -s tests -p test_claude_vscode_proxy.py -v
```

The official panel has now displayed the real quota-failure session in an isolated
profile, including after the replay patches. Still required: successful live owner
progress, native reconnect validation and integration of authoritative owner
events. The component does not make a 2.1.308 producer compatible or establish
three-surface operation. See [actual validation](native-validation.md).

Evidence file SHA-256 values:

```text
extension.js                  23f91879a93db01805307472aeb648ebd0b4b06ee625aa6cfe5dca724ae1c902
webview/index.js              0f11dd8dbf644aa03d46d3d91b3edbb78dcd35eddd389e2b0cb7507ba4008eb9
resources/native-binary/claude 4503bfe11a6c7fcc1e0b39b5e0d347c04248f750b03b0977b3ad6b531fe6f358
```
