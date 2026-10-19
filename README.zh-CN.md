# Dual Agent Studio

面向 Windows 开发者的多 AI Coding Agent 协作桌面工作区，使用独立可信验证确认结果。

[English](README.md) · [安装与离线演练](docs/getting-started.md) · [架构](docs/architecture.md) · [安全边界](docs/security.md)

**源码开发预览**：GUI 仍为 **v0.14.1**，后台为 **v0.15 Phase B2.2-A**。B2.2-A 已完成离线工程验收；**B2.2-B 真实 API 联调尚未执行**。不提供已验收安装包，尚未选定项目许可证。

![真实 Qt 桌面中的历史验证与 Diff](docs/images/01-workspace-verified.png)

截图来自 2026-10-16 的真实 GUI，展示历史任务，发布本仓库没有重新调用模型。同一文件的四份轮次 Diff 不代表四个业务文件。[四张截图及来源](docs/screenshots.md)。

## 用途与职责

桌面展示任务、阶段、Diff、历史、用量和可信证据。Executor 执行有范围的开发任务，其他 Coding Agent 审查和验收；Python Coordinator 独立运行固定可信命令、检查改动范围和当前快照。模型说“完成”不能直接创建 `verified`。

ManagerLoop 负责提出计划和决策，GoalLedger 持久化目标和身份，GoalHost 复用原有 TaskHost / Scheduler / Coordinator 派工。Manager 目前只有后台 Python API，**没有对应的 GUI 自主管理入口**。适合研究离线协作、执行证据与 Windows 集成的开发者。

## 实现状态

| 能力 | 状态与限制 |
| --- | --- |
| v0.14.1 任务页、历史、Inspector / Diff、内置 Mock | 已实现；基本离线演练不需要模型或生产签名 |
| Codex 开发、Cursor 审查/验收 | 已有适配器和历史真实验证；新任务仍需独立执行授权 |
| 调度、资源锁、Git Worktree、可信验证 | 已离线验证；不是 OS 沙箱 |
| GoalLedger、GoalHost、ManagerLoop | 已完成相应离线工程范围；Fake 结果不是模型能力证明 |
| API-only Manager | B2.2-A 离线验收完成；真实服务未验收 |
| CLI Manager | v2 独立调用通道；真实 Codex/Cursor Manager profile 仍关闭 |
| 生产成果继承、跨会话自动交接 | 前者阻断，后者规划中 |

## API-only Manager：B2.2-A

公开源码包含 `api_profiles.py`、`api_protocol.py`、`api_http.py`、`api_authorization.py`、`api_transport.py` 五个新模块及必要安全测试。OpenAI 与 DeepSeek 使用各自固定 endpoint / model profile 和字段白名单，复用 Responses 解析与领域校验。请求空 tools / tool_choice=none，拒绝工具输出和未知响应，不自动重发发送后结果未知的请求。

API 调用使用独立 **v3 manager_call 签名**，绑定 backend kind、provider、endpoint、model、protocol、数据范围和 profile 摘要；与 **v2 CLI Manager**、**v1 Executor** 权限区分。生产信任根为空，默认 API 离线禁网。离线演练不读取真实 API 凭据。

新增 **59 项离线工程测试**覆盖 FakeHTTP、脚本化 TLS/socket、临时 Fake Executor 和真实产品适配代码路径。它们不是 59 次真实模型测试，不证明账户可用性、真实模型效果或实际费用。B2.2-B 未执行，真实自动修复仍未直接验收。

## 架构

用户 → React / PySide6 桌面 → 受限 Bridge → 本地 IPC → TaskHost / TaskPolicy → Scheduler → Coordinator → 授权 Executor。Coordinator 独立验证改动和可信命令。

后台 ManagerLoop → GoalLedger / GoalHost → 原宿主派工；API-only v3 与 CLI Manager v2 为独立授权路径。Manager 不授予执行权限，也不能自证验收。[英文 README Mermaid](README.md#architecture) 与 [详细架构](docs/architecture.md)。

## Windows 安装与离线运行

要求 Windows 10/11、Python 3.13、Git、Node.js 22.12+。全新 Windows 机器仍需部署验收。

```powershell
git clone https://github.com/kun520c/dual-agent-studio.git
cd dual-agent-studio
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd ci --prefix design-prototype --registry=https://registry.npmjs.org
npm.cmd run check --prefix design-prototype
npm.cmd run build --prefix design-prototype
npm.cmd test --prefix design-prototype
.\.venv\Scripts\python.exe -B tools/run_offline_checks.py --suite all
.\.venv\Scripts\python.exe -B desktop-validation/run.py --state .test-output/gui-state
```

在 GUI 中按 **项目管理 → 创建安全演练 → 新建任务 → Mock → 预检 → 提交** 操作。固定样本不调用真实模型、不需要生产私钥。只读启动加 `--read-only`，不创建任务宿主。`design-prototype` 仅保留前端依赖声明和锁文件；启动前必须构建 `frontend/dist`。[完整说明](docs/getting-started.md)。

## 路线与限制

已完成相应离线基础：持久账本、原宿主集成、有限 Manager 编排、CLI/API 独立签名和未知结果协调。规划：经单独批准的真实 Manager 联调、生产继承、跨会话交接、真实修复验收及跨机打包。

Worktree / write_paths 不是 OS 沙箱；模型自述不是可信验证；次数/时间预算不是费用硬上限。截图中的历史首次通过、零修复不能证明真实自动修复。当前仍处于积极开发阶段。

## 文档

[安装](docs/getting-started.md) · [架构](docs/architecture.md) · [状态与验证](docs/development-status.md) · [安全](docs/security.md) · [FAQ](docs/faq.md) · [贡献](CONTRIBUTING.md) · [变更记录](CHANGELOG.md) · [发布同步](docs/public-export.md) · [第三方声明](THIRD_PARTY_NOTICES.md)

## 许可

Copyright © 2026 kun520c，保留所有权利。**尚未选定项目许可证**。公开可见不等于授予通用使用、修改或再分发许可；第三方依赖遵循各自许可证。本仓库不宣称已经采用 MIT / Apache 等开源许可证。
