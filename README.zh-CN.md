# Claudex Bridge

[English](README.md)

让 Claude 和 Codex 互相派工，由发起方验收和安排返工。

**当前状态：完整的原生三端同步仍有阻塞，尚未发布。** 这是本地开发仓库。
已保留 Linux 上的 Claude → Codex 桌面端执行器；它还不能实现两个产品各自的
原生命令行、桌面端、官方 VS Code 面板同时显示同一任务的实时进度。

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
- [实施与验收清单](docs/implementation.md)、[客户端接口调查](docs/native-clients.md)及离线回归测试。

原有 `codex-worker` 仍是兼容入口，没有被新 Skill 替换。
反向执行器和完整安装器尚未实现；不能把这个开发版本当作已经完成的双向工具使用。

## 本机检查

在仓库根目录执行，要求 Python 3.9+：

```sh
python3 skills/claudex-bridge/scripts/bridge.py doctor
python3 skills/claudex-bridge/scripts/bridge.py doctor --json
python3 -m unittest discover -s tests -v
```

`doctor` 在有阻塞或未验证项时返回退出码 `2`。它只读本地安装信息，不连接会话
socket、不运行模型 CLI、不修改配置、不创建任务。只报告是否存在自定义 Claude
供应商，不输出供应商地址和凭据。协议匹配、socket 存在和离线测试通过都不能
代替真实三端联调。

目标支持 Linux、Windows、macOS；每台电脑独立运行，不做跨电脑同步。
保留的执行器目前仅适用于 Linux，Windows/macOS 尚未做真实联调。

采用 MIT 许可证。完整需求通过验收后再发布到 GitHub。
