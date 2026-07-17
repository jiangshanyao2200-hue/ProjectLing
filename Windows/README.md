# PROJECT凌 Windows

## 启动

双击发行根目录的 `PROJECT凌.exe`。启动器会自动定位 `app/core.py`，不需要进入 `app/` 手动运行。

要求：

- Windows 10/11 x64。
- Python 3.11 或兼容 Python 3，可通过 `python`、`python3` 或 `py -3` 找到。
- 也可设置环境变量 `PROJECTLING_PYTHON` 指向 Python 可执行文件。

首次启动后输入 `/settings`，先选择主星或执行星（辅星），再分别配置 Provider、API Key、模型和场景预设；切换星位不会覆盖另一颗星。随后再配置搜索服务。

## 诊断

```powershell
Windows\aidebug.cmd windows
Windows\aidebug.cmd health --json
.\PROJECT凌.exe --aidebug-command-surface --json --widths 16,20,24,32,40,48,80,120
```

不要单独移动 EXE。`PROJECT凌.exe`、`app/` 和 `Windows/` 必须保持相对位置。
