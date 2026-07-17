# PROJECT凌 Termux

## 放置位置

建议解压到 `$HOME/PROJECTLing`。Android 共享存储可能限制权限和符号链接，不建议直接在 `/sdcard` 或 `/storage/emulated/0` 中运行。

## 环境

```bash
pkg install python zsh tmux
bash install.sh --check
```

安装脚本不会自动联网安装软件。缺少命令时会给出明确提示。

## 初始化

```bash
bash install.sh
```

初始化会：

- 检查 Python、zsh 与 tmux。
- 检查 `app/release/app-files.txt`、关键离线 smoke 资产及其 Python/Bash 语法。
- 为 `app/run.sh` 和 AIDEBUG 脚本添加执行权限。
- 在缺少 `app/config/env` 时，从 `app/config/example/env` 创建本地配置。
- 向 `~/.zshrc` 写入可刷新路径的幂等 PROJECT凌 接入块；移动目录或重复安装不会保留旧路径、不会生成重复块。
- 从旧平铺结构升级到 `app/` 时，先迁移 Settings、上下文和记忆，再创建缺失的默认配置。

## 启动

```bash
bash run.sh
```

常用检查：

```bash
bash run.sh doctor
bash run.sh selftest
bash app/aidebug/bin/aidebug termux --json
bash app/aidebug/bin/aidebug verify-termux --profile local
bash app/aidebug/bin/aidebug verify-termux --profile full
```

升级时保留 `app/config/env`、`app/context/`、`app/memory/` 和需要保留的 AIDEBUG 状态，然后用新版本程序文件更新其余内容。

Settings 已拆成主星 API 与执行星（辅星）API。进入设置会先提醒选择星位；可通过 `/settings main`、`/settings executor` 分别选择 GPT/Codex、Gemini、Grok 或 DeepSeek，并使用场景预设配置参数；Zsh 还提供 `/gpt`、`/codex`、`/openai`、`/gemini`、`/grok`、`/xai`、`/deepseek` 直达，切换星位不会覆盖另一颗星，`/api-test --slot both` 验证跨 Provider 双星。
