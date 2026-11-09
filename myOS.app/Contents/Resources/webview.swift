import Cocoa
import CryptoKit
import WebKit

// myOS — the bundle's main executable is THIS compiled Cocoa app (a real
// Mach-O), not a shell script. macOS LaunchServices refuses to launch an .app
// whose CFBundleExecutable is an interpreted script (it times out with
// errAETimeout / -1712), so the window must come up from a proper GUI binary.
// The Python dashboard server is started asynchronously via start-server.sh so
// the launch acknowledges immediately and we never block app startup.

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var serverPID: Int32 = 0
    let port = 8765
    var url: URL { URL(string: "http://127.0.0.1:\(port)")! }

    func applicationDidFinishLaunching(_: Notification) {
        installMainMenu()

        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: .zero, configuration: cfg)
        // Without a uiDelegate, WKWebView silently ignores <input type=file>
        // clicks — the chat's attach button looks dead. See runOpenPanel below.
        webView.uiDelegate = self
        // The window only ever shows myOS itself: any other web address opens in the
        // default browser, so a link in a bot's text never loads inside the app window.
        webView.navigationDelegate = self

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "myOS"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden          // hide "myOS" text from title bar
        window.isMovableByWindowBackground = true  // drag anywhere on the window
        window.contentView = webView
        window.setFrameAutosaveName("myOSWindow")
        window.center()
        window.makeKeyAndOrderFront(nil)

        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)

        // Show a splash immediately so the launch completes (no -1712 timeout),
        // then bring up the server and load the dashboard off the main thread.
        webView.loadHTMLString(splashHTML, baseURL: nil)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.startServerAndLoad()
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let target = action.request.url else { decisionHandler(.cancel); return }
        let scheme = target.scheme?.lowercased() ?? ""
        if scheme == "about" || scheme == "blob" || scheme == "data" {
            decisionHandler(.allow); return
        }
        if scheme == "http", target.host == url.host, target.port == url.port {
            decisionHandler(.allow); return
        }
        if scheme == "http" || scheme == "https" { NSWorkspace.shared.open(target) }
        decisionHandler(.cancel)
    }

    // WKWebView calls this when the page triggers an <input type=file>. Without
    // it, the file picker never appears (the chat's ⊕ attach button is inert).
    func webView(_ webView: WKWebView,
                 runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.begin { result in
            completionHandler(result == .OK ? panel.urls : nil)
        }
    }

    // WKWebView routes window.alert/confirm/prompt to these delegate methods.
    // Without them the calls are no-ops: alert() does nothing, and crucially
    // confirm() returns FALSE — so every `if(!confirm(...)) return;` guard in the
    // dashboard (all the delete buttons) silently aborts. Wire them to native
    // panels so confirmations actually work.
    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        if let win = webView.window {
            alert.beginSheetModal(for: win) { _ in completionHandler() }
        } else { alert.runModal(); completionHandler() }
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        if let win = webView.window {
            alert.beginSheetModal(for: win) { resp in
                completionHandler(resp == .alertFirstButtonReturn)
            }
        } else {
            completionHandler(alert.runModal() == .alertFirstButtonReturn)
        }
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        let finish: (NSApplication.ModalResponse) -> Void = { resp in
            completionHandler(resp == .alertFirstButtonReturn ? field.stringValue : nil)
        }
        if let win = webView.window {
            alert.beginSheetModal(for: win, completionHandler: finish)
        } else { finish(alert.runModal()) }
    }

    // A code-built Cocoa app installs no menu by default, which leaves the
    // standard Cmd-C/V/X/A/Z (and Cmd-Q) shortcuts dead everywhere, including
    // inside the webview. Wire up minimal App + Edit menus so they work.
    private func installMainMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "Quit myOS",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editItem.submenu = editMenu
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        NSApp.mainMenu = mainMenu
    }

    private func startServerAndLoad() {
        let repo = repoPath()

        if let script = Bundle.main.path(forResource: "start-server", ofType: "sh") {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/bash")
            proc.arguments = [script, repo, String(port)]
            let out = Pipe()
            proc.standardOutput = out
            proc.standardError = Pipe()
            try? proc.run()
            proc.waitUntilExit()
            let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            if let r = text.range(of: "PID ") {
                let rest = text[r.upperBound...].prefix { $0.isNumber }
                serverPID = Int32(rest) ?? 0
            }
        }

        // Poll the server for up to 12s (the indexer runs before it serves).
        var up = false
        for _ in 0..<60 {
            if ping(repo) { up = true; break }
            Thread.sleep(forTimeInterval: 0.2)
        }

        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if up {
                self.webView.load(URLRequest(url: self.loginURL(repo)))
            } else {
                self.showFailure(repo)
            }
        }
    }

    // The server only admits a browser that presents its per-run token once
    // (then a cookie). It writes the token to an owner-only file for this app.
    private func loginURL(_ repo: String) -> URL {
        let token = readToken(repo)
        return URL(string: "http://127.0.0.1:\(port)/?token=\(token)") ?? url
    }

    private func readToken(_ repo: String) -> String {
        let file = (repo as NSString).appendingPathComponent("dashboard/.auth-token")
        return (try? String(contentsOfFile: file, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    // Up = the listener proves it holds this checkout's token: it must return
    // HMAC-SHA256(token, nonce) for a fresh nonce. Anything else on the port
    // never receives the tokenized login URL.
    private func ping(_ repo: String) -> Bool {
        let token = readToken(repo)
        if token.isEmpty { return false }
        let nonce = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        let key = SymmetricKey(data: Data(token.utf8))
        let want = HMAC<SHA256>.authenticationCode(for: Data("myos-healthz:\(nonce)".utf8), using: key)
            .map { String(format: "%02x", $0) }.joined()
        guard let probe = URL(string: "\(url.absoluteString)/healthz?nonce=\(nonce)") else { return false }
        var req = URLRequest(url: probe)
        req.timeoutInterval = 1
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        let task = URLSession.shared.dataTask(with: req) { data, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200,
               let data,
               let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let proof = body["proof"] as? String {
                ok = proof == want
            }
            sem.signal()
        }
        task.resume()
        _ = sem.wait(timeout: .now() + 1.5)
        return ok
    }

    // The bundle lives at <repo>/myOS.app, so the repo is its parent directory.
    private func repoPath() -> String {
        (Bundle.main.bundlePath as NSString).deletingLastPathComponent
    }

    private func showFailure(_ repo: String) {
        let alert = NSAlert()
        alert.messageText = "myOS failed to start"
        alert.informativeText = "The dashboard server did not respond. Check \(repo)/dashboard/server.log for details."
        alert.runModal()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_: Notification) {
        if serverPID > 0 { kill(serverPID, SIGTERM) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_: NSApplication) -> Bool { true }

    private var splashHTML: String {
        """
        <html><body style="margin:0;background:#16181c;color:#9aa4af;\
        font:14px -apple-system,system-ui;display:flex;align-items:center;\
        justify-content:center;height:100vh">Starting myOS…</body></html>
        """
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
