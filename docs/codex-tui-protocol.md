# Codex native TUI through an existing Desktop owner

Historical component status, 2026-10-25: **the native TUI rendered the explicitly authorized, already
running Desktop conversation's history and subsequent real command output through
this adapter, without an execution mutation or second owner.** Offline fixture
regressions also pass. This is a Linux development adapter, not a claim that the
full Claude/Codex, three-surface, three-platform contract has passed.

## What changed

`skills/claudex-bridge/scripts/codex_tui_gateway.py` exposes a private Unix socket
with the app-server WebSocket protocol understood by the native Codex TUI. It
observes an already-owned Desktop conversation using the existing generic IPC
follower interface. Desktop keeps the original execution. No daemon, Desktop
restart, app-server execution, provider change or new conversation is needed.

The source connects with owner discovery and read-only history snapshots. It does
**not** call `Desktop.attach()` (which opens a conversation), `create_thread`,
`thread/resume`, `turn/start`, or `interrupt` on the actual backend. A TUI
`thread/resume` request only returns the selected owner's history and subscribes
that view to updates. Unknown requests and all execution/configuration mutations
are rejected. Closing the view does not stop the original task.

This is a passive native TUI view. Enter new instructions or approve tools in the
original owner. The native TUI currently labels a non-writable thread “Viewing
sub-agent”; that is the client's generic wording for `canAcceptDirectInput=false`,
not a new subagent or a changed main role.

## Observed native client protocol

The checked consumer is Desktop's bundled **`codex-cli 0.163.0-alpha.11.1`**.
The adapter checks this exact `initialize.clientInfo.version`, the `codex-tui`
client name, and the already-retained Desktop IPC protocol versions. The PATH
CLI `0.154.0` has not been validated with this adapter and is rejected.

The local binary's `app-server generate-json-schema --experimental` output and
actual isolated TUI handshakes establish this startup sequence (some reads run
concurrently):

1. `initialize`, then the `initialized` notification.
2. `account/read` and `thread/read` for the explicit UUID.
3. `model/list`, `configRequirements/read`, `collaborationMode/list`.
4. `thread/resume` with `excludeTurns=true`, normally null execution overrides,
   and possibly a non-empty `serviceTier` from the viewer's local configuration.
5. `thread/read(includeTurns=true)` for legacy history.
6. Optional inventory/history reads, including `thread/list`,
   `thread/loaded/list`, `thread/turns/list`, `hooks/list`, `skills/list` and
   `plugin/list`.

The adapter reports **its own** limited interface: no credential requirement, no
account identity, one original-owner model, and no executable skills/plugins.
These responses do not describe or modify the owner's account or installed
tools. `thread/resume` permissions, model, effort and working directory come from
the original owner; missing required metadata blocks attachment. Credential
refresh is rejected. A caller cannot select a different thread, inject history,
override execution settings or direct the owner to resume from another path.

The native TUI forwards a stored local `serviceTier` even for a passive remote
view. The gateway accepts that field only when it exactly matches the original
owner's current tier; the response still returns the owner's value and no backend
resume is sent. A conflicting tier is rejected with an explicit message. This is
an execution-setting comparison, not permission to alter the owner. Ordinary
commands do not require an empty or isolated `CODEX_HOME` on the checked local
configuration; arbitrary local execution overrides are not silently accepted.

The native TUI may attempt an ephemeral `thread/start` with
`threadSource=thread_title` for an unnamed conversation. The adapter returns a
clearly derived view-only title when the source title is absent to suppress this
auxiliary request; it never saves that title. The write rejection remains in
place if the consumer attempts it anyway.

## History and real-time updates

The selected rollout's first `session_meta` record must contain the exact thread
UUID, and the rollout must be under the current `CODEX_HOME/sessions` directory.
That record supplies original metadata. Text and tool messages come from the
authenticated existing owner's full snapshots, not generated placeholder output.

Most Desktop items already have app-server `ThreadItem` shapes. When the opening
user input exists only in `turn.params`, the adapter wraps that exact input as a
`userMessage` with a deterministic view ID. The real owner integration also
established these desktop-only representations:

- An acknowledged `steeringUserMessage` replaces the original user item in the
  Desktop timeline. The adapter restores its `serverUserMessageId`, client ID and
  exact input; the `steered` position marker is not a second message. Unacknowledged
  pending input is not presented as an admitted user message.
- A completed `userInputResponse` is a Desktop answer-form record. Its actual
  submitted answer strings become user content with the original item ID. The
  native view does not recreate the form, its questions/options, or pending answer
  interactions. Those remain in the original owner.
- Desktop `error` items come from app-server error notifications. New errors are
  forwarded as native `error` notifications with their original message,
  `willRetry`, error code and detail. Old transient retry banners are not replayed
  as current failures; final turn errors remain in the original turn metadata.

These conversions do not create assistant text or model output. Other unsupported
item types fail closed. The owner remains the source of statuses and native IDs;
fallback IDs for input found only in turn parameters are view metadata.

While subscribed, the adapter requests a fresh snapshot every 500 ms and emits:

- `thread/status/changed`, `turn/started`, `turn/completed`;
- `item/started`, `item/completed`;
- agent-message, plan, reasoning and command-output deltas computed from the
  exact increase in owner text;
- new original error notifications, without changing the turn's status.

Repeated snapshots emit no duplicate deltas. Item completion uses the owner's
completion anchors or explicit terminal statuses. A missing/disconnected owner
closes the view; it does not manufacture a completed turn. Text replacement that
cannot be represented as an append-only delta requires reconnecting to replay
authoritative history. The transport's item notification timestamps are gateway
observation times; they are not precise original execution timestamps.

History comes from the already-loaded original owner. The authorized real
conversation contained command, MCP, web, file-change, subagent-activity,
compaction, plan and image-view history alongside messages and reasoning. Native
TUI history rendering and live command output were observed; this does not prove
every specialized item's visual presentation or canonical/paginated history mode.
The adapter remains experimental.

## Run against an explicitly selected authorized task

Use the authorized existing task's saved UUID and rollout path. Do not substitute
`--last`, create another session, or use another conversation as a test target.

```sh
claudex_view_dir="$(mktemp -d)"
python3 skills/claudex-bridge/scripts/codex_tui_gateway.py \
  --thread EXACT_THREAD_UUID \
  --rollout /absolute/original-rollout.jsonl \
  --socket "$claudex_view_dir/tui.sock"
```

The command waits for one native TUI client and prints its endpoint. In another
terminal, on the observed Linux installation:

```sh
TERM=xterm-256color /opt/codex-desktop/resources/codex \
  --remote unix:///absolute/private-directory/tui.sock \
  resume EXACT_THREAD_UUID --no-alt-screen
```

The socket directory must already exist, be owned by the current user and not be
writable by group/others. The socket is mode `0600`; peer credentials are checked
on Linux. Existing paths are never replaced. Browser-origin upgrades and unknown
WebSocket extensions are rejected. The listener exits after that client closes
and removes only its own socket. Remove the empty temporary directory afterwards.
No new dependency beyond Python's standard library is required.

Use an empty private `CODEX_HOME` for the TUI viewer, as in the packaged
[native setup](../skills/claudex-bridge/references/native-setup.md), to keep its
native onboarding preferences out of the user's configuration. Do not set this
for the gateway or workflow, which need the original owner and state. A local
service tier, when configured, must match the original owner. `TERM` supplies a
supported terminal type; the TUI still needs an actual terminal.

## Verification and remaining acceptance

Offline protocol regressions:

```sh
python3 -B -m unittest discover -s tests -p test_codex_tui_gateway.py -v
```

An explicitly opted-in **real native TUI consuming offline fixtures**, with no
model request, real conversation, credentials or user config changes:

```sh
CLAUDEX_TEST_NATIVE_TUI=/opt/codex-desktop/resources/codex \
  python3 -B -m unittest discover -s tests -p test_codex_tui_gateway.py -v
```

The child TUI runs in a temporary config/history directory containing a matching
`service_tier = "priority"` default to cover the real TUI's non-null resume field.
Its PTY output was
observed to show the fixture history and two subsequent fixture message deltas,
and the adapter emitted the matching completion event. Its RPC capture contained
one `thread/resume` and no `thread/start` or `turn/start`. This proves the native
consumer accepts this transport; it is **not** a model/owner integration test.

