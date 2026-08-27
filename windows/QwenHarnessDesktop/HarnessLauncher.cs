using System.Diagnostics;
using System.Net.Sockets;
using System.Text.Json;

namespace QwenHarness.Desktop;

/// <summary>
/// Brings the harness up: Ollama, then gui/server.py, then waits for health.
/// The Windows counterpart to startHarness()/startOllamaIfNeeded() in
/// Sources/QwenHarnessDesktop/main.swift, and it must stay behaviourally
/// identical — the two apps talk to the same backend and the same state dir.
/// </summary>
internal static class HarnessLauncher
{
    public const int DefaultPort = 8420;
    private const int PortScanRange = 20;

    /// <summary>
    /// Runtime tuning for the Ollama server. Must stay in sync with the same
    /// block in wake-up.ps1 (and wake-up.sh / main.swift on the Mac) — if only
    /// one launcher sets these, starting the harness the other way silently
    /// reverts to Ollama's 4096-token / 5-minute defaults, and a 4k context
    /// cannot hold the tool schemas plus a source file plus the conversation.
    /// Ollama does not error on overflow, it drops the oldest content, so the
    /// symptom is a bizarre-looking hallucination rather than a truncation
    /// message.
    ///
    /// The context length differs from the Mac's 8192 on purpose: it is sized
    /// from real RAM by tools/context_budget.py, and this machine class has
    /// 16 GB rather than 8. MAX_LOADED_MODELS stays at 1 — two resident
    /// contexts still thrash, they just thrash at a higher watermark.
    /// </summary>
    private static readonly (string Key, string Value)[] OllamaEnvironment =
    {
        ("OLLAMA_CONTEXT_LENGTH", "16384"),
        ("OLLAMA_KEEP_ALIVE", "30m"),
        ("OLLAMA_MAX_LOADED_MODELS", "1"),
        ("OLLAMA_NUM_PARALLEL", "1"),
    };

    public sealed record StartResult(int Port, Process? ServerProcess, string? Error);

    // ── repo root ────────────────────────────────────────────────────────
    /// <summary>
    /// The qwen-harness checkout this app was built from.
    /// repo-root.txt is written next to the exe at build time, exactly as the
    /// Mac build writes it into Contents/Resources.
    /// </summary>
    public static string? FindRepoRoot()
    {
        var beside = Path.Combine(AppContext.BaseDirectory, "repo-root.txt");
        if (File.Exists(beside))
        {
            try
            {
                var recorded = File.ReadAllText(beside).Trim();
                if (IsRepoRoot(recorded)) return recorded;
            }
            catch (IOException) { /* fall through to the walk */ }
        }

        // Walk up from the exe and from the working directory. This is what
        // makes `dotnet run` from the project folder work without a build step
        // having written repo-root.txt yet.
        foreach (var start in new[] { AppContext.BaseDirectory, Environment.CurrentDirectory })
        {
            var candidate = new DirectoryInfo(start);
            for (var i = 0; i < 6 && candidate is not null; i++)
            {
                if (IsRepoRoot(candidate.FullName)) return candidate.FullName;
                candidate = candidate.Parent;
            }
        }
        return null;
    }

    private static bool IsRepoRoot(string? path) =>
        !string.IsNullOrWhiteSpace(path) && File.Exists(Path.Combine(path, "gui", "server.py"));

    // ── executable discovery ─────────────────────────────────────────────
    /// <summary>
    /// Absolute path to an executable, PATH or not.
    ///
    /// A shortcut-launched Windows app does not inherit a shell profile's PATH
    /// edits, and Ollama installs per-user into %LOCALAPPDATA%\Programs\Ollama
    /// — which is exactly the directory such a process cannot see. This is the
    /// Windows shape of the Finder-PATH trap documented in the runbook.
    /// </summary>
    public static string? FindExecutable(string name)
    {
        var exts = new[] { ".exe", ".cmd", ".bat", "" };
        var searchDirs = new List<string>();
        var path = Environment.GetEnvironmentVariable("PATH") ?? "";
        searchDirs.AddRange(path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries));
        searchDirs.AddRange(ExtraSearchDirectories());

