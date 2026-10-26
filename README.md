<p align="center">
  <img src="assets/readme/icon.svg" width="96" height="96" alt="Claudex-Bridge: interlocking orange and blue C forms representing Claude and Codex collaborating">
</p>

<h1 align="center">Claudex-Bridge</h1>

<p align="center">Claude ↔ Codex · Delegate, review, revise.</p>
<p align="center"><b>English</b> · <a href="README.zh-CN.md">简体中文</a></p>

Work through requirements with Claude or Codex, then let the other agent implement
them. The initiating agent reviews the actual changes and tests, requests revisions
in the same session, and reports back to you.

> **Linux development preview.** Both directions passed live acceptance on
> 2026-10-26. This updated checkout needs fresh local acceptance before installation.
> Windows and macOS are pending. [Evidence and current scope →](docs/native-validation.md#source-cleanup-after-acceptance)

## One task, a complete loop

**Discuss → Delegate → Implement & test → Review → Revise or accept**

Once set up, ask the initiating agent:

> `/claudex-bridge`
>
> Let Codex implement CSV export. Check the actual diff and tests when it finishes.
> If it fails review, send specific fixes back to the same session.
> Report the result after it passes, or explain what is blocking it.

Use the same flow from Codex to delegate to Claude. Roles stay fixed for the task;
your later feedback continues the original sessions. The initiator must remain
active for automatic review and rework. Codex may delegate independent subtasks;
Claude subagents are off by default.

## Start on Linux

You need Python 3.9+ with Linux pidfd support, Git, and the
[validated native clients](docs/native-validation.md). From the repository, check
compatibility without making model calls:

```sh
python3 -B skills/claudex-bridge/scripts/bridge.py doctor --json
```

Only when `runtime_ready=true`, install the Skill for both agents:

```sh
python3 -B install.py check --target both
python3 -B install.py install --target both
```

Reload skills in each client, then follow [native setup](skills/claudex-bridge/references/native-setup.md)
to bind the conversations and open their views. Installation preserves existing
accounts, providers, CC Switch, global rules, and the separately installed
`codex-worker`. [Install, update, or uninstall →](docs/installation.md)

## Follow the same conversation

| Agent | Execution and input | Live native views |
| --- | --- | --- |
| Codex | Desktop | Desktop · Terminal · VS Code |
| Claude | Terminal | Terminal · Desktop copy · VS Code |

Each agent has one execution owner. Other windows are read-only followers; input
and permission prompts stay with the owner. First attachment is a manual setup
step. The Claude Desktop copy needs exclusive use of its profile: quit the normal
client first. Codex terminal reconnection can show only recent history on the
pinned client version; live updates continue. [View setup and limits →](docs/native-views.md)

## Status and documentation

The tested build is `d379c41`: both directions completed dispatch, rejected review,
same-session repair, user feedback, reconnection, and stopping. The subsequent
source cleanup changes the package fingerprint; existing installed copies retain
their accepted version. Each computer and package version needs its own native
acceptance. This public source is a Linux development preview; Windows and macOS
support remains pending.

- [Workflow commands and recovery](skills/claudex-bridge/references/workflow.md)
- [Validation record and offline test command](docs/native-validation.md)
- [Implementation checklist](docs/implementation.md)

[MIT license](LICENSE). Each computer keeps its own task state; cross-computer
synchronization is outside the scope.
