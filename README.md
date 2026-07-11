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

AITermux 直接 clone 本仓库时，也可使用根目录兼容入口：

```bash
./run.sh
```

根目录 `run.sh` 与 `projectling.zsh` 只是兼容桥，仍执行 `app/` 中与 Windows 相同的核心源码，不维护独立 Termux 实现。

## 交互入口

- `/settings`：统一设置中心，包含 Provider、Key、模型、Gemini 参数、WebSearch 和连通测试。
- `/role`：抽卡、锁定、主星/执行星选择和角色停留时间。
- `/exit`：保存必要状态并退出。

首页只保留当前状态和简短提示；模型列表与 API 测试不作为外层菜单重复展示。

## 目录结构

```text
PROJECT凌.exe       Windows 启动入口
run.sh              AITermux/Termux 根目录兼容入口
projectling.zsh      AITermux zsh 兼容入口
app/                Windows 与 Termux 共用核心程序
Windows/            Windows 诊断入口与说明
Termux/             Termux 安装、启动与说明
assets/             应用图标
docs/               发布、模型和参数兼容性文档
```

首次启动后，本机状态会生成在 `app/config/`、`app/context/`、`app/memory/` 和 `app/aidebug/`。升级时先备份这些目录，避免空白文件覆盖已有状态。

## 模型与参数

项目支持 Gemini OpenAI-compatible 中转和 DeepSeek。模型是否真实可用由上游渠道、账号权限和实时容量决定；ProjectLing 会记录实际请求模型、上游响应模型、耗时和用量，不会把 Flash 成功伪装成 Pro 成功，也不会静默替换主星模型。

Gemini 的 `temperature`、`top_p`、`top_k` 等参数只有在目标模型和中转服务真实支持时才会生效。兼容结论见 `docs/MODEL-COMPATIBILITY.md` 与 `docs/GEMINI-PARAMETER-SUPPORT.md`。

## 验证状态

- Windows 原生 selftest：29 项通过，0 项失败；其余为宿主机缺少 bash/zsh 时的预期跳过。
- 公开发行树 Windows AIDEBUG：100 / 100；完整开发状态 AIDEBUG：99.5 / 100，状态 `ok`。
- EXE 已嵌入多尺寸横向 Windows 终端图标。
- 公开发布树经过禁止路径、密钥模式和本机真实密钥字面值三重扫描。

Android 的 `am`、tmux 前台标签页和 `allow-external-apps=true` 仍需在真实 Termux 设备验证；Windows/WSL 结果不能替代 Android 真机证据。

## 安全边界

工具可以在用户授权后处理项目外的绝对路径，能力不被锁死在自身目录内；这也意味着启用命令、文件写入和终端工具前必须确认目标路径与权限。不要把 API Key 写入 issue、截图、日志或公开提交。

发现安全问题时，请不要公开披露密钥或个人数据，按 `SECURITY.md` 处理。
