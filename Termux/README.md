# PROJECT凌 Termux

## 放置位置

建议解压到 `$HOME/ProjectLing`。Android 共享存储可能限制权限和符号链接，不建议直接在 `/sdcard` 或 `/storage/emulated/0` 中运行。

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
- 为 `app/run.sh` 和 AIDEBUG 脚本添加执行权限。
- 在缺少 `app/config/env` 时，从 `app/config/example/env` 创建本地配置。
- 向 `~/.zshrc` 添加一次幂等的 PROJECT凌 接入块。

## 启动

```bash
bash run.sh
```

常用检查：

```bash
bash run.sh doctor
bash run.sh selftest
bash app/aidebug/bin/aidebug health --json
```

升级时保留 `app/config/env`、`app/context/`、`app/memory/` 和需要保留的 AIDEBUG 状态，然后用新版本程序文件更新其余内容。
