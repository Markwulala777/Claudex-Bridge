# Claudex Bridge

[English](README.md)

让 Claude 和 Codex 互相派工，由发起方验收和安排返工。

**当前状态：完整的原生三端同步仍有阻塞，尚未发布。** 这是本地开发仓库。
已实现 Linux 双向任务状态、验收证据和原生执行适配器，并在 Claude 桌面端
观察到真实派工及失败结果。当前 Claude 供应商仍报额度不足，成功实施与返工闭环
尚未实测通过；原生三端完整体验和跨平台安装仍未交付。

目标流程：和任一主 Agent 聊需求 → 另一主 Agent 实现、测试 → 发起方完整验收 →
不通过就继续原会话返工 → 验收通过或遇到卡点后汇报。用户追加反馈时继续原任务；
独立新需求创建新任务。每个会话只能有一个实际执行者。

主角色在同一任务中固定。Codex 执行时可以按需使用子代理处理独立工作，
由主 Codex 汇总、验证并负责最终交付。Claude 默认不开子代理，以节省额度。
最终验收由发起方主 Agent 亲自完成。

## 已落地的内容

- 修正了 Skill 和实际派工消息中的子代理规则，移除了引导进程的多代理禁用参数。
- 保留原执行器的精确会话/轮次编号、重复派工防护、断线恢复、验收记录和停止逻辑。
- 新增只读兼容性检查，区分“找到接口”和“真实联调通过”。
- 新增 Codex 原生 TUI 网关，已连接真实 Desktop 会话并显示持续进度。
- 新增 Claude 官方 VS Code 只读适配器和隔离配置启动器，已显示真实临时会话
  及供应商的额度不足错误；成功执行、自动返工尚未完成实测。
- 新增可删除回滚的 Linux Claude 桌面端测试副本，已显示绑定会话历史、自动追加的
  派工和额度错误；接入原生轮次状态后，失败会停止忙碌指示。原客户端已恢复。
- 新增 Claude 原生 socket 派工与会话插件，真实匹配请求、原轮次和失败回执。
- 新增双向任务命令、需求版本、完整 Git 内容快照、实际测试记录、验收覆盖、同会话
  返工和停止请求；断线不会自动重发，文件变化会使旧验收失效。
- 修复 VS Code 隐藏原生 peer 派工正文的问题，保留来源标记和原文。
- 修复 VS Code 重连时历史重复显示，并分开本机启用条件与三平台发布条件。
- [实施与验收清单](docs/implementation.md)、[客户端接口调查](docs/native-clients.md)及离线回归测试。
- [真实验证记录](docs/native-validation.md)与[开发用查看命令](docs/native-views.md)。

原有 `codex-worker` 仍是兼容入口，没有被新 Skill 替换。
双向开发接口见 [工作流程命令](skills/claudex-bridge/references/workflow.md)。正式派工仍由
本机验收门槛拦截；完整安装器、成功双向原生闭环以及 Windows/macOS 验证尚未完成。

## 本机检查

在仓库根目录执行，要求 Python 3.9+：

```sh
python3 skills/claudex-bridge/scripts/bridge.py doctor
python3 skills/claudex-bridge/scripts/bridge.py doctor --json
python3 skills/claudex-bridge/scripts/bridge.py doctor --require-release
python3 -m unittest discover -s tests -v
```

`doctor` 在本机执行能力有阻塞或未验证项时返回退出码 `2`；`--require-release`
额外要求三平台验收通过。目前两个条件均未通过。Linux 完整验收后可先启用本地
开发版，GitHub 发布仍等待三平台通过。参见[验收证据约束](docs/acceptance-evidence.md)。
它只读本地安装信息，不连接会话
socket、不运行模型 CLI、不修改配置、不创建任务。只报告是否存在自定义 Claude
供应商，不输出供应商地址和凭据。协议匹配、socket 存在和离线测试通过都不能
代替真实三端联调。

目标支持 Linux、Windows、macOS；每台电脑独立运行，不做跨电脑同步。
保留的执行器目前仅适用于 Linux，Windows/macOS 尚未做真实联调。

采用 MIT 许可证。完整需求通过验收后再发布到 GitHub。
