# 安装与恢复

[English](installation.md)

当前是限定客户端版本的 Linux 开发集成。Windows/macOS 执行适配和真实验收未完成。
每台电脑需要独立验收，不复制另一台电脑的账户、任务状态或验收回执。

需要 Python 3.9+、Linux pidfd 支持、Git、VS Code，以及[验证记录](native-validation.md)
中的原生客户端版本。沿用现有登录和供应商配置。在仓库目录执行：

```sh
python3 -B install.py check --target both
python3 -B install.py install --target both
```

只有 `doctor.runtime_ready=true` 才能安装。也可指定 `--target claude` 或
`--target codex`。默认安装到 `~/.claude/skills/claudex-bridge` 和
`~/.codex/skills/claudex-bridge`，尊重 `CLAUDE_CONFIG_DIR`、`CODEX_HOME`。
使用完整文件副本，安装后不依赖仓库位置。不复制凭据、客户端程序、本地验收证据、
任务状态或旧 `codex-worker`。

安装器先检查两个目标，拒绝覆盖非本安装器管理的目录或用户修改的副本；激活失败会
回滚。更新后需要重新通过本机验收，再运行安装命令。修改过 Skill 时先保留自己的改动。

客户端重新加载 Skill 后，可使用 `/claudex-bridge` 或自然语言指定使用它。
发起方按随包附带的[原生接入](../skills/claudex-bridge/references/native-setup.md)和
[任务流程](../skills/claudex-bridge/references/workflow.md)绑定并打开视图。
安装命令本身不启动执行者，也不接管已有对话。当前执行者是 **Codex 桌面端和
Claude 原生 TUI**；Claude 桌面端与 VS Code 的跟随视图只读，聊天和权限确认在
执行端完成。原生客户端首次工作区、URI 和插件信任提示仍需正常处理。

Claude 桌面跟随视图使用可删除回滚的程序副本，独占原客户端配置目录。
确认原客户端空闲后正常退出，再打开副本；退出副本后重新打开原客户端。
不能为了接入而强退其他任务。关闭跟随窗口不会停止执行者。

```sh
python3 -B install.py uninstall --target both
```

卸载只删除未被修改的受管 Skill 副本，保留会话、证据、登录、CC Switch、全局规则和
`codex-worker`。删除临时视图目录前先正常关闭其窗口；保留恢复需要的原始会话和基线。

## 排查

| 现象 | 处理 |
| --- | --- |
| doctor/安装返回 2 | 阅读 `runtime_blockers`；不能伪造回执或换用未经验证的执行者。 |
| 客户端或代码更新 | 原回执失效，对当前安装重新验收。 |
| Claude 启动环境不符 | 正常恢复空闲的原会话，并加载返回的 hooks 和 `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`。 |
| detached/unresolved | 用保存的任务执行 `task-watch`，不要重复派工。 |
| 已请求停止 | 继续核对原轮次及命令清理；请求或回执不代表停止完成。 |
| 仍有属于任务的进程 | 保持未解决，检查执行端和精确命令；不能误杀其他会话或通过旧结果。 |
| 额度或权限失败 | 在实际执行端处理或如实汇报，不自动修改供应商和权限策略。 |
| 跟随窗口断开 | 重新打开原绑定，只恢复显示，不启动新推理。 |
| review_stale | 重新阅读实际差异、记录当前测试并验收。 |

自动验收和返工依赖发起方会话保持运行。停止不能撤销已完成的副作用，也不能保证
终止故意脱离或清除进程归属的外部任务。正常 Codex 命令会在中断前固定进程身份，
保留派工前已有的进程及其他会话；清理未确认时不报告完成。
