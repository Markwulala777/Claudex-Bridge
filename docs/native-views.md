# Development commands for native views

These commands attach read-only views. They do not implement the bidirectional
dispatch/review loop and are not an installer for the released skill. They were
checked on Linux only. See [actual validation](native-validation.md) before using
the observations as evidence of support.

## Codex

Use an already-owned Desktop conversation and its matching original rollout.
Create a private, user-owned directory for a new socket; do not use a directory
writable by another user or an existing socket. The gateway validates both IDs
and installed IPC versions. It does not open a new conversation or take ownership.

```sh
mkdir -m 700 /absolute/new-private-directory
python3 skills/claudex-bridge/scripts/codex_tui_gateway.py \
  --thread EXACT_UUID --rollout /absolute/original-rollout.jsonl \
  --socket /absolute/new-private-directory/tui.sock
```

After the gateway prints its endpoint, use a real terminal with the checked
Desktop-bundled CLI (0.163.0-alpha.11.1):

```sh
/opt/codex-desktop/resources/codex \
  --remote unix:///absolute/new-private-directory/tui.sock \
  resume EXACT_UUID --no-alt-screen
```

The word `resume` here subscribes the TUI to this read-only gateway; the gateway
never forwards `thread/resume` or an execution command to the owner. Submit work,
answer questions and stop work in the owner. Closing this view does not stop it.
The native TUI currently labels this passive mode “Viewing sub-agent.” No new
subagent is created. Use the same original IDs when reconnecting, with a fresh
gateway/socket. See [protocol limits](codex-tui-protocol.md).

The official IDE accepts `vscode://openai.chatgpt/local/EXACT_UUID`. Opening it may
require VS Code's normal URI and workspace-trust confirmation. It does not itself
provide automatic profile preparation for Codex in this development CLI.

## Claude official VS Code follower

Use the official extension and owner version 2.1.295. Keep the original owner idle
while the native panel loads history. Record its complete JSONL byte boundary,
then create a binding as specified in [the protocol document](claude-vscode-protocol.md).
Use a fresh readiness path per attachment. Do not calculate the boundary after a
running owner has already moved ahead of what the native panel loaded.

```sh
python3 skills/claudex-bridge/scripts/bridge.py prepare-claude-view \
  --directory /absolute/new-view-directory \
  --project /absolute/existing-project \
  --binding /absolute/binding.json \
  --extension /absolute/anthropic.claude-code-2.1.295-linux-x64

python3 skills/claudex-bridge/scripts/bridge.py open-claude-view \
  --directory /absolute/new-view-directory
```

Preparation validates the source and writes only the new directory. It creates a
separate VS Code user-data/extension directory with the official extension linked,
an executable wrapper, and `disableLoginPrompt: true`. No credentials are copied.
The native extension may log that its auth-status probe is unavailable: this
follower exposes no authentication endpoint and never claims to be logged in.

The first window may show VS Code onboarding, workspace trust and a URI prompt.
Complete those for the selected project. The helper returns `open_requested`,
not proof of native rendering. A readiness receipt proves the SDK host processed
initialization, not that the owner is alive or a task completed. Observe the native
panel before submitting another owner turn. The follower refuses messages entered
in its composer; use the original owner to execute work.

Do not add another extension symlink after VS Code has catalogued the isolated
directory: its profile/obsolete catalog may mark an unregistered link for removal.
This helper prepares its selected extension before the first launch. It is not a
general extension installer or a portable Windows launcher.

If a view or binding has been used, prepare a fresh attachment and history boundary
instead of overwriting the readiness record or silently switching its session.
Close only test windows and processes you created. Remove their private profile
directories when no longer needed; keep the original owner transcript intact.