Still required: exercise specialized item rendering and interrupted transports,
validate the complete dispatch/review/rework flow and the Claude direction, and
separately satisfy Windows/macOS gates. Do not mark release readiness from these
tests or from the Linux simultaneous-view observation below.

### Real Desktop owner observation

An additional explicitly authorized read-only check attached to the active
development conversation, with its exact saved UUID and matching rollout. No new
task or turn was created. The original Desktop owner returned 15 turns, including
the running turn. The bundled native TUI used one `thread/resume` against the
gateway and rendered 14 original history messages whose text fragments matched
the original snapshot. During a 50-second observation it received 10 item-start
events, 11 item-completion events, four command-output deltas and one reasoning
summary delta. All four new command-output deltas also matched actual PTY output.

The backend capture contained only initialization, owner discovery, history reads,
follower registration and discovery responses. The native client requested no
thread/turn start, steering or interruption. Two unsupported optional reads
(`account/bedrock/checkGovCloudRequirements` and `thread/goal/get`) were rejected
without preventing display. There were no owner-read failures. The temporary TUI,
listener and isolated config/history directory were cleaned up afterwards;
conversation content and private diagnostics were not added to the repository.

A second bounded, 180-second native view reconnected to the same existing UUID
and owner. It received 45 item-start and 45 item-completion events, 16 command
output deltas, three agent-message deltas and one reasoning-summary delta. Text
fragments from two new agent-message deltas matched the real PTY output. Again,
there was one native resume handshake, zero execution mutations and zero source
errors; its listener, TUI and temporary directory were removed at the end. This
checks deliberate view reconnection without resubmitting an execution, but does
not establish recovery after a network/owner failure.

This proves same-UUID history plus ongoing real progress in a native TUI while the
existing Desktop owner executes. It does not establish bidirectional control,
the complete dispatch/acceptance workflow, final task acceptance, or release
readiness.

### Default configuration and simultaneous Linux observation

A follow-up check used the user's normal environment and existing `CODEX_HOME`,
with no empty-home workaround. The real native `thread/resume` contained
`serviceTier="priority"`, exactly matching the current owner's tier. This exposed
and fixed an overly strict override rejection. The regression suite now checks
that matching defaults succeed, conflicting values fail, and the real native
fixture sends a non-null stored tier.

During 2026-10-25 14:16:15–14:17:15 UTC, the native TUI again attached to the same
running UUID under that default configuration. It rendered original text
fragments from 16 history messages, both new command-output deltas and both new
assistant-message deltas. The capture contained ten item starts/completions,
one native resume handshake, no execution mutations and no owner errors. The
existing `config.toml` remained byte-for-byte unchanged. The private listener,
temporary files and child TUI were cleaned up.

The coordinating agent separately confirmed screenshots of both the existing
Codex Desktop and the official Codex VS Code panel at 14:17:09.8 UTC, within that
same live TUI window. Both showed the same selected conversation/UUID and its
latest real commentary and tool progress. No task message was submitted for the
observation. This establishes simultaneous Linux observation of one existing
conversation in the three native clients. It is not an end-to-end test of
Claude-to-Codex dispatch, acceptance or rework, and does not establish reverse
or cross-platform support.

Primary implementation references:
[Codex app-server protocol](https://github.com/openai/codex/tree/main/codex-rs/app-server-protocol),
[native TUI app-server session](https://github.com/openai/codex/blob/main/codex-rs/tui/src/app_server_session.rs).
The installed binary's generated schema and captured handshake take precedence
over changes subsequently made to those `main` branch references.

## 2026-10-26 workflow and recovery follow-up

Both native workflow directions subsequently passed the checks recorded in
[native validation](native-validation.md). A Desktop window rebuild also exposed
an older-history pagination defect in this pinned client. Only its specific
`thread/items/list is not supported yet` error enables a fresh same-owner snapshot
read. The gateway preserves native history boundaries, labels its TUI name
`[recent history only]`, and returns an unavailable-older-history cursor/error.
It rejects incomplete individual turns rather than calling their items full.
Other failures remain closed. This path does not read rollout history, start a
model or send a turn. The actual TUI reconnected to the accepted workflow's final
result, including with a private empty viewer home; original config hashes stayed
unchanged in the latter check.
