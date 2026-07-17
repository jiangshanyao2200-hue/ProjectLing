using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

var launcher = new ProjectLingWindowsLauncher();
return launcher.Run(args);

sealed class ProjectLingWindowsLauncher
{
    private const string DefaultDistro = "Ubuntu-ProjectLing";
    private const int MaxLayoutWidth = 96;

    private readonly object _lock = new();
    private readonly NativeMethods.ConsoleCtrlDelegate _consoleHandler;
    private Process? _wslProcess;
    private bool _cleanupStarted;
    private string _distro = DefaultDistro;
    private string _linuxProjectPath = "";
    private string _projectRoot = "";
    private CommandSpec? _python;

    public ProjectLingWindowsLauncher()
    {
        _consoleHandler = OnConsoleControl;
    }

    public int Run(string[] args)
    {
        Console.OutputEncoding = Encoding.UTF8;
        Console.InputEncoding = Encoding.UTF8;
        Console.Title = "PROJECT LING";
        EnableVirtualTerminal();

        NativeMethods.SetConsoleCtrlHandler(_consoleHandler, true);
        AppDomain.CurrentDomain.ProcessExit += (_, _) => Cleanup();

        _projectRoot = ResolveProjectRoot();
        _distro = ReadEnv("PROJECTLING_WSL_DISTRO", DefaultDistro);
        _linuxProjectPath = ReadEnv("PROJECTLING_WSL_PROJECT_PATH", DefaultWslProjectPath(_projectRoot));

        if (args.Contains("--aidebug-layout", StringComparer.OrdinalIgnoreCase))
        {
            return RunAidebugLayoutProbe(args);
        }
        if (args.Contains("--aidebug-command-surface", StringComparer.OrdinalIgnoreCase))
        {
            return RunAidebugCommandSurfaceProbe(args);
        }

        _python = FindPython();

        if (_python is null)
        {
            WriteError("没有找到 Windows Python。请安装 Python 3，或设置 PROJECTLING_PYTHON。");
            PauseForExplorer();
            return 1;
        }
        if (!File.Exists(Path.Combine(_projectRoot, "core.py")))
        {
            WriteError($"PROJECT凌 目录不完整：{_projectRoot}");
            PauseForExplorer();
            return 1;
        }

        if (args.Contains("--wsl", StringComparer.OrdinalIgnoreCase))
        {
            return RunWslShell();
        }

        if (TryRunStartupCommand(args, out var startupExitCode))
        {
            return startupExitCode;
        }

        return RunNativeLauncher();
    }

    private bool TryRunStartupCommand(string[] args, out int exitCode)
    {
        exitCode = 0;
        if (TryParseStartupSettings(args, out var settingsTab))
        {
            var normalized = NormalizeSettingsTab(settingsTab);
            exitCode = normalized == "root"
                ? RunCore("shell-settings")
                : RunCore("shell-settings", "--tab", normalized);
            return true;
        }

        var cleaned = args
            .Where(arg => !string.IsNullOrWhiteSpace(arg))
            .Select(arg => arg.Trim())
            .ToArray();
        if (cleaned.Length == 0)
        {
            return false;
        }

        var first = cleaned[0].ToLowerInvariant();
        var passthrough = cleaned.Skip(1).ToArray();
        switch (first)
        {
            case "/models":
            case "/model-list":
            case "/list-models":
                exitCode = RunCore(new[] { "list-models" }.Concat(passthrough).ToArray());
                return true;
            case "/api-test":
            case "/apitest":
                exitCode = RunCore(new[] { "api-test" }.Concat(passthrough).ToArray());
                return true;
            case "/help":
            case "/menu":
                WriteWindowsHelp();
                exitCode = 0;
                return true;
        }

        return false;
    }

