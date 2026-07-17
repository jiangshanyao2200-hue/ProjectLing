<p align="center">
  <img src="assets/projectling-icon.png" width="160" alt="PROJECT凌 横向终端图标">
</p>

# PROJECT凌

面向 Windows 10/11 与 Android Termux/AITermux 的终端 AI 双星协作系统。Windows 使用原生 EXE 启动器，Termux 使用 shell/zsh 接入；两端共享角色、设置中心、上下文、记忆、模型路由和工具语义。

> 这是可公开分发的净化版。仓库不包含 API Key、本机配置、角色状态、聊天历史、上下文记录、memory、运行日志或 AIDEBUG 状态。

## 快速开始

### Windows

1. 下载仓库或 Release 中的公开版 ZIP，并保持目录结构不变。
2. 安装 Python 3.11 或兼容的 Python 3。
3. 双击根目录 `PROJECT凌.exe`。
4. 首次进入 `/settings`，配置 Provider、API Key 和模型。

诊断命令：

```powershell
python app\core.py doctor
python app\core.py selftest
Windows\aidebug.cmd windows
Windows\aidebug.cmd health --json
```

### Termux / AITermux

将目录放到 Termux 的 `$HOME` 下，不要直接在 Android 共享存储中运行：

```bash
bash Termux/install.sh --check
bash Termux/install.sh
bash Termux/run.sh
```

## 交互入口

- `/settings`：先选择主星或执行星，再分别配置 GPT/Codex、Gemini、Grok 或 DeepSeek 的 Key、地址、模型、场景预设、高级参数、WebSearch 和连通测试；两颗星可使用不同 Provider/模型且互不覆盖。
- `/role`：抽卡、锁定、主星/执行星选择和角色停留时间。
- `/exit`：保存必要状态并退出。

首页只保留当前状态和简短提示；模型列表与 API 测试不作为外层菜单重复展示。

## 目录结构

```text
PROJECT凌.exe       Windows 启动入口
app/                Windows 与 Termux 共用核心程序
Windows/            Windows 诊断入口与说明
Termux/             Termux 安装、启动与说明
assets/             应用图标
docs/               发布、模型和参数兼容性文档
```

首次启动后，本机状态会生成在 `app/config/`、`app/context/`、`app/memory/` 和 `app/aidebug/`。升级时先备份这些目录，避免空白文件覆盖已有状态。

## 模型与参数

项目支持 GPT/Codex、Gemini、Grok 与 DeepSeek 的 OpenAI-compatible 中转。模型是否真实可用由上游渠道、账号权限和实时容量决定；ProjectLing 会记录实际请求模型、上游响应模型、耗时和用量，不会把一个模型的成功伪装成另一个模型成功，也不会静默替换主星或执行星模型。

场景预设用“默认、编程、数据、日常、闲聊、想象力”等选项生成 Provider 支持的参数；GPT 5.6/5.5 推理等级、Gemini 采样参数、Grok 推理参数及多模态内容会按模型能力过滤。兼容结论见 `docs/MODEL-COMPATIBILITY.md` 与 `docs/GEMINI-PARAMETER-SUPPORT.md`。

## 验证状态

- 共享 core selftest：55 / 55，100%，0 fail，0 skip。
- 完整 AIDEBUG full health：67 / 67，100%；真实 Android Termux health：44 / 44，100%。
- Termux local/full verifier：两档各 6 / 6，包含 tmux、MOTD/Zsh、在线双星工具链、WebSearch 和 runtime-state guard。
- Windows `.NET 9` win-x64 构建：0 warning，0 error；EXE 已嵌入多尺寸横向 Windows 终端图标。
- 公开发布树经过禁止路径、密钥模式和本机真实密钥字面值三重扫描；原生 Windows 交互 UI 保留为最终发布人工验收门。

## 安全边界

工具可以在用户授权后处理项目外的绝对路径，能力不被锁死在自身目录内；这也意味着启用命令、文件写入和终端工具前必须确认目标路径与权限。不要把 API Key 写入 issue、截图、日志或公开提交。

发现安全问题时，请不要公开披露密钥或个人数据，按 `SECURITY.md` 处理。
