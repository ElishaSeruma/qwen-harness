import AppKit
import Foundation
import WebKit
import Darwin

private let defaultPort = 8420

@main
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSTextField!
    private var serverProcess: Process?
    private var ollamaProcess: Process?
    private var appURL: URL?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildWindow()
        startHarness()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        serverProcess?.terminate()
    }

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.preferences.javaScriptCanOpenWindowsAutomatically = true

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self

        let reloadButton = NSButton(title: "Reload", target: self, action: #selector(reloadPage))
        reloadButton.bezelStyle = .rounded
        let browserButton = NSButton(title: "Open in Browser", target: self, action: #selector(openInBrowser))
        browserButton.bezelStyle = .rounded
        statusItem = NSTextField(labelWithString: "Starting harness...")
        statusItem.lineBreakMode = .byTruncatingTail
        statusItem.textColor = .secondaryLabelColor

        let toolbar = NSStackView(views: [reloadButton, browserButton, statusItem])
        toolbar.orientation = .horizontal
        toolbar.alignment = .centerY
        toolbar.spacing = 10
        toolbar.edgeInsets = NSEdgeInsets(top: 8, left: 10, bottom: 8, right: 10)
        statusItem.setContentHuggingPriority(.defaultLow, for: .horizontal)

        let root = NSStackView(views: [toolbar, webView])
        root.orientation = .vertical
        root.spacing = 0
        webView.translatesAutoresizingMaskIntoConstraints = false
        toolbar.translatesAutoresizingMaskIntoConstraints = false

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Qwen Harness"
        window.contentView = root
        window.center()
        window.makeKeyAndOrderFront(nil)

        NSLayoutConstraint.activate([
            toolbar.heightAnchor.constraint(equalToConstant: 46),
            webView.widthAnchor.constraint(equalTo: root.widthAnchor),
            webView.heightAnchor.constraint(equalTo: root.heightAnchor, constant: -46)
        ])
    }

    private func startHarness() {
        guard let repoRoot = findRepoRoot() else {
            showError("Could not find qwen-harness repo root in the app bundle.")
            return
        }

        startOllamaIfNeeded()

        let port = startServerIfNeeded(repoRoot: repoRoot)
        waitForHealth(port: port, timeout: 12)

        let url = URL(string: "http://127.0.0.1:\(port)/gui/")!
        appURL = url
        statusItem.stringValue = "Qwen Harness running at \(url.absoluteString)"
        webView.load(URLRequest(url: url))
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
        guard !processExists(named: "ollama"), let ollamaPath = findExecutable("ollama") else {
            return
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: ollamaPath)
        process.arguments = ["serve"]
        process.standardOutput = fileHandle(path: "/tmp/ollama.log")
        process.standardError = process.standardOutput
        do {
            try process.run()
            ollamaProcess = process
        } catch {
            NSLog("Could not start Ollama: \(error.localizedDescription)")
        }
    }

    private func startServerIfNeeded(repoRoot: URL) -> Int {
        for port in defaultPort..<(defaultPort + 20) {
            if healthOK(port: port) {
                return port
            }
            if isPortOpen(port) {
                continue
            }
            launchServer(repoRoot: repoRoot, port: port)
            return port
        }
        return defaultPort
    }

    private func launchServer(repoRoot: URL, port: Int) {
        let process = Process()
        process.currentDirectoryURL = repoRoot
        process.executableURL = URL(fileURLWithPath: findExecutable("python3") ?? "/usr/bin/python3")
        process.arguments = ["gui/server.py", String(port)]
        process.standardOutput = fileHandle(path: "/tmp/qwen-harness-gui.log")
        process.standardError = process.standardOutput
        do {
            try process.run()
            serverProcess = process
        } catch {
            DispatchQueue.main.async {
                self.showError("Could not start gui/server.py: \(error.localizedDescription)")
            }
        }
    }

    private func waitForHealth(port: Int, timeout: TimeInterval) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if healthOK(port: port) {
                return
            }
            Thread.sleep(forTimeInterval: 0.25)
        }
    }

    private func healthOK(port: Int) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health"),
              let data = try? Data(contentsOf: url, options: []) else {
            return false
        }
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["ok"] as? Bool == true
    }

    private func isPortOpen(_ port: Int) -> Bool {
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

    private func processExists(named name: String) -> Bool {
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

    private func findExecutable(_ name: String) -> String? {
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

    private func fileHandle(path: String) -> FileHandle? {
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

    private func showError(_ message: String) {
        statusItem.stringValue = message
        let alert = NSAlert()
        alert.messageText = "Qwen Harness could not start"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}
