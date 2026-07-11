# PROJECT凌 Final Release Inventory

## 根目录

Combined：

- `PROJECT凌.exe`
- `README.md`
- `app/`
- `Windows/`
- `Termux/`
- `docs/`
- `SHA256SUMS.txt`

Windows：

- `PROJECT凌.exe`
- `README.md`
- `app/`
- `Windows/`
- `docs/`
- `SHA256SUMS.txt`

Termux：

- `README.md`
- `install.sh`
- `run.sh`
- `app/`
- `docs/`
- `SHA256SUMS.txt`

## app 白名单

- 核心：`core.py`、`projectling.py`、`tooling.py`、`__init__.py`
- 入口：`run.sh`、`projectling.zsh`
- 工具定义：`toolbox.json`、`config/toolbox.json`
- 默认配置：`config/roster.json`、`config/persona_links.json`、`config/example/`
- 默认上下文：`context/prompts.json`
- 诊断：`aidebug/aidebug.cmd`、`aidebug/aidebug.ps1`、`aidebug/bin/aidebug`、`aidebug/runner/`
- Launcher 源码：`windows-launcher/Program.cs`、`windows-launcher/ProjectLingLauncher.csproj`

## 明确排除

- 用户 `config/env` 与所有运行状态 JSON
- `context/entries.jsonl`、`context/shared_context.txt`
- `memory/`
- `aidebug/logs`、`notes`、`state`、`tmp`、`backup`
- `__pycache__`、`*.pyc`
- `windows-launcher/bin`、`obj`
- `.git`、开发网页、历史删除项、桌面草稿和未知文件

每个版本都包含逐文件 `SHA256SUMS.txt`。ZIP 的 SHA256 记录在发行根目录 `release-manifest.json`。

