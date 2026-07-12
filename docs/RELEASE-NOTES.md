# PROJECT凌 Final Release

## 安全修复 · 2026-07-12

- 修复变更型命令被标记为 `confirm` 后仍直接执行的问题。
- 删除、管理员/root 与高风险设备命令现在必须由用户输入完整 `yes`。
- `apply_patch` 禁止模型直接删除或移动整文件，普通创建、修改和跨目录编辑保持可用。

## 发布状态

- 状态：可发布。
- Windows AIDEBUG：100 / 100，24 checks，0 fail，0 warn。
- Windows selftest：29 passed，0 failed，10 expected skips。
- Termux/WSL selftest：36 passed，0 failed，3 expected skips。
- Windows 宽度矩阵：16、20、24、32、40、48、80、120 全部通过。
- Windows 输入编辑：首字符 Backspace、Delete、光标中部删除与边界条件全部通过。
- Termux：Bash/Zsh 语法、安装检查、doctor、单独版入口和合并版 wrapper 全部通过。

## 版本

- Combined：Windows 与 Termux 合并版，推荐作为完整发布包。
- Windows：仅保留 Windows 入口和共享 app。
- Termux：仅保留 Termux 安装/启动入口和共享 app。

三个版本的核心源码都来自同一个 `app/` 白名单，不维护平台分叉副本。

## Windows

- 根目录双击 `PROJECT凌.exe`。
- 启动器会自动定位 `app/core.py`。
- 需要 Windows 10/11 x64 与 Python 3。
- 首次启动使用 `/settings` 配置 Provider、API Key 和模型。

Launcher SHA256：

```text
91E0619C2B118F6BB5DA41B477B0D2AF85E6D3DC39004B50208617E0EDD2678C
```

## Termux

```bash
pkg install python zsh tmux
bash install.sh --check
bash install.sh
bash run.sh
```

安装脚本默认不联网安装依赖。它只检查环境、初始化本地 `app/config/env`、设置执行权限并幂等接入 zsh。

## 模型与参数

- 当前渠道暴露 23 个模型：6 recommended、10 usable_limited、5 diagnostic_only、1 incompatible、1 unavailable。
- `luna` 与精确 `5.6 compact` 未向当前令牌暴露。
- `gemini-2.5-flash-image` 不兼容 ProjectLing 文本主链。
- `gemini-3.1-pro-high` 当前渠道不可用。
- 21 个 Gemini 模型完成 105 次参数请求：97 accepted_unverified、6 model_unavailable、2 request_error、0 rejected、0 model mismatch。
- `accepted_unverified` 仅证明参数已发出、上游接受且响应模型一致，不代表一次采样已经统计证明参数效果。

## 用户状态

公开包不包含：

- `app/config/env`
- 角色、focus、计划和 context budget 状态
- context entries 与 shared context
- memory 数据库
- AIDEBUG 日志、缓存、临时文件和构建目录

首次启动可能创建第一个角色与空 memory 基线。升级时必须保留已有的 `app/config/env`、`app/context/`、`app/memory/` 和需要保留的 AIDEBUG 状态。

## 回滚

1. 关闭新版本。
2. 重新使用旧版本完整目录，不要只替换 EXE。
3. 不要用空白配置覆盖旧 `config/env`、context 或 memory。
4. 保留脱敏诊断报告，不公开 API Key 或原始上游错误正文。

