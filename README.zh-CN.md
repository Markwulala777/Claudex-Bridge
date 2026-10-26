<p align="center">
  <img src="assets/readme/icon.svg" width="96" height="96" alt="Claudex-Bridge：橙色与蓝紫色双 C 相扣，代表 Claude 与 Codex 协作">
</p>

<h1 align="center">Claudex-Bridge</h1>

<p align="center">Claude ↔ Codex · 双向派工，原会话返工，发起方验收。</p>
<p align="center"><a href="README.md">English</a> · <b>简体中文</b></p>

和 Claude 或 Codex 聊清需求，让另一方实现。发起方阅读实际改动、核验测试，
不通过就交回原会话修改，通过后向你汇报。

> **Linux 开发预览版。** 双向流程已于 2026-10-26 完成实测验收。
> 本次整理后的源码须重新进行本机验收，才能安装；Windows 和 macOS 尚待实现与验证。
> [验证证据与当前范围 →](docs/native-validation.md#source-cleanup-after-acceptance)

## 一个任务，走完交付闭环

**聊需求 → 派工 → 实现与测试 → 验收 → 返工或通过**

完成接入后，在发起方会话中提出任务：

> `/claudex-bridge`
>
> 让 Codex 实现 CSV 导出。完成后检查实际 diff 和测试结果。
> 验收不通过时，给出具体修改要求，交回原会话修复。
> 通过后向我汇报；遇到无法推进的问题，说明卡点。

也可以由 Codex 发起、交给 Claude 执行。同一任务主角色固定，追加反馈继续原会话。
自动验收和返工需要发起方会话保持运行。Codex 可委派独立子任务，Claude 默认不开子代理。

## 从 Linux 开始

需要 Python 3.9+、Linux pidfd 支持、Git，以及[验证记录中的原生客户端](docs/native-validation.md)。
在仓库目录检查兼容性，检查过程不调用模型：

```sh
python3 -B skills/claudex-bridge/scripts/bridge.py doctor --json
```

只有 `runtime_ready=true` 时，才为两端安装 Skill：

```sh
python3 -B install.py check --target both
python3 -B install.py install --target both
```

客户端重新加载 Skill 后，按[原生接入说明](skills/claudex-bridge/references/native-setup.md)绑定会话、
打开查看窗口。安装保留现有账户、供应商、CC Switch、全局规则和另行安装的
`codex-worker`。[安装、升级与卸载 →](docs/installation.zh-CN.md)

## 在三端查看同一会话

| Agent | 执行与输入入口 | 实时原生视图 |
| --- | --- | --- |
| Codex | 桌面端 | 桌面 · 终端 · VS Code |
| Claude | 终端 | 终端 · 桌面副本 · VS Code |

每个 Agent 只有一个实际执行入口。其他窗口只读跟随，输入和权限确认留在执行端，
首次接入需要手动设置。Claude 桌面副本独占配置目录，打开前须正常退出原客户端。
限定版本的 Codex 终端重连后可能只显示最近历史，实时更新仍会继续。
[查看窗口的设置与限制 →](docs/native-views.md)

## 当前状态与文档

已验收版本为 `d379c41`：两个方向均完成派工、拒收、原会话修复、追加反馈、重连和停止。
后续源码整理改变了包指纹，已安装副本仍保留原验收版本。每台电脑、每个包版本都需要
对应的原生验收。本仓库公开的是 Linux 开发预览源码，Windows 和 macOS 支持仍待完成。

- [工作流程、命令与恢复](skills/claudex-bridge/references/workflow.md)
- [验证记录与离线测试命令](docs/native-validation.md)
- [实施清单](docs/implementation.md)

[MIT 许可证](LICENSE)。每台电脑独立保存任务状态，不做跨电脑同步。
