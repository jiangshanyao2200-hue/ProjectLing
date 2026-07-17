# PROJECT凌 Final Release

## 发布状态

- 状态：本地阶段候选；按交付约束，ProjectLing 尚未 push。
- 共享 core selftest：55 / 55，100%，0 fail，0 skip。
- AIDEBUG full health：67 / 67，100%，0 fail，0 warn。
- 真实 Android Termux health：44 / 44，100%。
- Termux local/full verification：两档各 6 / 6；安装、doctor、selftest、AIDEBUG auto、MOTD/Zsh 和 health 全部通过，runtime-state guard 无越界或语义变化。
- 公开合并版真实副本：34 个 `app/` 白名单文件、51 个初始校验项；包内 selftest 的关键 smoke 无跳过，Termux 安装检查与四 Provider 本地合同通过。
- Windows `.NET 9` clean build/publish：0 warning，0 error；根 EXE 与 clean publish 产物字节一致。
- Windows 静态/AIDEBUG 源码门：100%；16、20、24、32、40、48、80、120 宽度和命令面合同由共享源码检查覆盖。
- 真实 TTY 中断恢复与失败 ping 工具链通过：Zsh 恢复 `stty`/光标，执行星读取失败回执后回报阻塞，最终正文显示并返回提示符。
- 同类首轮工具请求的 prompt usage 从旧证据 104,579 tokens 降至 52,841 tokens；默认活动上下文收束为 128,000 字符，压缩目标为 48,000 字符。
- 外部门：最终公开发布前仍需在原生 Windows 主机执行交互 UI；GPT/Grok 在线验证需先轮换聊天中暴露的 Key，再从 Settings 安全写入。

## 版本

- Combined：Windows 与 Termux 合并版，推荐作为完整发布包。
- Windows：仅保留 Windows 入口和共享 app。
- Termux：仅保留 Termux 安装/启动入口和共享 app。

三个版本的核心源码都来自同一个 `app/` 白名单，不维护平台分叉副本。

## Windows

- 根目录双击 `PROJECT凌.exe`。
- 启动器会自动定位 `app/core.py`。
- 需要 Windows 10/11 x64 与 Python 3。
- 首次启动使用 `/settings` 分别配置主星 API 与执行星 API；支持跨 Provider、跨模型协同和场景预设。

Launcher SHA256：

```text
B1AB92144E6D943961DDF05E95BE7289BA109499DED400EC9BFE07B82F24C445
```

## Termux

```bash
pkg install python zsh tmux
bash install.sh --check
bash install.sh
bash run.sh
```

安装脚本默认不联网安装依赖。它只检查环境、初始化本地 `app/config/env`、设置执行权限并幂等接入 zsh。

完整验证：

```bash
bash app/aidebug/bin/aidebug verify-termux --profile local
bash app/aidebug/bin/aidebug verify-termux --profile full
```

## 模型与参数

- Relay 快照覆盖 22 个模型：7 recommended、9 usable_limited、5 diagnostic_only、1 incompatible、0 unavailable。
- Gemini 完整快照为 20 个模型、100 次参数请求：92 accepted_unverified、8 request_error、0 rejected、0 model mismatch。
- GPT/Codex、Gemini、Grok 与 DeepSeek 均有独立参数过滤和无网络 payload 合同；GPT 5.6 支持 low→ultra，5.5 将 ultra 安全收束为 xhigh。
- 主星与执行星（辅星）分别保存 Provider、Key/地址、模型、场景预设、SSE、超时和重试；切换星位不会覆盖另一颗星。
- 当前 Gemini full smoke 完成 9 次 API 调用和 7 轮双星工具链，累计 prompt 为 40,509 tokens、无成本告警；主星复审、执行星命令归属、WebSearch 3 条结果和多模态 payload 保持均通过。
- 模型可见性随 Relay 与 Key 动态变化，最终 ID 必须以对应星位的 `/models` 返回为准。
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