        foreach (var dir in searchDirs)
        {
            foreach (var ext in exts)
            {
                try
                {
                    var candidate = Path.Combine(dir.Trim('"'), name + ext);
                    if (File.Exists(candidate)) return candidate;
                }
                catch (ArgumentException) { /* an unusable PATH entry is not fatal */ }
            }
        }
        return null;
    }

    private static IEnumerable<string> ExtraSearchDirectories()
    {
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        yield return Path.Combine(local, "Programs", "Ollama");
        yield return Path.Combine(local, "Ollama");
        yield return Path.Combine(programFiles, "Ollama");
        yield return Path.Combine(programFiles, "nodejs");
        yield return Path.Combine(programFiles, "Git", "cmd");
        yield return Path.Combine(local, "Programs", "Python", "Python312");
        yield return Path.Combine(local, "Microsoft", "WindowsApps");
    }

    /// <summary>
    /// The Python that should run gui/server.py.
    ///
    /// Deliberately not "python3": on Windows that name is a Microsoft Store
    /// redirector stub under WindowsApps which exits 9009 and pops the Store
    /// open. The launcher prefers a real python.exe and falls back to the `py`
    /// launcher, which is what a standard python.org install registers.
    /// </summary>
    public static string? FindPython()
    {
        foreach (var name in new[] { "python", "py" })
        {
            var found = FindExecutable(name);
            // Skip the WindowsApps redirector stubs: they exist as files but
            // are 0-byte reparse points that do not run Python.
            if (found is null) continue;
            if (found.Contains("WindowsApps", StringComparison.OrdinalIgnoreCase)
                && new FileInfo(found).Length == 0) continue;
            return found;
        }
        return null;
    }

    // ── Ollama ───────────────────────────────────────────────────────────
    public static Process? StartOllamaIfNeeded(Action<string> log)
    {
        var binary = FindExecutable("ollama");
        if (binary is null)
        {
            log("Ollama not found — download it from https://ollama.com/download/windows");
            return null;
        }

        // An already-running server was started without our environment, so it
        // still has Ollama's 4096-token default. Restart it so the settings
        // actually apply — matching wake-up.ps1 and the Mac app.
        var running = Process.GetProcessesByName("ollama");
        if (running.Length > 0)
        {
            log("Restarting Ollama to apply context/keep-alive settings");
            foreach (var proc in running)
            {
                try { proc.Kill(entireProcessTree: true); proc.WaitForExit(5000); }
                catch (Exception) { /* already gone, or not ours to kill */ }
            }
            // Wait for the listener to actually go away rather than sleeping
            // blindly: a fresh serve that loses the bind race silently exits.
            for (var i = 0; i < 20 && IsPortOpen(11434); i++) Thread.Sleep(250);
        }

        var info = new ProcessStartInfo(binary, "serve")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var (key, value) in OllamaEnvironment)
        {
            // Only set what the user has not already set for themselves.
            if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable(key)))
                info.Environment[key] = value;
        }

        try
        {
            var process = Process.Start(info);
            // Drain both pipes. A redirected pipe nobody reads fills its ~4KB
            // buffer and then blocks the child forever — Ollama logs steadily,
            // so this is a matter of minutes, not a theoretical risk.
            process?.BeginOutputReadLine();
            process?.BeginErrorReadLine();
            log($"Ollama started ({binary})");
            return process;
        }
        catch (Exception exc)
        {
            log($"Could not start Ollama: {exc.Message}");
            return null;
        }
    }

    // ── the GUI server ───────────────────────────────────────────────────
    public static StartResult StartServer(string repoRoot, Action<string> log)
    {
        var python = FindPython();
        if (python is null)
            return new StartResult(DefaultPort, null, "Python was not found. Install Python 3.9+ from python.org.");

        for (var port = DefaultPort; port < DefaultPort + PortScanRange; port++)
        {
            // An already-healthy harness on this port is reused, not replaced:
            // a browser tab and this window are meant to share one backend and
            // one state directory.
            if (ServerReady(port))
            {
                log($"Reusing the harness already listening on :{port}");
                return new StartResult(port, null, null);
            }
            if (IsPortOpen(port)) continue;   // something else owns it

            var info = new ProcessStartInfo(python)
            {
                WorkingDirectory = repoRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            info.ArgumentList.Add(Path.Combine("gui", "server.py"));
            info.ArgumentList.Add(port.ToString());

            try
            {
                var process = Process.Start(info);
                if (process is null) continue;
                var logPath = Path.Combine(Path.GetTempPath(), "qwen-harness-gui.log");
                AttachLogWriter(process, logPath);
                log($"Started gui/server.py on :{port} (log: {logPath})");
                return new StartResult(port, process, null);
            }
            catch (Exception exc)
            {
                return new StartResult(port, null, $"Could not start gui/server.py: {exc.Message}");
            }
        }
        return new StartResult(DefaultPort, null,
            $"No free port in {DefaultPort}-{DefaultPort + PortScanRange - 1}.");
    }

    private static void AttachLogWriter(Process process, string logPath)
    {
        // One writer, one lock: stdout and stderr arrive on separate threads.
        var gate = new object();
        void Write(string? line)
        {
            if (line is null) return;
            lock (gate)
            {
                try { File.AppendAllText(logPath, line + Environment.NewLine); }
                catch (IOException) { /* a log that cannot be written is not fatal */ }
            }
        }
        process.OutputDataReceived += (_, e) => Write(e.Data);
        process.ErrorDataReceived += (_, e) => Write(e.Data);
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
    }

    // ── readiness ────────────────────────────────────────────────────────
    public static void WaitForHealth(int port, TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (ServerReady(port)) return;
            Thread.Sleep(250);
        }
    }

    /// <summary>
    /// Health means more than "something answered on the port": it means our
    /// server, with its filesystem-grants API live. The Mac app checks the
    /// same two endpoints, and for the same reason — a stale process from a
    /// previous version answers /api/health and then fails every real call.
    /// </summary>
    public static bool ServerReady(int port)
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            var health = client.GetStringAsync($"http://127.0.0.1:{port}/api/health").GetAwaiter().GetResult();
            using var healthDoc = JsonDocument.Parse(health);
            if (!healthDoc.RootElement.TryGetProperty("ok", out var ok) || !ok.GetBoolean()) return false;

            var grants = client.GetStringAsync($"http://127.0.0.1:{port}/api/fs/grants").GetAwaiter().GetResult();
            using var grantsDoc = JsonDocument.Parse(grants);
            return grantsDoc.RootElement.TryGetProperty("roots", out var roots)
                   && roots.ValueKind == JsonValueKind.Array;
        }
        catch (Exception)
        {
            return false;
        }
    }

    public static bool IsPortOpen(int port)
    {
        try
        {
            using var client = new TcpClient();
            // A connect with no timeout blocks for the OS default (~20s) on a
            // filtered port; the port scan does this up to 20 times.
            return client.ConnectAsync("127.0.0.1", port).Wait(TimeSpan.FromMilliseconds(300))
                   && client.Connected;
        }
        catch (Exception)
        {
            return false;
        }
    }
}
