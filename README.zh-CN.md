# Claudex Bridge

[English](README.md)

让 Claude 和 Codex 互相派工，由发起方阅读实际改动、核验测试、安排原会话返工并汇报。
同一任务的两个主角色保持固定。

**Linux 开发集成已于 2026-10-26 完成双向实测验收。** Windows/macOS 尚未实现并验证；
按既定三平台发布条件，目前没有发布到 GitHub。

## 已验证的流程

聊需求 → 派给一个执行者 → 实现与测试 → 发起方亲自验收 → 不通过则原会话返工，
直到通过或遇到具体卡点。用户追加反馈继续原任务和两个原会话。自动闭环要求发起方会话持续运行。

两个方向都在临时 Git 项目中完成了故意拒收、同会话修复、独立验收测试、追加反馈、
监控中断恢复、重复派工拦截、原生窗口重连和前台命令停止。
已有文件修改和未跟踪文件均保留；真实 Claude 额度失败没有误报成功。
参见[验证记录与限制](docs/native-validation.md)。

| 产品 | 实际执行与输入入口 | 同时查看的原生界面 |
| --- | --- | --- |
| Codex | 桌面端 | 桌面端、配套终端 TUI、官方 VS Code 插件 |
| Claude | 原生终端 TUI 2.1.295 | TUI、可删除的桌面副本、官方 VS Code 插件 |

查看窗口的输入框为只读，权限提示在执行入口处理。安装器不会自动创建会话或打开所有窗口。
Claude 桌面副本独占原配置目录，须先正常退出原客户端。这一版尚不支持在任意前端之间自由切换输入。
当前 Codex 客户端有历史分页缺陷，重连后的终端可能只能显示桌面已加载的最近轮次；
实时更新仍可继续，缺失的旧历史会明确标记，不会导入历史来伪装完整。

Codex 执行者可委派独立子任务，并负责整合与验证。Claude 默认不开子代理。
最终验收由发起方主 Agent 亲自完成，执行者不能自批通过，不递归调用桥或交换主角色。

## 在完成验收的 Linux 电脑上安装

需要 Python 3.9+、Linux pidfd 支持、Git，以及记录中的原生客户端版本。

```sh
python3 -B skills/claudex-bridge/scripts/bridge.py doctor --json
python3 -B install.py check --target both
python3 -B install.py install --target both
```

客户端重新加载 Skill 后使用 `/claudex-bridge`，再按包内[原生接入说明](skills/claudex-bridge/references/native-setup.md)
和[工作流程](skills/claudex-bridge/references/workflow.md)操作。安装器向两端个人目录复制完整 Skill，
保护已有修改，部分安装失败会回滚。保留原 `codex-worker`、账户、CC Switch、供应商、全局规则和共用任务状态。

Doctor 不调用模型。本机启用必须有匹配当前电脑、代码和客户端版本的真实验收证据。
把仓库复制到另一台电脑不会转移验收结果。`--require-release` 另要求 Windows/macOS 验收，
目前仍不通过。参见[证据约束](docs/acceptance-evidence.md)。

[安装、升级、卸载与排障](docs/installation.zh-CN.md) ·
[实施清单](docs/implementation.md) · [历史接口调查](docs/native-clients.md)

## 验证

```sh
CLAUDEX_TEST_NATIVE_TUI=/opt/codex-desktop/resources/codex \
  python3 -B -m unittest discover -s tests
```

回归测试使用临时项目与协议替身，不调用真实模型。可选的原生 TUI 测试检查客户端如何显示测试数据；
实际原生验收单独记录。凭据、对话、任务状态、客户端副本和本机验收记录不提交到 Git。

MIT 许可证。每台电脑状态独立，不做跨电脑同步。三平台条件全部通过后再发布 GitHub。
