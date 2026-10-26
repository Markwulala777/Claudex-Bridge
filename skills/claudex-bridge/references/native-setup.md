# Native setup on the validated Linux installation

Use the actual installed Skill directory below as `<skill>`. Check `doctor`
first. This version's owners are Codex Desktop and Claude native TUI 2.1.295;
other attached views do not execute. Do not start a second owner, switch a saved
task's roles, or use a different conversation after disconnection.

For an existing task, recover its saved session IDs, transport and prepared view
directories. Reopen those views and use `task-watch`. For a new task, bind distinct
main conversations and a fixed project before `task-create`. A new independent
task needs a new executor conversation. Native onboarding and owner creation are
not performed by the Skill installer; never describe an unopened view as attached.

## Claude owner and views

Prepare task-local hooks using `prepare-claude-owner` as described in
[workflow.md](workflow.md). Start/resume the one idle native TUI with the returned
plugin arguments and `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`. Use its actual
existing account/provider; do not copy the development test's process-specific
account/model choices into user settings. Resolve native plugin trust normally.

After the native ready event and a real transcript establish the same session,
write a private binding with actual values (these are placeholders):

```json
{
  "schema": 1,
  "session_id": "EXACT_CLAUDE_UUID",
  "source": "/absolute/original/transcript.jsonl",
  "source_format": "transcript",
  "claude_code_version": "2.1.295",
  "history": "replay",
  "desktop_version": "3.6556.1",
  "cwd": "/absolute/project",
  "owner_state_dir": "/absolute/private-owner"
}
```

Use new private output directories and the supported installed official extension:

```sh
python3 <skill>/scripts/bridge.py prepare-claude-view \
  --directory /absolute/private/vscode --project /absolute/project \
  --binding /absolute/private/binding.json --extension /absolute/official-extension
python3 <skill>/scripts/bridge.py open-claude-view --directory /absolute/private/vscode
python3 <skill>/scripts/bridge.py prepare-claude-desktop-view \
  --directory /absolute/private/desktop --project /absolute/project \
  --binding /absolute/private/binding.json
python3 <skill>/scripts/bridge.py open-claude-desktop-view --directory /absolute/private/desktop
```

The Desktop copy requires the normal client to be closed safely first. It uses the
existing profile exclusively; never run both together. Observe actual session
content and progress in the native windows. These are read-only followers: enter
requirements, answer permissions and stop work in the native TUI owner. Closing
the copy and reopening the original restores normal Desktop use.

## Codex views

Use the exact existing Desktop-owned thread and its original rollout under
`CODEX_HOME/sessions`. The Desktop window opens when the adapter attaches it.
Open `vscode://openai.chatgpt/local/EXACT_CODEX_UUID` in the official extension;
complete its normal URI/workspace trust. Do not start another CLI inference.

For the native TUI follower, use a new socket inside a private user-owned directory:

```sh
python3 <skill>/scripts/codex_tui_gateway.py \
  --thread EXACT_CODEX_UUID --rollout /absolute/original-rollout.jsonl \
  --socket /absolute/private/tui.sock
mkdir -m 700 /absolute/private/tui-home
CODEX_HOME=/absolute/private/tui-home TERM=xterm-256color /opt/codex-desktop/resources/codex \
  --remote unix:///absolute/private/tui.sock resume EXACT_CODEX_UUID --no-alt-screen
```

Keep the gateway running while this view is open. The native TUI labels its
read-only mode “Viewing sub-agent”; opening it creates no subagent. Permissions
and inputs stay with the Desktop owner. Reconnect with the same IDs, never
`--last`. Verify each required native view before dispatch. First-time VS Code
profile preparation and conversation creation still need the native client flow.
Only this read-only TUI process uses the empty private `CODEX_HOME`, so native
onboarding preferences stay out of the user's real configuration. Do not copy
credentials into it. The gateway and workflow commands must keep the original
Codex home and shared worker state; this is not a second execution/state root.

If Desktop rebuilds its window, reopen the exact thread and restart the gateway
and TUI with those same arguments. Do not dispatch again. In the pinned client,
full history loading can fail with `thread/items/list is not supported yet`.
The adapter then uses fresh snapshots from that same owner and labels the view
`[recent history only]`. Older pagination reports unavailable; no rollout is
imported. Missing old turns remain unknown to workflow monitoring. Other protocol
or owner errors still fail closed. This limit does not create another inference.
