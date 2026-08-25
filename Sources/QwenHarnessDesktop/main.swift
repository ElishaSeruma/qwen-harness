import AppKit
import Foundation
import WebKit
import Darwin
import OSLog

private let defaultPort = 8420
private let logger = Logger(subsystem: "local.qwen-harness.desktop", category: "lifecycle")

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSTextField!
    private var serverProcess: Process?
    private var ollamaProcess: Process?
    private var appURL: URL?

    func applicationDidFinishLaunching(_ notification: Notification) {
        installApplicationIcon()
        buildMenus()
        buildWindow()
        startHarness()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        serverProcess?.terminate()
        ollamaProcess?.terminate()
        logger.info("Application terminating")
    }

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.preferences.javaScriptCanOpenWindowsAutomatically = true
        config.userContentController.add(self, name: "desktop")

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self

        let reloadButton = NSButton(image: NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "Reload")!, target: self, action: #selector(reloadPage))
        reloadButton.bezelStyle = .rounded
        reloadButton.toolTip = "Reload"
        let browserButton = NSButton(image: NSImage(systemSymbolName: "safari", accessibilityDescription: "Open in Browser")!, target: self, action: #selector(openInBrowser))
        browserButton.bezelStyle = .rounded
        browserButton.toolTip = "Open in Browser"
        statusItem = NSTextField(labelWithString: "Starting harness...")
        statusItem.lineBreakMode = .byTruncatingTail
        statusItem.textColor = .secondaryLabelColor

        let toolbar = NSStackView(views: [reloadButton, browserButton, statusItem])
        toolbar.orientation = .horizontal
        toolbar.alignment = .centerY
        toolbar.spacing = 10
        toolbar.edgeInsets = NSEdgeInsets(top: 8, left: 10, bottom: 8, right: 10)
        statusItem.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let root = NSView()
        root.addSubview(toolbar)
        root.addSubview(webView)
        root.translatesAutoresizingMaskIntoConstraints = false
        webView.translatesAutoresizingMaskIntoConstraints = false
        toolbar.translatesAutoresizingMaskIntoConstraints = false

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Qwen Harness"
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("QwenHarnessMainWindow")
        window.contentView = root
        if !window.setFrameUsingName("QwenHarnessMainWindow") { window.center() }

        NSLayoutConstraint.activate([
            toolbar.topAnchor.constraint(equalTo: root.topAnchor),
            toolbar.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            toolbar.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            toolbar.heightAnchor.constraint(equalToConstant: 48),
            webView.topAnchor.constraint(equalTo: toolbar.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            webView.widthAnchor.constraint(equalTo: root.widthAnchor),
        ])

        showMainWindow()
        logger.info("Main window created")
    }

    private func buildMenus() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Qwen Harness", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Qwen Harness", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(withTitle: "Reload", action: #selector(reloadPage), keyEquivalent: "r").target = self
        viewMenu.addItem(withTitle: "Open in Browser", action: #selector(openInBrowser), keyEquivalent: "b").target = self
        viewMenu.addItem(.separator())
        viewMenu.addItem(withTitle: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        viewItem.submenu = viewMenu
        NSApp.mainMenu = main
    }

    private func installApplicationIcon() {
        if let icon = NSImage(named: "AppIcon") {
            NSApp.applicationIconImage = icon
            return
        }
        if let logoPath = Bundle.main.path(forResource: "AppLogo", ofType: "png"),
           let logo = NSImage(contentsOfFile: logoPath) {
            NSApp.applicationIconImage = logo
        }
    }

    private func showMainWindow() {
        guard let window else { return }
        window.deminiaturize(nil)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
    }

    private func startHarness() {
        guard let repoRoot = findRepoRoot() else {
            showError("Could not find qwen-harness repo root in the app bundle.")
            return
        }

        startOllamaIfNeeded()

        DispatchQueue.global(qos: .userInitiated).async {
            let server = Self.startServerIfNeeded(repoRoot: repoRoot)
            Self.waitForHealth(port: server.port, timeout: 12)

            let url = URL(string: "http://127.0.0.1:\(server.port)/gui/")!
            DispatchQueue.main.async {
                self.serverProcess = server.process
                self.appURL = url
                self.statusItem.stringValue = "Qwen Harness running at \(url.absoluteString)"
                logger.info("Harness ready at \(url.absoluteString, privacy: .public)")
                self.showMainWindow()
                self.webView.load(URLRequest(url: url))
            }
        }
    }

    private func findRepoRoot() -> URL? {
        if let resourcePath = Bundle.main.path(forResource: "repo-root", ofType: "txt"),
           let text = try? String(contentsOfFile: resourcePath, encoding: .utf8) {
            let url = URL(fileURLWithPath: text.trimmingCharacters(in: .whitespacesAndNewlines), isDirectory: true)
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("gui/server.py").path) {
                return url
            }
        }

        var candidate = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
        for _ in 0..<5 {
            if FileManager.default.fileExists(atPath: candidate.appendingPathComponent("gui/server.py").path) {
                return candidate
            }
            candidate.deleteLastPathComponent()
        }
        return nil
    }

    private func startOllamaIfNeeded() {
        guard !Self.processExists(named: "ollama"), let ollamaPath = Self.findExecutable("ollama") else {
            return
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: ollamaPath)
        process.arguments = ["serve"]
        process.standardOutput = Self.fileHandle(path: "/tmp/ollama.log")
        process.standardError = process.standardOutput
        do {
            try process.run()
            ollamaProcess = process
        } catch {
            logger.error("Could not start Ollama: \(error.localizedDescription, privacy: .public)")
        }
    }

    nonisolated private static func startServerIfNeeded(repoRoot: URL) -> (port: Int, process: Process?) {
        for port in defaultPort..<(defaultPort + 20) {
            if serverReady(port: port) {
                return (port, nil)
            }
            if isPortOpen(port) {
                continue
            }
            return (port, launchServer(repoRoot: repoRoot, port: port))
        }
        return (defaultPort, nil)
    }

    nonisolated private static func launchServer(repoRoot: URL, port: Int) -> Process? {
        let process = Process()
        process.currentDirectoryURL = repoRoot
        process.executableURL = URL(fileURLWithPath: findExecutable("python3") ?? "/usr/bin/python3")
        process.arguments = ["gui/server.py", String(port)]
        process.standardOutput = fileHandle(path: "/tmp/qwen-harness-gui.log")
        process.standardError = process.standardOutput
        do {
            try process.run()
            return process
        } catch {
            logger.error("Could not start gui/server.py: \(error.localizedDescription, privacy: .public)")
            return nil
        }
    }

    nonisolated private static func waitForHealth(port: Int, timeout: TimeInterval) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if serverReady(port: port) {
                return
            }
            Thread.sleep(forTimeInterval: 0.25)
        }
    }

    nonisolated private static func serverReady(port: Int) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health"),
              let data = try? Data(contentsOf: url, options: []) else {
            return false
        }
        guard (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["ok"] as? Bool == true else {
            return false
        }
        guard let grantsURL = URL(string: "http://127.0.0.1:\(port)/api/fs/grants"),
              let grantsData = try? Data(contentsOf: grantsURL, options: []),
              let grants = try? JSONSerialization.jsonObject(with: grantsData) as? [String: Any] else {
            return false
        }
        return grants["roots"] is [Any]
    }

    nonisolated private static func isPortOpen(_ port: Int) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = UInt16(port).bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")

        return withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
    }

    nonisolated private static func processExists(named name: String) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        process.arguments = ["-x", name]
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }

    nonisolated private static func findExecutable(_ name: String) -> String? {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["which", name]
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return nil }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            return path?.isEmpty == false ? path : nil
        } catch {
            return nil
        }
    }

    nonisolated private static func fileHandle(path: String) -> FileHandle? {
        FileManager.default.createFile(atPath: path, contents: nil)
        return FileHandle(forWritingAtPath: path)
    }

    @objc private func reloadPage() {
        webView.reload()
    }

    @objc private func openInBrowser() {
        if let appURL {
            NSWorkspace.shared.open(appURL)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        let isLocalApp = url.scheme == "http" && url.host == "127.0.0.1" && url.port == appURL?.port
        if isLocalApp || url.scheme == "about" {
            decisionHandler(.allow)
        } else if navigationAction.navigationType == .linkActivated || navigationAction.targetFrame == nil {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        } else {
            logger.warning("Blocked web view navigation to \(url.absoluteString, privacy: .public)")
            decisionHandler(.cancel)
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "desktop",
              let body = message.body as? [String: Any],
              let id = body["id"] as? String,
              let action = body["action"] as? String else {
            return
        }

        switch action {
        case "chooseDirectory":
            choosePath(id: id, directories: true, files: false)
        case "chooseFile":
            choosePath(id: id, directories: false, files: true)
        default:
            resolveDesktopRequest(id: id, payload: ["ok": false, "error": "unknown desktop action"])
        }
    }

    // WKWebView does not show a native file picker for <input type="file">
    // on its own — without this delegate method, clicking a file input in
    // the web UI (e.g. the file-upload capability, Document Dock) silently
    // does nothing. This is the fix for that.
    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @MainActor @Sendable ([URL]?) -> Void
    ) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canCreateDirectories = false
        panel.prompt = "Choose"

        panel.beginSheetModal(for: window) { response in
            if response == .OK {
                completionHandler(panel.urls)
            } else {
                completionHandler(nil)
            }
        }
    }

    // WKWebView has no default UI for alert()/confirm()/prompt() — without
    // these three delegate methods, WebKit silently auto-dismisses them
    // (confirm() always resolves to `false`, with no dialog ever shown).
    // The entire Projects feature gates its destructive actions (delete
    // project, push, pull, commit, merge, checkout, duplicate, delete .git)
    // behind confirm() — that's why none of them appeared to work on Mac:
    // every confirmation was being silently auto-cancelled.
    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @MainActor @Sendable () -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = message
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @MainActor @Sendable (Bool) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { response in
            completionHandler(response == .alertFirstButtonReturn)
        }
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @MainActor @Sendable (String?) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(string: defaultText ?? "")
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.beginSheetModal(for: window) { response in
            completionHandler(response == .alertFirstButtonReturn ? field.stringValue : nil)
        }
    }

    private func choosePath(id: String, directories: Bool, files: Bool) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = directories
        panel.canChooseFiles = files
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = directories
        panel.prompt = directories ? "Grant Access" : "Open"
        panel.message = directories
            ? "Choose a folder Qwen Harness can browse, edit, and run commands inside."
            : "Choose a file Qwen Harness can read or edit."

        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            if response == .OK, let url = panel.url {
                self.resolveDesktopRequest(id: id, payload: ["ok": true, "path": url.path])
            } else {
                self.resolveDesktopRequest(id: id, payload: ["ok": false, "cancelled": true])
            }
        }
    }

    private func resolveDesktopRequest(id: String, payload: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let json = String(data: data, encoding: .utf8) else {
            return
        }
        let escapedId = id.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "'", with: "\\'")
        webView.evaluateJavaScript("window.__qhDesktopResolve && window.__qhDesktopResolve('\(escapedId)', \(json));")
    }

    private func showError(_ message: String) {
        statusItem.stringValue = message
        let alert = NSAlert()
        alert.messageText = "Qwen Harness could not start"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}

private let app = NSApplication.shared
private let delegate = AppDelegate()
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
