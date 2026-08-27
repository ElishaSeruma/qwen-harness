using System.Diagnostics;

namespace QwenHarness.Desktop;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        // --verify mirrors script/build_and_run.sh --verify on the Mac: launch,
        // confirm the process is alive, print where it came from. It is what
        // makes "it built" and "it actually runs" two different claims.
        if (args.Contains("--version"))
        {
            Console.WriteLine("Qwen Harness Desktop 1.0.0");
            return;
        }

        ApplicationConfiguration.Initialize();

        // WebView2 is a runtime dependency, not a bundled one. Failing here
        // with the download link beats the control throwing an opaque
        // "class not registered" the first time the window paints.
        if (!WebView2RuntimeAvailable(out var detail))
        {
            MessageBox.Show(
                "The Microsoft Edge WebView2 Runtime is required.\n\n" +
                "Install it from https://developer.microsoft.com/microsoft-edge/webview2/ " +
                "and start Qwen Harness again.\n\n" + detail,
                "Qwen Harness", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        Application.Run(new MainForm());
    }

    private static bool WebView2RuntimeAvailable(out string detail)
    {
        try
        {
            var version = Microsoft.Web.WebView2.Core.CoreWebView2Environment
                .GetAvailableBrowserVersionString();
            detail = $"Detected: {version ?? "none"}";
            return !string.IsNullOrEmpty(version);
        }
        catch (Exception exc)
        {
            detail = exc.Message;
            return false;
        }
    }
}
