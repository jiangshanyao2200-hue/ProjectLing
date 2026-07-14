# PROJECT凌 最终发行结构

## 原则

- 发行根目录保持可读：Windows 入口、总说明和平台/文档文件夹。
- 核心程序只保留一份，统一放在 `app/`。
- Windows launcher 和 Termux wrapper 都解析到同一 `app/`。
- 不把用户 API Key、角色、focus、context、memory、日志或缓存打进发行包。
- Windows、Termux 和合并版都从同一白名单生成，避免版本漂移。

## 合并版

```text
PROJECTLing-Combined/
├─ PROJECT凌.exe
├─ run.sh
├─ projectling.zsh
├─ README.md
├─ app/
├─ Windows/
├─ Termux/
└─ docs/
```

## Windows 版

```text
PROJECTLing-Windows/
├─ PROJECT凌.exe
├─ README.md
├─ app/
├─ Windows/
└─ docs/
```

## Termux 版

```text
PROJECTLing-Termux/
├─ README.md
├─ install.sh
├─ run.sh
├─ app/
└─ docs/
```

`app/config/env` 只在用户首次设置或 Termux 初始化时创建，不进入公开发行包。

构建器兼容开发目录中的 `PROJECT LING.exe` 和仓库快照中的 `PROJECT凌.exe`，所有发行包统一输出为 `PROJECT凌.exe`。公开发布目标保持 `PROJECTling`，私有完整状态目标为 `ProjectLing-Private`。
