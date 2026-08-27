using System.Diagnostics;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace QwenHarness.Desktop;

/// <summary>
/// The Control Room in a native window. Mirrors the Mac app's AppDelegate:
/// same toolbar (Reload / Open in Browser / status), same desktop bridge, same
/// navigation policy, same lifecycle.
/// </summary>
internal sealed class MainForm : Form
{
    private readonly WebView2 _webView = new();
    private readonly ToolStripStatusLabel _status = new("Starting harness…");
    private Process? _serverProcess;
    private Process? _ollamaProcess;
    private Uri? _appUri;
    private bool _bridgeReady;

    private static readonly string StateDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "QwenHarness");
    private static readonly string WindowStatePath = Path.Combine(StateDir, "window.json");

    public MainForm()
    {
        Text = "Qwen Harness";
        MinimumSize = new Size(880, 620);
        StartPosition = FormStartPosition.Manual;
        // Sets the title bar, the taskbar button and the Alt-Tab entry. This
        // is separate from the icon <ApplicationIcon> embeds in the exe, which
        // only covers Explorer — see LoadAppIcon().
        Icon = LoadAppIcon();
        ShowIcon = true;
        RestoreWindowState();

        BuildChrome();
        Load += async (_, _) => await StartHarnessAsync();
        FormClosing += OnFormClosing;
    }

    // ── chrome ───────────────────────────────────────────────────────────
    private void BuildChrome()
    {
        var toolbar = new ToolStrip
        {
            GripStyle = ToolStripGripStyle.Hidden,
            RenderMode = ToolStripRenderMode.System,
            Padding = new Padding(6, 4, 6, 4),
        };

        var reload = new ToolStripButton("⟳  Reload")
        {
            DisplayStyle = ToolStripItemDisplayStyle.Text,
            ToolTipText = "Reload (F5 / Ctrl+R)",
        };
        reload.Click += (_, _) => ReloadPage();

        var openBrowser = new ToolStripButton("↗  Open in Browser")
        {
            DisplayStyle = ToolStripItemDisplayStyle.Text,
            ToolTipText = "Open the Control Room in your default browser (Ctrl+B)",
        };
        openBrowser.Click += (_, _) => OpenInBrowser();

        toolbar.Items.Add(reload);
        toolbar.Items.Add(openBrowser);
        toolbar.Items.Add(new ToolStripSeparator());
        toolbar.Items.Add(new ToolStripLabel("Local models · localhost only") { ForeColor = SystemColors.GrayText });

        var statusStrip = new StatusStrip();
        _status.Spring = true;
        _status.TextAlign = ContentAlignment.MiddleLeft;
        statusStrip.Items.Add(_status);

        _webView.Dock = DockStyle.Fill;
        // A visible white flash before the page paints reads as a broken
        // launch on a dark UI; match the Control Room's background instead.
        _webView.DefaultBackgroundColor = ColorTranslator.FromHtml("#0b0e14");

        Controls.Add(_webView);
        Controls.Add(toolbar);
        Controls.Add(statusStrip);

        // Ctrl+R / F5 / Ctrl+B. WebView2 handles Ctrl+C/V/X/A itself — unlike
        // WKWebView, which needed a whole Edit menu installed before the
        // clipboard worked at all, so there is deliberately no menu bar here.
        KeyPreview = true;
        KeyDown += (_, e) =>
        {
            if (e.KeyCode == Keys.F5 || (e.Control && e.KeyCode == Keys.R)) { ReloadPage(); e.Handled = true; }
            else if (e.Control && e.KeyCode == Keys.B) { OpenInBrowser(); e.Handled = true; }
            else if (e.KeyCode == Keys.F11) { ToggleFullScreen(); e.Handled = true; }
        };
    }

    /// <summary>
    /// The app icon (AppLogo.png, rendered to AppIcon.ico at build time).
    ///
    /// Two independent mechanisms have to agree, and only one of them is the
    /// csproj's job:
    ///
    ///   * &lt;ApplicationIcon&gt; embeds the icon in the executable's PE
    ///     resource section. That is what Explorer, a pinned shortcut and the
    ///     file properties dialog read.
    ///   * Form.Icon is what draws the TITLE BAR, the running taskbar button
    ///     and the Alt-Tab entry. It is a separate runtime property, and
    ///     leaving it null gets the generic WinForms window glyph regardless of
    ///     how good the embedded icon is.
    ///
    /// So this reads the .ico copied beside the exe, and falls back to pulling
    /// the icon back out of our own executable — which is always correct when
    /// &lt;ApplicationIcon&gt; embedded one, and needs no loose file at all.
    /// </summary>
    private static Icon? LoadAppIcon()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "AppIcon.ico");
        try
        {
            // Preferred: the multi-size .ico. Windows picks 16px for the title
            // bar and 32px for Alt-Tab out of it, so all sizes stay crisp —
            // ExtractAssociatedIcon below returns a single size and has to be
            // scaled.
            if (File.Exists(path)) return new Icon(path);
        }
        catch (Exception) { /* fall through to the embedded copy */ }

        try
        {
            var exe = Environment.ProcessPath;
            if (!string.IsNullOrEmpty(exe)) return Icon.ExtractAssociatedIcon(exe);
        }
        catch (Exception) { /* no icon is better than a wrong one */ }

        // Deliberately no stock-icon fallback: a missing icon should leave
        // Windows to its default, not swap in something that reads as a
        // different application.
        return null;
    }

    // ── startup ──────────────────────────────────────────────────────────
    private async Task StartHarnessAsync()
    {
        var repoRoot = HarnessLauncher.FindRepoRoot();
        if (repoRoot is null)
        {
            ShowError("Could not find the qwen-harness checkout. Rebuild with script\\build_and_run.ps1.");
            return;
        }

        var port = await Task.Run(() =>
        {
            _ollamaProcess = HarnessLauncher.StartOllamaIfNeeded(SetStatusFromWorker);
            var result = HarnessLauncher.StartServer(repoRoot, SetStatusFromWorker);
            if (result.Error is not null)
            {
                SetStatusFromWorker(result.Error);
                return -1;
            }
            _serverProcess = result.ServerProcess;
            HarnessLauncher.WaitForHealth(result.Port, TimeSpan.FromSeconds(30));
            return result.Port;
        });

        if (port < 0)
        {
            ShowError(_status.Text ?? "The harness backend could not be started.");
            return;
        }

        _appUri = new Uri($"http://127.0.0.1:{port}/gui/");
        await InitialiseWebViewAsync();
        _status.Text = $"Qwen Harness running at {_appUri}";
        _webView.Source = _appUri;
    }

    private void SetStatusFromWorker(string message)
    {
        if (IsDisposed || !IsHandleCreated) return;
        BeginInvoke(() => _status.Text = message);
    }

    private async Task InitialiseWebViewAsync()
    {
        // An explicit user-data folder: the default sits next to the exe, which
        // is under Program Files for an installed copy and therefore read-only
        // — WebView2 then fails to create its environment at all.
        Directory.CreateDirectory(StateDir);
        var environment = await CoreWebView2Environment.CreateAsync(
            userDataFolder: Path.Combine(StateDir, "WebView2"));
        await _webView.EnsureCoreWebView2Async(environment);

        var core = _webView.CoreWebView2;
        core.Settings.AreDefaultScriptDialogsEnabled = true;   // alert/confirm/prompt
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.AreDefaultContextMenusEnabled = true;
        core.Settings.IsSwipeNavigationEnabled = false;        // no accidental back-swipe mid-run
#if !DEBUG
        core.Settings.AreDevToolsEnabled = false;
#endif

        core.WebMessageReceived += OnWebMessageReceived;
        core.NewWindowRequested += OnNewWindowRequested;
        core.NavigationStarting += OnNavigationStarting;
        core.ProcessFailed += (_, e) =>
            SetStatusFromWorker($"WebView2 process failed ({e.ProcessFailedKind}) — press F5 to reload.");
        _bridgeReady = true;
    }

    // ── navigation policy ────────────────────────────────────────────────
    // Same rule as the Mac app: only the local Control Room loads in-window;
    // anything else opens in the real browser. This is what stops a scraped
    // page or a model-emitted link from navigating the app away from itself.
    private void OnNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
    {
        if (IsLocalApp(e.Uri) || e.Uri.StartsWith("about:", StringComparison.OrdinalIgnoreCase)) return;
        e.Cancel = true;
        OpenExternally(e.Uri);
    }

    private void OnNewWindowRequested(object? sender, CoreWebView2NewWindowRequestedEventArgs e)
    {
        // target="_blank" links (the GitHub token page, search results) belong
        // in the user's browser, where they are already signed in — not in a
        // second chrome-less window with no address bar.
        e.Handled = true;
        OpenExternally(e.Uri);
    }

    private bool IsLocalApp(string uri) =>
        Uri.TryCreate(uri, UriKind.Absolute, out var parsed)
        && parsed.Scheme == Uri.UriSchemeHttp
        && parsed.Host == "127.0.0.1"
        && _appUri is not null
        && parsed.Port == _appUri.Port;

    private static void OpenExternally(string uri)
    {
        if (!Uri.TryCreate(uri, UriKind.Absolute, out var parsed)) return;
        if (parsed.Scheme != Uri.UriSchemeHttp && parsed.Scheme != Uri.UriSchemeHttps) return;
        try { Process.Start(new ProcessStartInfo(parsed.AbsoluteUri) { UseShellExecute = true }); }
        catch (Exception) { /* no default browser is not worth an error dialog */ }
    }

    // ── the desktop bridge ───────────────────────────────────────────────
    // gui/capabilities.js posts {id, action} and waits for
    // window.__qhDesktopResolve(id, payload). Identical protocol to the Mac
    // app's WKScriptMessageHandler, so the frontend needs no host-specific
    // branch beyond picking the channel.
    private void OnWebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        string? id = null, action = null;
        try
        {
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            if (doc.RootElement.TryGetProperty("id", out var idNode)) id = idNode.GetString();
            if (doc.RootElement.TryGetProperty("action", out var actionNode)) action = actionNode.GetString();
        }
        catch (JsonException)
        {
            return;   // not our envelope
        }
        if (id is null || action is null) return;

        switch (action)
        {
            case "chooseDirectory":
                ChooseDirectory(id);
                break;
            case "chooseFile":
                ChooseFile(id);
                break;
            default:
                Resolve(id, new { ok = false, error = "unknown desktop action" });
                break;
        }
    }

    private void ChooseDirectory(string id)
    {
        using var dialog = new FolderBrowserDialog
        {
            Description = "Choose a folder Qwen Harness can browse, edit, and run commands inside.",
            UseDescriptionForTitle = true,
            ShowNewFolderButton = true,
        };
        if (dialog.ShowDialog(this) == DialogResult.OK && !string.IsNullOrWhiteSpace(dialog.SelectedPath))
            Resolve(id, new { ok = true, path = dialog.SelectedPath });
        else
            // Cancelling is a no-op, not a failure — the frontend checks for
            // this flag and stays silent rather than showing an error toast.
            Resolve(id, new { ok = false, cancelled = true });
    }

    private void ChooseFile(string id)
    {
        using var dialog = new OpenFileDialog
        {
            Title = "Choose a file Qwen Harness can read or edit.",
            Multiselect = false,
            CheckFileExists = true,
        };
        if (dialog.ShowDialog(this) == DialogResult.OK && !string.IsNullOrWhiteSpace(dialog.FileName))
            Resolve(id, new { ok = true, path = dialog.FileName });
        else
            Resolve(id, new { ok = false, cancelled = true });
    }

    private void Resolve(string id, object payload)
    {
        if (!_bridgeReady || _webView.CoreWebView2 is null) return;
        // Both arguments go through JsonSerializer: an id or a path containing
        // a quote or a backslash (routine on Windows) would otherwise break
        // out of the script string.
        var script = $"window.__qhDesktopResolve && window.__qhDesktopResolve(" +
                     $"{JsonSerializer.Serialize(id)}, {JsonSerializer.Serialize(payload)});";
        _ = _webView.CoreWebView2.ExecuteScriptAsync(script);
    }

    // ── commands ─────────────────────────────────────────────────────────
    private void ReloadPage() => _webView.CoreWebView2?.Reload();

    private void OpenInBrowser()
    {
        if (_appUri is not null) OpenExternally(_appUri.AbsoluteUri);
    }

    private FormWindowState _preFullScreenState = FormWindowState.Normal;
    private bool _isFullScreen;

    private void ToggleFullScreen()
    {
        if (_isFullScreen)
        {
            FormBorderStyle = FormBorderStyle.Sizable;
            WindowState = _preFullScreenState;
        }
        else
        {
            _preFullScreenState = WindowState;
            // Normal first: Windows ignores a border-style change while the
            // form is already maximized, so the frame would stay on screen.
            WindowState = FormWindowState.Normal;
            FormBorderStyle = FormBorderStyle.None;
            WindowState = FormWindowState.Maximized;
        }
        _isFullScreen = !_isFullScreen;
    }

    private void ShowError(string message)
    {
        _status.Text = message;
        MessageBox.Show(this, message, "Qwen Harness could not start",
            MessageBoxButtons.OK, MessageBoxIcon.Warning);
    }

    // ── window state ─────────────────────────────────────────────────────
    private sealed record WindowState_(int X, int Y, int Width, int Height, bool Maximized);

    private void RestoreWindowState()
    {
        Size = new Size(1280, 860);
        var screen = Screen.PrimaryScreen?.WorkingArea ?? new Rectangle(0, 0, 1280, 860);
        Location = new Point(screen.X + (screen.Width - Width) / 2, screen.Y + (screen.Height - Height) / 2);
        try
        {
            if (!File.Exists(WindowStatePath)) return;
            var saved = JsonSerializer.Deserialize<WindowState_>(File.ReadAllText(WindowStatePath));
            if (saved is null || saved.Width < 400 || saved.Height < 300) return;
            // Only restore a position that is still on a connected monitor:
            // a window remembered from an unplugged second screen would open
            // entirely off-screen with no way to reach it.
            var bounds = new Rectangle(saved.X, saved.Y, saved.Width, saved.Height);
            if (Screen.AllScreens.Any(s => s.WorkingArea.IntersectsWith(bounds)))
            {
                Location = new Point(saved.X, saved.Y);
                Size = new Size(saved.Width, saved.Height);
            }
            if (saved.Maximized) WindowState = FormWindowState.Maximized;
        }
        catch (Exception) { /* a corrupt state file is not worth failing launch over */ }
    }

    private void SaveWindowState()
    {
        try
        {
            Directory.CreateDirectory(StateDir);
            // RestoreBounds, not Bounds: while maximized, Bounds is the screen
            // and restoring it would lose the user's real window size.
            var bounds = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
            var state = new WindowState_(bounds.X, bounds.Y, bounds.Width, bounds.Height,
                WindowState == FormWindowState.Maximized);
            File.WriteAllText(WindowStatePath, JsonSerializer.Serialize(state));
        }
        catch (Exception) { /* best effort */ }
    }

    // ── shutdown ─────────────────────────────────────────────────────────
    private void OnFormClosing(object? sender, FormClosingEventArgs e)
    {
        SaveWindowState();

        // Only stop what this app started. A server or an Ollama that was
        // already running is shared with a browser tab or another window, and
        // killing it out from under an in-flight run is exactly the kind of
        // surprise the runbook warns about.
        TryStop(_serverProcess);
        TryStop(_ollamaProcess);
    }

    private static void TryStop(Process? process)
    {
        if (process is null || process.HasExited) return;
        try
        {
            process.Kill(entireProcessTree: true);
            process.WaitForExit(3000);
        }
        catch (Exception) { /* already gone */ }
    }
}
