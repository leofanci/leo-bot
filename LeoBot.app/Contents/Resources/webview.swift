import Cocoa
import CryptoKit
import WebKit

// Leo Bot — the bundle's main executable is THIS compiled Cocoa app (a real
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
    // Retry timing: ping every retryInterval; after startGrace of silence (and no
    // update running) start the server once; give up after totalWait.
    let retryInterval: TimeInterval = 2.0
    let startGrace: TimeInterval = 30.0
    let totalWait: TimeInterval = 90.0
    var url: URL { URL(string: "http://127.0.0.1:\(port)")! }

    func applicationDidFinishLaunching(_: Notification) {
        installMainMenu()

        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: .zero, configuration: cfg)
        // Without a uiDelegate, WKWebView silently ignores <input type=file>
        // clicks — the chat's attach button looks dead. See runOpenPanel below.
        webView.uiDelegate = self
        // The window only ever shows Leo Bot itself: any other web address opens in the
        // default browser, so a link in a bot's text never loads inside the app window.
        webView.navigationDelegate = self

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Leo Bot"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden          // hide "Leo Bot" text from title bar
        window.isMovableByWindowBackground = true  // drag anywhere on the window
        window.contentView = webView
        window.setFrameAutosaveName("LeoBotWindow")
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
        // The window itself never navigates to a data: or blob: page (a link in a bot's text could
        // carry its own HTML); frames inside Leo Bot pages may still use them.
        let inMainFrame = action.targetFrame?.isMainFrame ?? true
        if target.absoluteString == "about:blank" || ((scheme == "blob" || scheme == "data") && !inMainFrame) {
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
        appMenu.addItem(withTitle: "Quit Leo Bot",
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

    // Starts (or finds) the server, waits for it, loads the dashboard, then keeps
    // watching: a restart (an update, a crash) is ridden out instead of shown as a failure.
    private func startServerAndLoad() {
        let repo = repoPath()
        if !isUpdating() { runStartScript(repo) }

        // Retry every retryInterval for up to totalWait (the indexer runs before the server serves).
        if !waitUp(repo, startAfter: startGrace) {
            DispatchQueue.main.async { [weak self] in self?.showFailure() }
            return
        }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.webView.load(URLRequest(url: self.loginURL(repo)))
        }
        watch(repo)
    }

    private func runStartScript(_ repo: String) {
        serverPID = 0
        guard let script = Bundle.main.path(forResource: "start-server", ofType: "sh") else { return }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [script, repo, String(port)]
        var env = ProcessInfo.processInfo.environment
        env["LEOBOT_HOME"] = homePath()
        proc.environment = env
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

    // True once a ping succeeds, polling every retryInterval for up to totalWait.
    // If nothing answers for startGrace, start the server once, but never while the
    // updater holds its lock (it stops, backs up and restarts the server itself).
    private func waitUp(_ repo: String, startAfter: TimeInterval) -> Bool {
        let begin = Date()
        var started = false
        while true {
            if ping(repo) { return true }
            let elapsed = Date().timeIntervalSince(begin)
            if elapsed >= totalWait { return false }
            if !started && elapsed >= startAfter && !isUpdating() {
                started = true
                runStartScript(repo)
            }
            Thread.sleep(forTimeInterval: retryInterval)
        }
    }

    // True while core/updater.py holds run/update.lock (it takes flock LOCK_EX on it).
    private func isUpdating() -> Bool {
        let path = (homePath() as NSString).appendingPathComponent("run/update.lock")
        let fd = open(path, O_RDONLY)
        if fd < 0 { return false }
        defer { close(fd) }
        if flock(fd, LOCK_SH | LOCK_NB) != 0 { return true }
        flock(fd, LOCK_UN)
        return false
    }

    // While the app is open: when the server stops answering (an update restarts it),
    // only poll; start it ourselves after startGrace of silence with no update running.
    // Once it is back, reload the dashboard.
    private func watch(_ repo: String) {
        while true {
            Thread.sleep(forTimeInterval: retryInterval)
            if ping(repo) { continue }
            if !waitUp(repo, startAfter: startGrace) {
                DispatchQueue.main.async { [weak self] in self?.showFailure() }
                return
            }
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.webView.load(URLRequest(url: self.loginURL(repo)))
            }
        }
    }

    // The server only admits a browser that presents its per-run token once
    // (then a cookie). It writes the token to an owner-only file for this app.
    private func loginURL(_ repo: String) -> URL {
        let token = readToken(repo)
        return URL(string: "http://127.0.0.1:\(port)/?token=\(token)") ?? url
    }

    // Refuses a token file that is a symlink, not ours, or readable by group/other.
    private func readToken(_ repo: String) -> String {
        let file = (homePath() as NSString).appendingPathComponent("run/auth-token")
        var st = stat()
        if lstat(file, &st) != 0 { return "" }
        if (st.st_mode & S_IFMT) != S_IFREG { return "" }
        if st.st_uid != getuid() { return "" }
        if (st.st_mode & 0o077) != 0 { return "" }
        return (try? String(contentsOfFile: file, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    // Up = the listener proves it holds this home's token: it must return
    // HMAC-SHA256(token, nonce) for a fresh nonce. Anything else on the port
    // never receives the tokenized login URL.
    private func ping(_ repo: String) -> Bool {
        let token = readToken(repo)
        if token.isEmpty { return false }
        let nonce = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        let key = SymmetricKey(data: Data(token.utf8))
        let want = HMAC<SHA256>.authenticationCode(for: Data("leobot-healthz:\(nonce)".utf8), using: key)
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

    // The home folder: $LEOBOT_HOME, else ~/.leobot (token, pid lock and log live here).
    private func homePath() -> String {
        if let h = ProcessInfo.processInfo.environment["LEOBOT_HOME"], !h.isEmpty { return h }
        // Finder/Dock launches carry no environment: build-app.sh --out records a custom home here.
        if let f = Bundle.main.path(forResource: "leobot-home", ofType: nil),
           let h = (try? String(contentsOfFile: f, encoding: .utf8))?
               .trimmingCharacters(in: .whitespacesAndNewlines), !h.isEmpty { return h }
        return (NSHomeDirectory() as NSString).appendingPathComponent(".leobot")
    }

    // A development clone (the bundle sits next to dashboard/server.py) runs its own
    // code; an installed app runs the code in $LEOBOT_HOME/app, else ~/.leobot/app.
    private func repoPath() -> String {
        let parent = (Bundle.main.bundlePath as NSString).deletingLastPathComponent
        let marker = (parent as NSString).appendingPathComponent("dashboard/server.py")
        if FileManager.default.fileExists(atPath: marker) { return parent }
        if ProcessInfo.processInfo.environment["LEOBOT_HOME"] != nil || Bundle.main.path(forResource: "leobot-home", ofType: nil) != nil {
            return (homePath() as NSString).appendingPathComponent("app")
        }
        return (NSHomeDirectory() as NSString).appendingPathComponent(".leobot/app")
    }

    private func showFailure() {
        let log = (homePath() as NSString).appendingPathComponent("logs/server.log")
        let alert = NSAlert()
        alert.messageText = "Leo Bot failed to start"
        alert.informativeText = "The dashboard server did not respond. Check \(log) for details."
        alert.runModal()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_: Notification) {
        // Only a pid from this run that is still a dashboard/server.py process.
        if serverPID > 0 && isLeoBotServer(serverPID) { kill(serverPID, SIGTERM) }
    }

    private func isLeoBotServer(_ pid: Int32) -> Bool {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/ps")
        proc.arguments = ["-p", String(pid), "-o", "command="]
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = Pipe()
        guard (try? proc.run()) != nil else { return false }
        let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        proc.waitUntilExit()
        return text.contains("dashboard/server.py")
    }

    func applicationShouldTerminateAfterLastWindowClosed(_: NSApplication) -> Bool { true }

    private var splashHTML: String {
        """
        <html><body style="margin:0;background:#16181c;color:#9aa4af;\
        font:14px -apple-system,system-ui;display:flex;align-items:center;\
        justify-content:center;height:100vh">Starting Leo Bot…</body></html>
        """
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