    private static bool TryParseStartupSettings(string[] args, out string tab)
    {
        tab = "root";
        var cleaned = args
            .Where(arg => !string.IsNullOrWhiteSpace(arg))
            .Select(arg => arg.Trim())
            .ToArray();
        if (cleaned.Length == 0)
        {
            return false;
        }

        var first = cleaned[0];
        if (
            first.Equals("/settings", StringComparison.OrdinalIgnoreCase)
            || first.Equals("/s", StringComparison.OrdinalIgnoreCase)
            || first.Equals("--settings", StringComparison.OrdinalIgnoreCase)
            || first.Equals("settings", StringComparison.OrdinalIgnoreCase)
        )
        {
            tab = cleaned.Length > 1 ? string.Join(" ", cleaned.Skip(1)) : "root";
            return true;
        }

        foreach (var prefix in new[] { "/settings:", "/settings=", "--settings=", "settings:" })
        {
            if (!first.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            var inlineTab = first[prefix.Length..].Trim();
            tab = inlineTab.Length > 0
                ? inlineTab
                : cleaned.Length > 1
                    ? string.Join(" ", cleaned.Skip(1))
                    : "root";
            return true;
        }

        return false;
    }

    private int RunNativeLauncher()
    {
        RenderAnimatedCard(reroll: false);
        DrawWindowBackdrop();
        RenderCard(reroll: false);
        DrawStatusPanel();
        WriteChatHint();
        RunChatLoop();
        Cleanup();
        return 0;
    }

    private void RunChatLoop()
    {
        while (true)
        {
            DrawInputBox();
            var line = ReadInputLine();
            if (line is null)
            {
                return;
            }
            line = line.Trim();
            if (line.Length == 0)
            {
                continue;
            }
            switch (line.ToLowerInvariant())
            {
                case "/exit":
                case "/quit":
                case "/q":
                case "exit":
                    return;
                case "/help":
                case "/menu":
                    WriteWindowsHelp();
                    DrawStatusPanel();
                    continue;
                case "/settings":
                case "/s":
                    OpenSettings("root");
                    continue;
                case "/main-api":
                case "/main":
                    OpenSettings("main");
                    continue;
                case "/executor-api":
                case "/executor":
                    OpenSettings("executor");
                    continue;
                case "/gpt":
                case "/codex":
                    OpenSettings("gpt");
                    continue;
                case "/gemini":
                    OpenSettings("gemini");
                    continue;
                case "/grok":
                case "/xai":
                    OpenSettings("grok");
                    continue;
                case "/deepseek":
                    OpenSettings("deepseek");
                    continue;
                case "/reroll":
                    RunCore("reroll-role");
                    RenderAnimatedCard(reroll: false);
                    DrawWindowBackdrop();
                    RenderCard(reroll: false);
                    DrawStatusPanel();
                    WriteChatHint();
                    continue;
                case "/role":
                    OpenSettings("persona");
                    continue;
                case "/card":
                    DrawSectionHeader("ROLE CARD", "当前角色状态");
                    RenderCard(reroll: false);
                    DrawStatusPanel();
                    continue;
                case "/roles":
                case "/roster":
                    RunCore("show-roster");
                    continue;
                case "/models":
                case "/model-list":
                case "/list-models":
                    DrawSectionHeader("MODELS", "主星 Provider 模型列表；执行星可用 /models --slot executor");
                    RunCore("list-models", "--slot", "main");
                    DrawStatusPanel();
                    continue;
                case "/api-test":
                case "/apitest":
                    DrawSectionHeader("API TEST", "主星 / 执行星跨 Provider 连通性");
                    RunCore("api-test", "--slot", "both");
                    DrawStatusPanel();
                    continue;
                case "/debug":
                case "/aidebug":
                    DrawSectionHeader("AIDEBUG", "Windows 前端检查");
                    RunAidebugWindows();
                    DrawStatusPanel();
                    continue;
                case "/termux":
                case "/termux-shell":
                case "/wsl":
                    DrawSectionHeader("TERMUX COMPAT", "兼容层调试入口");
                    RunWslShell();
                    RenderCard(reroll: false);
                    DrawStatusPanel();
                    continue;
            }

            if (line.StartsWith("/settings ", StringComparison.OrdinalIgnoreCase))
            {
                OpenSettings(line[10..].Trim());
                continue;
            }
            if (line.StartsWith("/models ", StringComparison.OrdinalIgnoreCase)
                || line.StartsWith("/model-list ", StringComparison.OrdinalIgnoreCase)
                || line.StartsWith("/list-models ", StringComparison.OrdinalIgnoreCase))
            {
                var parts = line.Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
                RunCore(new[] { "list-models" }.Concat(parts.Skip(1)).ToArray());
                DrawStatusPanel();
                continue;
            }
            if (line.StartsWith("/api-test ", StringComparison.OrdinalIgnoreCase)
                || line.StartsWith("/apitest ", StringComparison.OrdinalIgnoreCase))
            {
                var parts = line.Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
                RunCore(new[] { "api-test" }.Concat(parts.Skip(1)).ToArray());
                DrawStatusPanel();
                continue;
            }
            if (line.StartsWith("/mode ", StringComparison.OrdinalIgnoreCase))
            {
                RunCore("mode", line[6..].Trim());
                continue;
            }
            if (line.Equals("/mode", StringComparison.OrdinalIgnoreCase))
            {
                RunCore("mode");
                continue;
            }
            if (line.StartsWith("/model ", StringComparison.OrdinalIgnoreCase))
            {
                RunCore("model", line[7..].Trim());
                continue;
            }
            if (line.Equals("/model", StringComparison.OrdinalIgnoreCase))
            {
                RunCore("model");
                continue;
            }

            DrawMessageDivider("PROJECTLING", "processing");
            RunCore("shell-dispatch", "--mode", "chat", "--cwd", Directory.GetCurrentDirectory(), "--raw", line);
        }
    }

    private string? ReadInputLine()
    {
        if (Console.IsInputRedirected)
        {
            return Console.ReadLine();
        }

        var buffer = new StringBuilder();
        var cursor = 0;
        var originLeft = Console.CursorLeft;
        var originTop = Console.CursorTop;

        void RenderEditor()
        {
            var text = buffer.ToString();
            var availableWidth = Math.Max(1, LayoutWidth() - originLeft);
            var start = 0;
            while (start < cursor && DisplayWidth(text[start..cursor]) > Math.Max(1, availableWidth - 1))
            {
                start++;
            }
            var end = text.Length;
            while (end > cursor && DisplayWidth(text[start..end]) > availableWidth)
            {
                end--;
            }
            var visible = text[start..end];
            while (visible.Length > 0 && DisplayWidth(visible) > availableWidth)
            {
                end--;
                visible = text[start..end];
            }
            var visibleWidth = DisplayWidth(visible);
            var cursorWidth = DisplayWidth(text[start..cursor]);
            try
            {
                Console.SetCursorPosition(originLeft, originTop);
                Console.Write(visible + new string(' ', Math.Max(0, availableWidth - visibleWidth)));
                Console.SetCursorPosition(originLeft + Math.Min(availableWidth - 1, cursorWidth), originTop);
            }
            catch
            {
                // Keep accepting input if the console is resized while editing.
            }
        }

        void FinishEditorLine()
        {
            RenderEditor();
            try
            {
                Console.SetCursorPosition(0, originTop);
                Console.WriteLine();
            }
            catch
            {
                WriteLine("");
            }
        }

        while (true)
        {
            var key = Console.ReadKey(intercept: true);
            switch (key.Key)
            {
                case ConsoleKey.Enter:
                    FinishEditorLine();
                    return buffer.ToString();
                case ConsoleKey.Escape:
                    buffer.Clear();
                    cursor = 0;
                    FinishEditorLine();
                    return "";
                case ConsoleKey.Backspace:
                    if (DeleteBeforeCursor(buffer, ref cursor))
                    {
                        RenderEditor();
                    }
                    continue;
                case ConsoleKey.Delete:
                    if (DeleteAtCursor(buffer, cursor))
                    {
                        RenderEditor();
                    }
                    continue;
                case ConsoleKey.LeftArrow:
                    if (cursor > 0)
                    {
                        cursor--;
                        RenderEditor();
                    }
                    continue;
                case ConsoleKey.RightArrow:
                    if (cursor < buffer.Length)
                    {
                        cursor++;
                        RenderEditor();
                    }
                    continue;
                case ConsoleKey.Home:
                    cursor = 0;
                    RenderEditor();
                    continue;
                case ConsoleKey.End:
                    cursor = buffer.Length;
                    RenderEditor();
                    continue;
            }

            if (buffer.Length == 0 && key.KeyChar == '/')
            {
                return ReadSlashMenuSelection();
            }
            if (char.IsControl(key.KeyChar) || buffer.Length >= 4096)
            {
                continue;
            }
            buffer.Insert(cursor, key.KeyChar);
            cursor++;
            RenderEditor();
        }
    }

    private static bool DeleteBeforeCursor(StringBuilder buffer, ref int cursor)
    {
        if (cursor <= 0 || buffer.Length == 0)
        {
            return false;
        }
        buffer.Remove(cursor - 1, 1);
        cursor--;
        return true;
    }

    private static bool DeleteAtCursor(StringBuilder buffer, int cursor)
    {
        if (cursor < 0 || cursor >= buffer.Length)
        {
            return false;
        }
        buffer.Remove(cursor, 1);
        return true;
    }

    private string ReadSlashMenuSelection()
    {
        var items = BuildSlashMenuItems();
        var selected = 0;
        Console.Write("/");
        WriteLine("");

        var renderedLineCount = 0;

        void Render()
        {
            if (renderedLineCount > 0)
            {
                Console.Write($"\u001b[{renderedLineCount}A\u001b[J");
            }
            var lines = BuildSlashMenuSelectionLines(items, selected, LayoutWidth());
            renderedLineCount = lines.Count;
            SetColor(ConsoleColor.Cyan);
            WriteLine(lines[0]);
            ResetColor();
            foreach (var line in lines.Skip(1))
            {
                SetColor(line.Contains("▶", StringComparison.Ordinal) ? ConsoleColor.White : ConsoleColor.DarkGray);
                WriteLine(line);
                ResetColor();
            }
        }

        Render();
        while (true)
        {
            var key = Console.ReadKey(intercept: true);
            switch (key.Key)
            {
                case ConsoleKey.UpArrow:
                    selected = (selected - 1 + items.Count) % items.Count;
                    Render();
                    continue;
                case ConsoleKey.DownArrow:
                case ConsoleKey.Tab:
                    selected = (selected + 1) % items.Count;
                    Render();
                    continue;
                case ConsoleKey.Home:
                    selected = 0;
                    Render();
                    continue;
                case ConsoleKey.End:
                    selected = items.Count - 1;
                    Render();
                    continue;
                case ConsoleKey.Enter:
                    return FinishSlashMenu(renderedLineCount, items[selected].Command);
                case ConsoleKey.Escape:
                case ConsoleKey.Backspace:
                    return FinishSlashMenu(renderedLineCount, "");
            }

            if (key.KeyChar >= '1' && key.KeyChar <= '9')
            {
                var index = key.KeyChar - '1';
                if (index >= 0 && index < items.Count)
                {
                    return FinishSlashMenu(renderedLineCount, items[index].Command);
                }
            }
        }
    }

    private static string FinishSlashMenu(int renderedLineCount, string command)
    {
        Console.Write($"\u001b[{renderedLineCount + 1}A\u001b[J");
        if (string.IsNullOrWhiteSpace(command))
        {
            WriteLine("› ");
            return "";
        }
        WriteLine($"› {command}");
        return command;
    }

    private void OpenSettings(string tab)
    {
        var normalized = NormalizeSettingsTab(tab);
        if (normalized == "root")
        {
            RunCore("shell-settings");
        }
        else
        {
            RunCore("shell-settings", "--tab", normalized);
        }
        DrawWindowBackdrop();
        RenderCard(reroll: false);
        DrawStatusPanel();
        WriteChatHint();
    }

    private static string NormalizeSettingsTab(string value)
    {
        var tab = (value ?? "").Trim().ToLowerInvariant();
        return tab switch
        {
            "" or "root" or "all" => "root",
            "api" or "key" or "keys" => "api",
            "main" or "main-api" or "main_api" or "planner" => "main",
            "executor" or "executor-api" or "executor_api" or "support" => "executor",
            "gpt" or "codex" or "openai" => "gpt",
            "deepseek" => "deepseek",
            "gemini" => "gemini",
            "grok" or "xai" => "grok",
            "gemini-params" or "gemini_params" or "params" or "advanced" => "gemini_params",
            "websearch" or "web-search" or "web_search" or "search" => "websearch",
            "role" or "roles" or "persona" or "personas" => "persona",
            "system" or "sys" or "settings" => "system",
            _ => "root",
        };
    }

    private void WriteChatHint()
    {
        var width = LayoutWidth();
        WriteLine("");
        WritePanel(
            "TIP",
            BuildInputHintLines(width),
            ConsoleColor.DarkGray,
            compact: true
        );
        WriteLine("");
    }

    private void WriteSlashMenu()
    {
        var width = LayoutWidth();
        WriteLine("");
        WritePanel(
            "菜单",
            BuildStartupSlashMenuLines(width),
            ConsoleColor.Cyan,
            compact: true
        );
    }

    private void WriteWindowsHelp()
    {
        WriteLine("");
        WritePanel(
            "菜单",
            BuildWindowsHelpLines(LayoutWidth()),
            ConsoleColor.Cyan
        );
        WriteLine("");
    }

    private static IReadOnlyList<string> BuildWindowsHelpLines(int width)
    {
        return BuildStartupSlashMenuLines(width);
    }

    private void DrawWindowBackdrop()
    {
        SafeClear();
        var width = LayoutWidth();
        var lines = BuildWindowBackdropLines(width);
        SetColor(ConsoleColor.Cyan);
        WriteLine(lines[0]);
        ResetColor();
        SetColor(ConsoleColor.DarkCyan);
        foreach (var line in lines.Skip(1))
        {
            WriteLine(line);
        }
        ResetColor();
        WriteLine("");
    }

    private void DrawStatusPanel(bool compact = false)
    {
        var role = ReadCurrentRoleInfo();
        var mode = ReadEnvFileValue("PROJECTLING_COLLAB_MODE", "standard");
        var api = ReadApiStatus();
        var lines = compact ? BuildCompactStatusLines(role, mode, api, LayoutWidth()) : BuildStatusLines(role, mode, api, LayoutWidth());
        WritePanel("STATUS", lines, ConsoleColor.DarkMagenta, compact: compact);
    }

    private void DrawInputBox()
    {
        var width = LayoutWidth();
        var lines = BuildInputBoxLines(width, ReadUserLabel());
        WriteLine("");
        SetColor(ConsoleColor.DarkCyan);
        WriteLine(lines[0]);
        SetColor(ConsoleColor.Cyan);
        Console.Write("› ");
        ResetColor();
    }

    private void DrawMessageDivider(string label, string status)
    {
        var width = LayoutWidth();
        WriteLine("");
        SetColor(ConsoleColor.DarkGray);
        WriteLine(BuildMessageDividerLine(width, label, status));
        ResetColor();
    }

    private void DrawSectionHeader(string title, string subtitle)
    {
        WriteLine("");
        WritePanel(title, [subtitle], ConsoleColor.DarkCyan, compact: true);
    }

    private void WritePanel(string title, IReadOnlyList<string> lines, ConsoleColor color, bool compact = false)
    {
        var width = LayoutWidth();
        var rendered = BuildPanelLines(title, lines, width);
        SetColor(color);
        WriteLine(rendered[0]);
        ResetColor();
        foreach (var line in rendered.Skip(1))
        {
            SetColor(compact ? ConsoleColor.DarkGray : ConsoleColor.Gray);
            WriteLine(line);
            ResetColor();
        }
    }

    private void RenderAnimatedCard(bool reroll)
    {
        var width = LayoutWidth();
        var args = new List<string>
        {
            "core.py",
            "animate-motd-card",
            "--width",
            width.ToString(),
            "--frames",
            "5",
            "--final-card",
            "--max-lines",
            "12",
            "--settings-label",
            "",
        };
        if (reroll)
        {
            args.Add("--reroll");
        }

        var output = RunPythonCapture(args);
        if (string.IsNullOrWhiteSpace(output))
        {
            RenderCard(reroll);
            return;
        }

        var frames = output.Split('\f');
        foreach (var frame in frames.Where(frame => !string.IsNullOrWhiteSpace(frame)))
        {
            SafeClear();
            WriteLine(frame.TrimEnd('\r', '\n'));
            Thread.Sleep(90);
        }
    }

    private void RenderCard(bool reroll)
    {
        var width = LayoutWidth();
        var args = new List<string>
        {
            "core.py",
            "render-motd-card",
            "--width",
            width.ToString(),
            "--max-lines",
            "12",
            "--settings-label",
            "",
        };
        if (reroll)
        {
            args.Add("--reroll");
        }
        RunPython(args, inheritConsole: true);
    }

    private void RunAidebugWindows()
    {
        var runner = Path.Combine(_projectRoot, "aidebug", "runner", "aidebug_health.py");
        if (!File.Exists(runner))
        {
            WriteError("缺少 AIDEBUG runner。");
            return;
        }
        RunPython(new[] { runner, "--windows" }, inheritConsole: true);
    }

    private int RunAidebugLayoutProbe(string[] args)
    {
        var widths = ParseLayoutProbeWidths(args);
        var role = ReadCurrentRoleInfo();
        var samples = widths.Select(width => BuildLayoutProbeSample(width, role)).ToList();
        var status = samples.All(sample => sample.Ok) ? "ok" : "fail";
        var payload = new
        {
            status,
            maxLayoutWidth = MaxLayoutWidth,
            generatedAt = DateTimeOffset.UtcNow.ToString("O"),
            samples,
        };
        if (args.Contains("--json", StringComparer.OrdinalIgnoreCase))
        {
            var options = new JsonSerializerOptions
            {
                PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
                WriteIndented = true,
            };
            Console.WriteLine(JsonSerializer.Serialize(payload, options));
            return status == "ok" ? 0 : 2;
        }

        Console.WriteLine($"layout_probe={status}");
        foreach (var sample in samples)
        {
            Console.WriteLine($"width={sample.ConsoleWidth} layout={sample.LayoutWidth} ok={sample.Ok}");
            foreach (var issue in sample.Issues)
            {
                Console.WriteLine($"  issue={issue}");
            }
            foreach (var line in sample.Lines)
            {
                Console.WriteLine($"  {line.Group} {line.DisplayWidth}/{line.ExpectedWidth} {line.Text}");
            }
        }
        return status == "ok" ? 0 : 2;
    }

    private int RunAidebugCommandSurfaceProbe(string[] args)
    {
        var widths = ParseLayoutProbeWidths(args);
        var role = ReadCurrentRoleInfo();
        var mode = ReadEnvFileValue("PROJECTLING_COLLAB_MODE", "standard");
        var activeProvider = $"{ReadApiProvider("main")}+{ReadApiProvider("executor")}";
        var apiStatus = ReadApiStatus();
        var helpLines = BuildWindowsHelpLines(MaxLayoutWidth);
        var commandAliases = new[]
        {
            "/settings",
            "/role",
            "/exit",
        };
        var editorProbe = new StringBuilder("A");
        var editorProbeCursor = editorProbe.Length;
        var inputEditorBackspaceOk = DeleteBeforeCursor(editorProbe, ref editorProbeCursor)
            && editorProbe.Length == 0
            && editorProbeCursor == 0;
        var deleteProbe = new StringBuilder("A");
        var inputEditorDeleteOk = DeleteAtCursor(deleteProbe, 0)
            && deleteProbe.Length == 0;
        var cursorProbe = new StringBuilder("ABC");
        var cursorProbeCursor = 2;
        var inputEditorCursorOk = DeleteBeforeCursor(cursorProbe, ref cursorProbeCursor)
            && cursorProbe.ToString() == "AC"
            && cursorProbeCursor == 1
            && DeleteAtCursor(cursorProbe, cursorProbeCursor)
            && cursorProbe.ToString() == "A"
            && cursorProbeCursor == 1;
        var boundaryProbe = new StringBuilder("A");
        var boundaryProbeCursor = 0;
        var inputEditorBoundaryOk = !DeleteBeforeCursor(boundaryProbe, ref boundaryProbeCursor)
            && !DeleteAtCursor(boundaryProbe, boundaryProbe.Length)
            && boundaryProbe.ToString() == "A"
            && boundaryProbeCursor == 0;
        var slashMenuContractOk = BuildSlashMenuItems()
            .Select(item => item.Command)
            .SequenceEqual(commandAliases);
        var samples = widths.Select(width =>
        {
            var layoutWidth = LayoutWidthForConsoleWidth(width);
            var lines = new List<object>();
            var sampleOk = true;

            void Add(string group, string text)
            {
                var displayWidth = DisplayWidth(text);
                var lineOk = displayWidth <= layoutWidth;
                sampleOk = sampleOk && lineOk;
                lines.Add(new
                {
                    group,
                    text,
                    displayWidth,
                    expectedWidth = layoutWidth,
                    ok = lineOk,
                });
            }

            foreach (var line in BuildPanelLines("STATUS", BuildStatusLines(role, mode, apiStatus, layoutWidth), layoutWidth))
            {
                Add("status", line);
            }
            var menuLines = BuildPanelLines("菜单", BuildStartupSlashMenuLines(layoutWidth), layoutWidth);
            foreach (var line in menuLines)
            {
                Add("menu", line);
            }
            var menuText = string.Join("\n", menuLines);
            var menuPositions = commandAliases.Select(command => menuText.IndexOf(command, StringComparison.Ordinal)).ToArray();
            var menuOrderOk = menuPositions.All(position => position >= 0)
                && menuPositions.SequenceEqual(menuPositions.OrderBy(position => position))
                && commandAliases.All(command => menuText.Split(command, StringSplitOptions.None).Length == 2);
            sampleOk = sampleOk && menuOrderOk;

            return new
            {
                consoleWidth = width,
                layoutWidth,
                ok = sampleOk,
                menuOrderOk,
                lines,
            };
        }).ToList();
        var responsiveOrderOk = samples.All(sample => sample.menuOrderOk);
        var status = samples.All(sample => sample.ok)
            && inputEditorBackspaceOk
            && inputEditorDeleteOk
            && inputEditorCursorOk
            && inputEditorBoundaryOk
            && slashMenuContractOk
            && responsiveOrderOk
            ? "ok"
            : "fail";
        var payload = new
        {
            status,
            activeProvider,
            apiStatus,
            helpLines,
            commandAliases,
            inputEditorBackspaceOk,
            inputEditorDeleteOk,
            inputEditorCursorOk,
            inputEditorBoundaryOk,
            slashMenuContractOk,
            responsiveOrderOk,
            samples,
        };
        if (args.Contains("--json", StringComparer.OrdinalIgnoreCase))
        {
            var options = new JsonSerializerOptions
            {
                PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
                WriteIndented = true,
            };
            Console.WriteLine(JsonSerializer.Serialize(payload, options));
            return status == "ok" ? 0 : 2;
        }

        Console.WriteLine($"command_surface={status}");
        Console.WriteLine($"provider={activeProvider}");
        Console.WriteLine($"api_status={apiStatus}");
        foreach (var line in helpLines)
        {
            Console.WriteLine($"help={line}");
        }
        return status == "ok" ? 0 : 2;
    }

    private int RunWslShell()
    {
        var wslExe = FindWslExe();
        if (wslExe is null)
        {
            WriteError("没有找到 wsl.exe。");
            return 1;
        }
        if (!CheckWslDistro(wslExe, _distro, _linuxProjectPath))
        {
            WriteError($"WSL 发行版不可用：{_distro}");
            return 1;
        }

        var shellScript = BuildWslShellScript(_linuxProjectPath);
        var psi = new ProcessStartInfo
        {
            FileName = wslExe,
            UseShellExecute = false,
        };
        psi.ArgumentList.Add("-d");
        psi.ArgumentList.Add(_distro);
        psi.ArgumentList.Add("--");
        psi.ArgumentList.Add("bash");
        psi.ArgumentList.Add("-lc");
        psi.ArgumentList.Add(shellScript);

        try
        {
            _wslProcess = Process.Start(psi);
            _wslProcess?.WaitForExit();
            return _wslProcess?.ExitCode ?? 1;
        }
        catch (Exception ex)
        {
            WriteError($"启动 WSL 失败：{ex.Message}");
            return 1;
        }
        finally
        {
            _wslProcess = null;
        }
    }

    private static string BuildWslShellScript(string projectPath)
    {
        var encodedProject = Convert.ToBase64String(Encoding.UTF8.GetBytes(projectPath));
        return $$"""
        set -e
        export LANG=C.UTF-8
        export LC_ALL=C.UTF-8
        export AITERMUX_HOME=/tmp/projectling-windows
        mkdir -p "$AITERMUX_HOME"
        python3 -c 'import base64, os, pathlib, shutil, sys; target=pathlib.Path(base64.b64decode(sys.argv[1]).decode()); link=pathlib.Path(sys.argv[2]); link.parent.mkdir(parents=True, exist_ok=True); sys.exit(2) if not target.is_dir() else None; (shutil.rmtree(link) if link.exists() and not link.is_symlink() and link.is_dir() else link.unlink() if link.exists() or link.is_symlink() else None); os.symlink(target, link, target_is_directory=True)' {{encodedProject}} "$AITERMUX_HOME/projectling"
        export PROJECTLING_HOME="$AITERMUX_HOME/projectling"
        export PROJECTLING_RUNNER="$PROJECTLING_HOME/run.sh"
        mkdir -p /data/data/com.termux/files/usr/bin
        if [ ! -e /data/data/com.termux/files/usr/bin/bash ]; then
          ln -s /usr/bin/bash /data/data/com.termux/files/usr/bin/bash 2>/dev/null || true
        fi
        cd "$PROJECTLING_HOME"
        chmod +x run.sh aidebug/bin/aidebug 2>/dev/null || true
        ./run.sh doctor >/dev/null 2>&1 || true
        ./aidebug/bin/aidebug windows --repair >/dev/null 2>&1 || true
        zdotdir="$(mktemp -d /tmp/projectling-zshrc.XXXXXX)"
        cat >"$zdotdir/.zshrc" <<'PROJECTLING_ZSHRC'
        export AITERMUX_HOME=/tmp/projectling-windows
        export PROJECTLING_HOME="$AITERMUX_HOME/projectling"
        export PROJECTLING_RUNNER="$PROJECTLING_HOME/run.sh"
        cd "$PROJECTLING_HOME"
        source "$PROJECTLING_HOME/projectling.zsh"
        print ""
        print "PROJECT LING WSL 兼容层已启动。输入 /help 查看命令，输入 exit 关闭。"
        PROJECTLING_ZSHRC
        export ZDOTDIR="$zdotdir"
        exec zsh -i
        """;
    }

    private int RunCore(params string[] args)
    {
        var fullArgs = new List<string> { "core.py" };
        fullArgs.AddRange(args);
        return RunPython(fullArgs, inheritConsole: true);
    }

    private int RunPython(IEnumerable<string> args, bool inheritConsole)
    {
        if (_python is null)
        {
            return 1;
        }
        var psi = PythonStartInfo(_python, args);
        if (!inheritConsole)
        {
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = Encoding.UTF8;
            psi.StandardErrorEncoding = Encoding.UTF8;
        }
        using var process = Process.Start(psi);
        if (process is null)
        {
            return 1;
        }
        process.WaitForExit();
        return process.ExitCode;
    }

    private string PromptRoleName()
    {
        var role = ReadCurrentRole();
        if (string.IsNullOrWhiteSpace(role))
        {
            return "PROJECT凌";
        }
        return role;
    }

    private string ReadCurrentRole()
    {
        var rolePath = Path.Combine(_projectRoot, "config", "role.json");
        if (!File.Exists(rolePath))
        {
            return "";
        }
        try
        {
            using var stream = File.OpenRead(rolePath);
            using var doc = JsonDocument.Parse(stream);
            var root = doc.RootElement;
            var zh = root.TryGetProperty("name_zh", out var zhValue) ? zhValue.GetString() : "";
            var en = root.TryGetProperty("name_en", out var enValue) ? enValue.GetString() : "";
            if (!string.IsNullOrWhiteSpace(zh) && !string.IsNullOrWhiteSpace(en))
            {
                return $"{zh} / {en}";
            }
            return !string.IsNullOrWhiteSpace(zh) ? zh! : en ?? "";
        }
        catch
        {
            return "";
        }
    }

    private string RunPythonCapture(IEnumerable<string> args)
    {
        if (_python is null)
        {
            return "";
        }
        var psi = PythonStartInfo(_python, args);
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = Encoding.UTF8;
        psi.StandardErrorEncoding = Encoding.UTF8;
        using var process = Process.Start(psi);
        if (process is null)
        {
            return "";
        }
        var output = process.StandardOutput.ReadToEnd();
        var error = process.StandardError.ReadToEnd();
        process.WaitForExit();
        if (process.ExitCode != 0 && !string.IsNullOrWhiteSpace(error))
        {
            WriteError(error.Trim());
        }
        return output;
    }

    private ProcessStartInfo PythonStartInfo(CommandSpec python, IEnumerable<string> args)
    {
        var psi = new ProcessStartInfo
        {
            FileName = python.FileName,
            UseShellExecute = false,
            WorkingDirectory = _projectRoot,
        };
        foreach (var prefix in python.PrefixArgs)
        {
            psi.ArgumentList.Add(prefix);
        }
        foreach (var arg in args)
        {
            psi.ArgumentList.Add(arg);
        }
        psi.Environment["PYTHONUTF8"] = "1";
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        psi.Environment["PROJECTLING_DIR"] = _projectRoot;
        psi.Environment["AITERMUX_AIDEBUG_DIR"] = Path.Combine(_projectRoot, "aidebug");
        psi.Environment["PROJECTLING_WINDOWS_UI"] = "1";
        return psi;
    }

    private CommandSpec? FindPython()
    {
        var configured = Environment.GetEnvironmentVariable("PROJECTLING_PYTHON");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            var spec = new CommandSpec(configured, []);
            if (CommandWorks(spec, "--version"))
            {
                return spec;
            }
        }

        var candidates = new[]
        {
            new CommandSpec("py", ["-3"]),
            new CommandSpec("python", []),
            new CommandSpec("python3", []),
        };
        return candidates.FirstOrDefault(candidate => CommandWorks(candidate, "--version"));
    }

