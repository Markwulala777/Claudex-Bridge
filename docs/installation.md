# Installation and recovery

[简体中文](installation.zh-CN.md)

This is a version-pinned Linux development integration. Windows/macOS execution
is not implemented or validated. Each computer needs its own native acceptance;
do not copy accounts, task state, or an acceptance receipt from another computer.

Use Python 3.9+ with Linux pidfd support, Git, VS Code, and the native versions in
[the validation record](native-validation.md). Keep the existing authenticated
clients and provider settings. Run from this repository:

```sh
python3 -B install.py check --target both
python3 -B install.py install --target both
```

Installation requires `doctor.runtime_ready=true`. `--target claude` and
`--target codex` install one copy. Defaults are `~/.claude/skills/claudex-bridge`
and `~/.codex/skills/claudex-bridge`; `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are
respected. Files are copied, so the installed Skill does not depend on this
checkout's location. The installer never copies credentials, native app bundles,
local evidence, task state, or the old `codex-worker`.

It preflights both destinations, refuses unmanaged or edited copies, and rolls
back if activation fails. To update, rerun it after the changed implementation
passes local acceptance. Preserve any locally edited Skill before updating.

After the product reloads its skills, invoke `/claudex-bridge` or ask the initiating
agent to use it. See the packaged [native setup](../skills/claudex-bridge/references/native-setup.md)
and [workflow](../skills/claudex-bridge/references/workflow.md) instructions. The
installer does not itself launch owners or take over an existing conversation.
Current execution owners are **Codex Desktop and Claude native TUI**. Claude
Desktop and VS Code followers show the same conversation but their composers
are read-only. Chat and answer native permissions in the owner. First-time native
workspace/URI/plugin trust still belongs to those clients.

The Claude Desktop follower is a removable copy that exclusively uses its normal
profile. After verifying the original is idle, quit it normally before opening
the copy. Close the copy normally before reopening the original. Never force-quit
unrelated sessions to make room. Closing a follower does not stop its owner.

```sh
python3 -B install.py uninstall --target both
```

Uninstall removes only unchanged managed Skill copies. It leaves conversations,
evidence, authentication, CC Switch, global rules and `codex-worker` intact.
Close prepared follower windows before removing their private view directories.
Never delete an owner transcript or task baseline needed for recovery.

## Troubleshooting

| Result | Action |
| --- | --- |
| Doctor/install exits 2 | Read `runtime_blockers`; do not manufacture a receipt or substitute an unverified executor. |
| Client/code updated | Revalidate the exact installation; the old receipt becomes stale. |
| Claude dispatch rejects launch environment | Normally resume its idle owner with the returned `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and hooks. |
| Task is detached/unresolved | Use `task-watch` on the saved task; do not dispatch again. |
| Stop requested | Watch the original turn and command cleanup. A request/acknowledgment is not completion. |
| Attributed commands still running | Keep the task unresolved; inspect its owner and exact commands. Do not kill other sessions or accept stale results. |
| Provider/permission failure | Resolve it in the actual owner or report it; do not change provider or permission policy automatically. |
| Follower disconnected | Reopen its same prepared binding; it must not start inference. |
| `review_stale` | Read the new actual diff, record current tests and review again. |

The initiating conversation must stay active for automatic review/rework. Native
cancellation cannot undo completed side effects or guarantee termination of
external jobs that deliberately detach or erase their process attribution.
Normal Codex commands are pinned before interruption; pre-existing processes and
other sessions are preserved. Unconfirmed cleanup blocks completion.