    private static bool CommandWorks(CommandSpec spec, string arg)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = spec.FileName,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            foreach (var prefix in spec.PrefixArgs)
            {
                psi.ArgumentList.Add(prefix);
            }
            psi.ArgumentList.Add(arg);
            using var process = Process.Start(psi);
            if (process is null)
            {
                return false;
            }
            process.WaitForExit(10_000);
            return process.HasExited && process.ExitCode == 0;
        }
        catch
        {
            return false;
        }
    }

    private static bool CheckWslDistro(string wslExe, string distro, string projectPath)
    {
        var psi = new ProcessStartInfo
        {
            FileName = wslExe,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.ArgumentList.Add("-d");
        psi.ArgumentList.Add(distro);
        psi.ArgumentList.Add("--");
        psi.ArgumentList.Add("bash");
        psi.ArgumentList.Add("-lc");
        psi.ArgumentList.Add($"test -d {ShellQuote(projectPath)} && command -v zsh >/dev/null && command -v python3 >/dev/null");

        using var process = Process.Start(psi);
        if (process is null)
        {
            return false;
        }
        process.WaitForExit(30_000);
        return process.HasExited && process.ExitCode == 0;
    }

    private static string ResolveProjectRoot()
    {
        var configured = Environment.GetEnvironmentVariable("PROJECTLING_WINDOWS_PROJECT_PATH");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            return Path.GetFullPath(configured);
        }
        var candidates = new List<string>();
        AddProjectRootCandidates(candidates, AppContext.BaseDirectory);
        AddProjectRootCandidates(candidates, Directory.GetCurrentDirectory());
        var processPath = Environment.ProcessPath;
        if (!string.IsNullOrWhiteSpace(processPath))
        {
            AddProjectRootCandidates(candidates, Path.GetDirectoryName(processPath) ?? "");
        }

        foreach (var candidate in candidates.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            if (File.Exists(Path.Combine(candidate, "core.py")))
            {
                return candidate;
            }
            var appCandidate = Path.Combine(candidate, "app");
            if (File.Exists(Path.Combine(appCandidate, "core.py")))
            {
                return appCandidate;
            }
        }
        return AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
    }

    private static void AddProjectRootCandidates(List<string> candidates, string start)
    {
        if (string.IsNullOrWhiteSpace(start))
        {
            return;
        }
        var directory = new DirectoryInfo(Path.GetFullPath(start));
        while (directory is not null)
        {
            candidates.Add(directory.FullName.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
            directory = directory.Parent;
        }
    }

    private static string DefaultWslProjectPath(string projectRoot)
    {
        if (!RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
        {
            return projectRoot.Replace('\\', '/');
        }
        var fullPath = Path.GetFullPath(projectRoot);
        var root = Path.GetPathRoot(fullPath) ?? "";
        if (root.Length < 2 || root[1] != ':')
        {
            return fullPath.Replace('\\', '/');
        }
        var drive = char.ToLowerInvariant(root[0]);
        var relative = fullPath[root.Length..].TrimStart(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar).Replace('\\', '/');
        return $"/mnt/{drive}/{relative}";
    }

    private bool OnConsoleControl(NativeMethods.CtrlType signal)
    {
        Cleanup();
        return false;
    }

    private void Cleanup()
    {
        lock (_lock)
        {
            if (_cleanupStarted)
            {
                return;
            }
            _cleanupStarted = true;
        }

        try
        {
            if (_wslProcess is { HasExited: false })
            {
                TryStartAndWait(FindWslExe(), "--terminate", _distro);
            }
        }
        catch
        {
            // ProcessExit and console-close handlers must not throw.
        }
    }

    private static string? FindWslExe()
    {
        var windir = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        var candidates = new[]
        {
            Path.Combine(windir, "System32", "wsl.exe"),
            Path.Combine(windir, "Sysnative", "wsl.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Microsoft", "WindowsApps", "wsl.exe"),
        };
        return candidates.FirstOrDefault(File.Exists);
    }

    private static void TryStartAndWait(string? fileName, params string[] args)
    {
        if (string.IsNullOrWhiteSpace(fileName))
        {
            return;
        }
        var psi = new ProcessStartInfo
        {
            FileName = fileName,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var arg in args)
        {
            psi.ArgumentList.Add(arg);
        }
        using var process = Process.Start(psi);
        process?.WaitForExit(10_000);
    }

    private static string ShellQuote(string value)
    {
        return "'" + value.Replace("'", "'\"'\"'") + "'";
    }

    private static string ReadEnv(string name, string fallback)
    {
        var value = Environment.GetEnvironmentVariable(name);
        return string.IsNullOrWhiteSpace(value) ? fallback : value;
    }

    private static int SafeConsoleWidth()
    {
        try
        {
            return Math.Max(1, Console.WindowWidth);
        }
        catch
        {
            return 96;
        }
    }

    private static int UsableConsoleWidth()
    {
        var width = SafeConsoleWidth();
        return width > 1 ? width - 1 : width;
    }

    private static int LayoutWidth()
    {
        return LayoutWidthForConsoleWidth(SafeConsoleWidth());
    }

    private static int LayoutWidthForConsoleWidth(int consoleWidth)
    {
        var usable = consoleWidth > 1 ? consoleWidth - 1 : consoleWidth;
        return Math.Max(1, Math.Min(MaxLayoutWidth, usable));
    }

    private RoleInfo ReadCurrentRoleInfo()
    {
        var rolePath = Path.Combine(_projectRoot, "config", "role.json");
        if (!File.Exists(rolePath))
        {
            return new RoleInfo("PROJECT凌", "未设置");
        }
        try
        {
            using var stream = File.OpenRead(rolePath);
            using var doc = JsonDocument.Parse(stream);
            var root = doc.RootElement;
            var zh = root.TryGetProperty("name_zh", out var zhValue) ? zhValue.GetString() : "";
            var en = root.TryGetProperty("name_en", out var enValue) ? enValue.GetString() : "";
            var expiresAt = root.TryGetProperty("expires_at", out var expiresValue) && expiresValue.TryGetInt64(out var expires)
                ? expires
                : 0L;
            var locked = root.TryGetProperty("locked", out var lockedValue)
                && lockedValue.ValueKind is JsonValueKind.True;
            var display = !string.IsNullOrWhiteSpace(zh) && !string.IsNullOrWhiteSpace(en)
                ? $"{zh} / {en}"
                : !string.IsNullOrWhiteSpace(zh)
                    ? zh!
                    : en ?? "PROJECT凌";
            return new RoleInfo(display, FormatRemaining(expiresAt, locked));
        }
        catch
        {
            return new RoleInfo("PROJECT凌", "未设置");
        }
    }

    private string ReadEnvFileValue(string key, string fallback)
    {
        var envPath = Path.Combine(_projectRoot, "config", "env");
        if (!File.Exists(envPath))
        {
            return fallback;
        }
        try
        {
            foreach (var raw in File.ReadLines(envPath, Encoding.UTF8))
            {
                var line = raw.Trim();
                if (line.Length == 0 || line.StartsWith("#", StringComparison.Ordinal))
                {
                    continue;
                }
                var separator = line.IndexOf('=');
                if (separator <= 0)
                {
                    continue;
                }
                var name = line[..separator].Trim();
                if (!name.Equals(key, StringComparison.Ordinal))
                {
                    continue;
                }
                return line[(separator + 1)..].Trim().Trim('"', '\'');
            }
        }
        catch
        {
            return fallback;
        }
        return fallback;
    }

    private static string FormatRemaining(long expiresAt, bool locked = false)
    {
        if (locked)
        {
            return "已锁定";
        }
        if (expiresAt <= 0)
        {
            return "未设置";
        }
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        var seconds = Math.Max(0, expiresAt - now);
        var totalMinutes = Math.Max(1, (seconds + 59) / 60);
        var hours = totalMinutes / 60;
        var minutes = totalMinutes % 60;
        return hours > 0 ? $"{hours} 小时 {minutes:D2} 分" : $"{minutes} 分钟";
    }

    private static IReadOnlyList<string> WrapDisplay(string text, int width)
    {
        width = Math.Max(1, width);
        var result = new List<string>();
        var current = "";
        foreach (var rune in text.EnumerateRunes())
        {
            var piece = rune.ToString();
            var next = current + piece;
            if (DisplayWidth(next) > width && current.Length > 0)
            {
                result.Add(current);
                current = piece;
            }
            else
            {
                current = next;
            }
        }
        result.Add(current);
        return result;
    }

    private static IReadOnlyList<int> ParseLayoutProbeWidths(string[] args)
    {
        var raw = "";
        for (var index = 0; index < args.Length - 1; index++)
        {
            if (args[index].Equals("--widths", StringComparison.OrdinalIgnoreCase))
            {
                raw = args[index + 1];
                break;
            }
        }
        if (string.IsNullOrWhiteSpace(raw))
        {
            return [16, 20, 24, 32, 40, 48, 80, 120];
        }

        var widths = raw.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(value => int.TryParse(value, out var width) ? Math.Clamp(width, 1, 240) : 0)
            .Where(width => width > 0)
            .Distinct()
            .Order()
            .ToList();
        return widths.Count == 0 ? [16, 20, 24, 32, 40, 48, 80, 120] : widths;
    }

    private LayoutProbeSample BuildLayoutProbeSample(int consoleWidth, RoleInfo role)
    {
        var layoutWidth = LayoutWidthForConsoleWidth(consoleWidth);
        var lines = new List<LayoutProbeLine>();

        void Add(string group, string text, bool exact)
        {
            var displayWidth = DisplayWidth(text);
            var ok = displayWidth <= layoutWidth && (!exact || displayWidth == layoutWidth);
            lines.Add(new LayoutProbeLine(group, text, displayWidth, layoutWidth, exact, ok));
        }

        foreach (var line in BuildWindowBackdropLines(layoutWidth))
        {
            Add("backdrop", line, exact: line.Length > 0);
        }

        var mode = ReadEnvFileValue("PROJECTLING_COLLAB_MODE", "standard");
        var api = ReadApiStatus();
        var sessionLines = BuildStatusLines(role, mode, api, layoutWidth);
        foreach (var line in BuildPanelLines("STATUS", sessionLines, layoutWidth))
        {
            Add("session", line, exact: layoutWidth >= 8);
        }

        foreach (var line in BuildPanelLines(
                     "TIP",
                     BuildInputHintLines(layoutWidth),
                     layoutWidth))
        {
            Add("tip", line, exact: layoutWidth >= 8);
        }

        foreach (var line in BuildInputBoxLines(layoutWidth, ReadUserLabel()))
        {
            Add("input", line, exact: false);
        }

        Add("divider", BuildMessageDividerLine(layoutWidth, "PROJECTLING", "processing"), exact: true);

        var issues = lines
            .Where(line => !line.Ok)
            .Select(line =>
                $"{line.Group}: display_width={line.DisplayWidth} expected={line.ExpectedWidth} exact={line.Exact} text={line.Text}")
            .ToList();
        var fullBoxLineCount = lines.Count(line => HasFullBoxGlyph(line.Text));
        if (fullBoxLineCount > 0)
        {
            issues.Add($"full_box_glyph_lines={fullBoxLineCount}");
        }
        return new LayoutProbeSample(consoleWidth, layoutWidth, issues.Count == 0, fullBoxLineCount, issues, lines);
    }

    private static bool HasFullBoxGlyph(string text)
    {
        return text.IndexOfAny(['╭', '╮', '╰', '╯', '╔', '╗', '╚', '╝', '┌', '┐', '└', '┘', '┬', '┴', '┼']) >= 0;
    }

    private static IReadOnlyList<string> BuildWindowBackdropLines(int width)
    {
        var title = PadDisplay("▌ PROJECT LING // PC", width);
        return
        [
            title,
            PadDisplay("┃ windows · wsl · /settings", width),
        ];
    }

    private static IReadOnlyList<string> BuildPanelLines(string title, IReadOnlyList<string> lines, int width)
    {
        var result = new List<string>();
        var header = $"▌ {title} //";
        result.Add(PadDisplay(header, width));
        var contentWidth = Math.Max(1, width - 2);
        foreach (var raw in lines)
        {
            var wrapped = WrapDisplay(raw, contentWidth);
            foreach (var line in wrapped)
            {
                result.Add(PadDisplay($"┃ {line}", width));
            }
        }
        return result;
    }

    private static IReadOnlyList<string> BuildInputBoxLines(int width, string userLabel)
    {
        var label = string.IsNullOrWhiteSpace(userLabel) ? "YOU" : userLabel.Trim();
        return
        [
            PadDisplay($"▌ USER // {label}", width),
            "› ",
        ];
    }

    private static IReadOnlyList<string> BuildStatusLines(RoleInfo role, string mode, string api, int width)
    {
        var roleText = CompactRoleDisplay(role.Display);
        var modeText = CollaborationModeLabel(mode);
        if (width < 20)
        {
            return [$"主星 {roleText}", $"协同 {modeText}", api, $"角色时间 {role.RemainingText}"];
        }
        if (width < 32)
        {
            return [$"主星：{roleText}", $"协同模式：{modeText}", $"双星 API：{api}", $"角色剩余时间：{role.RemainingText}"];
        }
        var contentWidth = Math.Max(1, width - 2);
        var combined = $"协同模式：{modeText} · {api}";
        if (DisplayWidth(combined) <= contentWidth)
        {
            return [$"主星：{roleText}", combined, $"角色剩余时间：{role.RemainingText}"];
        }
        return [$"主星：{roleText}", $"协同模式：{modeText}", $"双星 API：{api}", $"角色剩余时间：{role.RemainingText}"];
    }

    private static IReadOnlyList<string> BuildCompactStatusLines(RoleInfo role, string mode, string api, int width)
    {
        var roleText = CompactRoleDisplay(role.Display);
        var modeText = CollaborationModeLabel(mode);
        if (width < 32)
        {
            return [$"主星：{roleText}", $"协同模式：{modeText}", $"双星 API：{api}", $"角色剩余时间：{role.RemainingText}"];
        }
        var contentWidth = Math.Max(1, width - 2);
        var combined = $"协同模式：{modeText} · {api}";
        if (DisplayWidth(combined) <= contentWidth)
        {
            return [$"主星：{roleText}", combined, $"角色剩余时间：{role.RemainingText}"];
        }
        return [$"主星：{roleText}", $"协同模式：{modeText}", $"双星 API：{api}", $"角色剩余时间：{role.RemainingText}"];
    }

    private static string CollaborationModeLabel(string mode)
    {
        return (mode ?? "").Trim().ToLowerInvariant() switch
        {
            "rapid" => "快速",
            "precise" => "精确",
            _ => "标准",
        };
    }

    private static IReadOnlyList<string> BuildInputHintLines(int width)
    {
        return ["输入 / 查看菜单"];
    }

    private static IReadOnlyList<SlashMenuItem> BuildSlashMenuItems()
    {
        return
        [
            new SlashMenuItem("/settings", "设置", "主星 API / 执行星 API / 搜索 / 系统"),
            new SlashMenuItem("/role", "角色", "抽卡 / 锁定 / 主星 / 执行星"),
            new SlashMenuItem("/exit", "退出", "关闭窗口"),
        ];
    }

    private static IReadOnlyList<string> BuildSlashMenuSelectionLines(IReadOnlyList<SlashMenuItem> items, int selected, int width)
    {
        var rows = new List<string>
        {
            "↑↓ 选择 · Enter 进入 · Esc 取消",
        };
        var showDetail = width >= 54;
        var indexWidth = Math.Max(1, items.Count.ToString().Length);
        for (var index = 0; index < items.Count; index++)
        {
            var item = items[index];
            var marker = index == selected ? "▶" : " ";
            var number = (index + 1).ToString().PadLeft(indexWidth);
            var row = showDetail
                ? $"{marker} {number}. {item.Command,-20} {item.Label} · {item.Detail}"
                : $"{marker} {number}. {item.Command} {item.Label}";
            rows.Add(row);
        }
        return BuildPanelLines("菜单", rows, width);
    }

    private static IReadOnlyList<string> BuildStartupSlashMenuLines(int width)
    {
        if (width < 20)
        {
            return ["/settings", "/role", "/exit"];
        }
        if (width < 24)
        {
            return ["设置 /settings", "角色 /role", "退出 /exit"];
        }
        return
        [
            "/settings  设置",
            "/role      角色",
            "/exit      退出",
        ];
    }

    private static string NormalizeApiProvider(string provider)
    {
        return (provider ?? "").Trim().ToLowerInvariant() switch
        {
            "gpt" or "openai" or "codex" => "gpt",
            "gemini" => "gemini",
            "grok" or "xai" => "grok",
            _ => "deepseek",
        };
    }

    private string ReadApiProvider(string slot = "main")
    {
        var legacy = ReadEnvFileValue("PROJECTLING_API_PROVIDER", "deepseek");
        var key = slot.Equals("executor", StringComparison.OrdinalIgnoreCase)
            ? "PROJECTLING_EXECUTOR_PROVIDER"
            : "PROJECTLING_MAIN_PROVIDER";
        var configured = ReadEnvFileValue(key, legacy);
        return NormalizeApiProvider(string.IsNullOrWhiteSpace(configured) ? legacy : configured);
    }

    private static string ApiProviderLabel(string provider)
    {
        return NormalizeApiProvider(provider) switch
        {
            "gpt" => "GPT/Codex",
            "gemini" => "Gemini",
            "grok" => "Grok",
            _ => "DeepSeek",
        };
    }

    private string ReadApiStatus(string? provider = null)
    {
        if (!string.IsNullOrWhiteSpace(provider) && !provider.Contains('+'))
        {
            return ApiProviderLabel(provider);
        }
        var main = ApiProviderLabel(ReadApiProvider("main"));
        var executor = ApiProviderLabel(ReadApiProvider("executor"));
        return main == executor ? main : $"主 {main} · 执 {executor}";
    }

    private static string BuildMessageDividerLine(int width, string label, string status)
    {
        var tail = " ╌╌";
        var text = TrimDisplay($"{label} · {status}", Math.Max(0, width - DisplayWidth(tail) - 2));
        var line = $"╌ {text}{tail}";
        if (DisplayWidth(line) > width)
        {
            line = TrimDisplay($"╌ {text}", width);
        }
        return PadDisplay(line, width);
    }

    private static string ReadUserLabel()
    {
        var configured = Environment.GetEnvironmentVariable("PROJECTLING_USER_LABEL");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            return configured.Trim();
        }
        var username = Environment.GetEnvironmentVariable("USERNAME");
        if (!string.IsNullOrWhiteSpace(username))
        {
            return username.Trim();
        }
        var user = Environment.GetEnvironmentVariable("USER");
        return string.IsNullOrWhiteSpace(user) ? "YOU" : user.Trim();
    }

    private static string CompactRoleDisplay(string display)
    {
        var text = string.IsNullOrWhiteSpace(display) ? "role" : display.Trim();
        var slashIndex = text.IndexOf(" / ", StringComparison.Ordinal);
        if (slashIndex > 0)
        {
            return text[..slashIndex].Trim();
        }
        return text;
    }

    private static string FitBoxLine(string text, int width)
    {
        if (width < 4)
        {
            return TrimDisplay(text, width);
        }
        var inner = Math.Max(1, width - 4);
        return $"║ {PadDisplay(TrimDisplay(text, inner), inner)} ║";
    }

    private static string TrimDisplay(string text, int width)
    {
        if (width <= 0)
        {
            return "";
        }
        if (DisplayWidth(text) <= width)
        {
            return text;
        }
        var builder = new StringBuilder();
        var used = 0;
        foreach (var rune in text.EnumerateRunes())
        {
            var runeWidth = RuneWidth(rune);
            if (used + runeWidth > Math.Max(0, width - 1))
            {
                break;
            }
            builder.Append(rune);
            used += runeWidth;
        }
        builder.Append('…');
        return builder.ToString();
    }

    private static string PadDisplay(string text, int width)
    {
        width = Math.Max(0, width);
        var clipped = TrimDisplay(text, width);
        var padding = Math.Max(0, width - DisplayWidth(clipped));
        return clipped + new string(' ', padding);
    }

    private static int DisplayWidth(string text)
    {
        var width = 0;
        foreach (var rune in text.EnumerateRunes())
        {
            width += RuneWidth(rune);
        }
        return width;
    }

    private static int RuneWidth(Rune rune)
    {
        var value = rune.Value;
        if (value == 0)
        {
            return 0;
        }
        if (value < 32 || (value >= 0x7f && value < 0xa0))
        {
            return 0;
        }
        return value >= 0x2e80 ? 2 : 1;
    }

    private static void SetColor(ConsoleColor color)
    {
        Console.ForegroundColor = color;
    }

    private static void ResetColor()
    {
        Console.ResetColor();
    }

    private static void SafeClear()
    {
        try
        {
            Console.Clear();
        }
        catch
        {
            WriteLine("");
        }
    }

    private static void PauseForExplorer()
    {
        if (Console.IsInputRedirected)
        {
            return;
        }
        WriteLine("");
        WriteLine("按 Enter 关闭窗口。");
        Console.ReadLine();
    }

    private static void EnableVirtualTerminal()
    {
        var handle = NativeMethods.GetStdHandle(NativeMethods.StdOutputHandle);
        if (handle == IntPtr.Zero || handle == new IntPtr(-1))
        {
            return;
        }
        if (!NativeMethods.GetConsoleMode(handle, out var mode))
        {
            return;
        }
        NativeMethods.SetConsoleMode(handle, mode | NativeMethods.EnableVirtualTerminalProcessing);
    }

    private static void WriteLine(string message) => Console.WriteLine(message);

    private static void WriteAccent(string message)
    {
        var previous = Console.ForegroundColor;
        Console.ForegroundColor = ConsoleColor.Cyan;
        Console.WriteLine(message);
        Console.ForegroundColor = previous;
    }

    private static void WriteDim(string message)
    {
        var previous = Console.ForegroundColor;
        Console.ForegroundColor = ConsoleColor.DarkGray;
        Console.WriteLine(message);
        Console.ForegroundColor = previous;
    }

    private static void WriteError(string message)
    {
        var previous = Console.ForegroundColor;
        Console.ForegroundColor = ConsoleColor.Red;
        Console.Error.WriteLine(message);
        Console.ForegroundColor = previous;
    }
}

sealed record CommandSpec(string FileName, string[] PrefixArgs);
sealed record RoleInfo(string Display, string RemainingText);
sealed record SlashMenuItem(string Command, string Label, string Detail);
sealed record LayoutProbeLine(string Group, string Text, int DisplayWidth, int ExpectedWidth, bool Exact, bool Ok);
sealed record LayoutProbeSample(
    int ConsoleWidth,
    int LayoutWidth,
    bool Ok,
    int FullBoxLineCount,
    IReadOnlyList<string> Issues,
    IReadOnlyList<LayoutProbeLine> Lines
);

static class NativeMethods
{
    internal const int StdOutputHandle = -11;
    internal const int EnableVirtualTerminalProcessing = 0x0004;

    internal enum CtrlType
    {
        CtrlCEvent = 0,
        CtrlBreakEvent = 1,
        CtrlCloseEvent = 2,
        CtrlLogoffEvent = 5,
        CtrlShutdownEvent = 6,
    }

    internal delegate bool ConsoleCtrlDelegate(CtrlType signal);

    [DllImport("kernel32.dll")]
    internal static extern bool SetConsoleCtrlHandler(ConsoleCtrlDelegate handler, bool add);

    [DllImport("kernel32.dll")]
    internal static extern IntPtr GetStdHandle(int nStdHandle);

    [DllImport("kernel32.dll")]
    internal static extern bool GetConsoleMode(IntPtr hConsoleHandle, out int lpMode);

    [DllImport("kernel32.dll")]
    internal static extern bool SetConsoleMode(IntPtr hConsoleHandle, int dwMode);
}
