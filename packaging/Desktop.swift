import AppKit
import WebKit
import UniformTypeIdentifiers

// 仅负责原生窗口和应用生命周期；所有业务仍由包内原Python入口处理。
@MainActor
final class Desktop: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate,
                     WKUIDelegate, WKScriptMessageHandlerWithReply {
    let backend = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/ChatPickService")
    let port: Int
    let dataDirectory: URL
    let isolatedTest: Bool
    var window: NSWindow!
    var webView: WKWebView!
    var statusItem: NSStatusItem!
    var statusLine: NSMenuItem!
    var service: Process?
    var timer: Timer?
    var ready = false
    var starting = false
    var terminating = false
    var origin: URL { URL(string: "http://127.0.0.1:\(port)/")! }
    var scopeArguments: [String] { ["--port", String(port), "--data-dir", dataDirectory.path] }

    init(port: Int, directory: URL, test: Bool) {
        self.port = port
        dataDirectory = directory.standardizedFileURL.resolvingSymlinksInPath()
        isolatedTest = test
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if Bundle.main.bundlePath.hasPrefix("/Volumes/") {
            let alert = NSAlert()
            alert.messageText = "请先安装拾聊"
            alert.informativeText = "将拾聊拖入“应用程序”，再从“应用程序”打开。"
            alert.runModal()
            NSApp.terminate(nil)
            return
        }
        makeMenus()
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "chatpick")
        let bridge = """
        Object.defineProperty(window, 'chatpickCopyText', {value: text =>
          window.webkit.messageHandlers.chatpick.postMessage({action:'copy',text:String(text)})});
        """
        configuration.userContentController.addUserScript(WKUserScript(source: bridge, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        let area = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1320, height: 900)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: min(1240, area.width - 60), height: min(820, area.height - 60)),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "拾聊 ChatPick"
        window.minSize = NSSize(width: 940, height: 620)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentView = webView
        window.center()
        if !isolatedTest { window.setFrameAutosaveName("ChatPickMainWindow") }
        webView.loadHTMLString("<meta charset='utf-8'><p style='font:17px -apple-system;padding:48px'>正在启动拾聊…</p>", baseURL: nil)
        showWindow()
        startService()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            DispatchQueue.main.async { self?.refreshStatus() }
        }
    }

    func makeMenus() {
        let main = NSMenu()
        let application = NSMenu()
        let appItem = NSMenuItem()
        appItem.submenu = application
        main.addItem(appItem)
        application.addItem(withTitle: "关于拾聊", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        application.addItem(.separator())
        application.addItem(withTitle: "隐藏拾聊", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        application.addItem(.separator())
        application.addItem(withTitle: "退出拾聊", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let file = NSMenu(title: "文件")
        file.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let fileItem = NSMenuItem(title: "文件", action: nil, keyEquivalent: "")
        fileItem.submenu = file
        main.addItem(fileItem)
        let edit = NSMenu(title: "编辑")
        for (title, action, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"),
                                     ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        editItem.submenu = edit
        main.addItem(editItem)
        let view = NSMenu(title: "显示")
        let reopen = view.addItem(withTitle: "打开拾聊", action: #selector(openWindow), keyEquivalent: "0")
        reopen.target = self
        let reload = view.addItem(withTitle: "刷新页面", action: #selector(reloadPage), keyEquivalent: "r")
        reload.target = self
        let viewItem = NSMenuItem(title: "显示", action: nil, keyEquivalent: "")
        viewItem.submenu = view
        main.addItem(viewItem)
        NSApp.mainMenu = main
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "bubble.left.and.bubble.right", accessibilityDescription: "拾聊")
        statusItem.button?.image?.isTemplate = true
        statusItem.button?.toolTip = "拾聊 ChatPick"
        let menu = NSMenu()
        statusLine = menu.addItem(withTitle: "正在启动…", action: nil, keyEquivalent: "")
        let open = menu.addItem(withTitle: "打开拾聊", action: #selector(openWindow), keyEquivalent: "")
        open.target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出拾聊", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        statusItem.menu = menu
    }

    func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func openWindow() {
        showWindow()
        if !ready && !starting { startService() }
    }

    @objc func reloadPage() {
        if ready { webView.reload() } else if !starting { startService() }
    }

    func runBackend(_ arguments: [String], completion: @escaping (Bool, String) -> Void) {
        let executable = backend
        DispatchQueue.global(qos: .userInitiated).async {
            let task = Process()
            task.executableURL = executable
            task.arguments = arguments
            let pipe = Pipe()
            task.standardOutput = pipe
            task.standardError = pipe
            do {
                try task.run()
                let output = pipe.fileHandleForReading.readDataToEndOfFile()
                task.waitUntilExit()
                let text = String(data: output, encoding: .utf8) ?? "本机服务操作未完成。"
                DispatchQueue.main.async { completion(task.terminationStatus == 0, text) }
            } catch {
                DispatchQueue.main.async { completion(false, "无法启动包内服务，请重新安装拾聊。") }
            }
        }
    }

    func startService() {
        guard !starting else { return }
        starting = true
        statusLine.title = "正在启动…"
        if isolatedTest {
            do {
                try FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700])
                let log = dataDirectory.appendingPathComponent("desktop-test.log")
                FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600])
                let task = Process()
                task.executableURL = backend
                task.arguments = ["--serve"] + scopeArguments
                task.currentDirectoryURL = Bundle.main.resourceURL
                task.standardOutput = try FileHandle(forWritingTo: log)
                task.standardError = task.standardOutput
                var environment = ProcessInfo.processInfo.environment
                environment.removeValue(forKey: "CHATPICK_BACKGROUND")
                environment.removeValue(forKey: "CHATPICK_DATA_DIR")
                task.environment = environment
                try task.run()
                service = task
                waitForService(attempt: 0)
            } catch { startFailed("隔离测试服务未能启动。") }
        } else {
            runBackend(["--no-open"] + scopeArguments) { [weak self] success, text in
                guard let self = self else { return }
                if success { self.waitForService(attempt: 0) } else { self.startFailed(text) }
            }
        }
    }

    func serviceStatus(_ completion: @escaping ([String: Any]?) -> Void) {
        var request = URLRequest(url: origin.appendingPathComponent("api/background"))
        request.timeoutInterval = 2
        request.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            let value = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async {
                guard let self = self else { return }
                completion(value?["app"] as? String == "GroupPulse" && value?["data_dir"] as? String == self.dataDirectory.path ? value : nil)
            }
        }.resume()
    }

    func waitForService(attempt: Int) {
        serviceStatus { [weak self] state in
            guard let self = self, !self.terminating else { return }
            if state != nil {
                self.ready = true
                self.starting = false
                self.statusLine.title = "后台运行中"
                self.webView.load(URLRequest(url: self.origin))
            } else if attempt < 80 && (self.service?.isRunning ?? true) {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitForService(attempt: attempt + 1) }
            } else { self.startFailed("本机服务未能就绪。可以从菜单栏重新打开拾聊；配置和历史保留。") }
        }
    }

    func refreshStatus() {
        guard !starting && !terminating else { return }
        serviceStatus { [weak self] state in
            self?.ready = state != nil
            self?.statusLine.title = state == nil ? "后台未运行" : (state?["generating"] as? Bool == true ? "正在生成…" : "后台运行中")
        }
    }

    func startFailed(_ text: String) {
        starting = false
        ready = false
        statusLine.title = "后台未运行"
        let alert = NSAlert()
        alert.messageText = "拾聊未能启动"
        alert.informativeText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        alert.beginSheetModal(for: window)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openWindow()
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if window == nil { return .terminateNow }
        if terminating { return .terminateLater }
        terminating = true
        timer?.invalidate()
        if isolatedTest {
            if let task = service, task.isRunning {
                task.terminate()
                DispatchQueue.global().async {
                    task.waitUntilExit()
                    DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
                }
                return .terminateLater
            }
            return .terminateNow
        }
        runBackend(["--background", "stop"] + scopeArguments) { [weak self] success, text in
            if !success { self?.terminating = false; self?.startFailed(text) }
            NSApp.reply(toApplicationShouldTerminate: success)
        }
        return .terminateLater
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if url.absoluteString == "about:blank" || (url.scheme == "http" && url.host == "127.0.0.1" && url.port == port) {
            decisionHandler(.allow)
        } else {
            if ["http", "https"].contains(url.scheme ?? "") && (action.navigationType == .linkActivated || action.targetFrame == nil) {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowedContentTypes = [.json]
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { result in completionHandler(result == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, ["http", "https"].contains(url.scheme ?? "") {
            if url.scheme == "http" && url.host == "127.0.0.1" && url.port == port {
                webView.load(action.request)
            } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { result in completionHandler(result == .alertFirstButtonReturn) }
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        let source = message.frameInfo.securityOrigin
        guard message.frameInfo.isMainFrame, source.protocol == "http", source.host == "127.0.0.1", source.port == port,
              let body = message.body as? [String: Any], body["action"] as? String == "copy", let text = body["text"] as? String else {
            replyHandler(nil, "仅允许拾聊本机页面复制文本。")
            return
        }
        NSPasteboard.general.clearContents()
        if NSPasteboard.general.setString(text, forType: .string) { replyHandler(true, nil) }
        else { replyHandler(nil, "复制未完成，请再试一次。") }
    }
}

@main
struct DesktopMain {
@MainActor static func main() {
let arguments = Array(CommandLine.arguments.dropFirst())
if let first = arguments.first, ["--serve", "--background", "--self-check"].contains(first) {
    let backend = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/ChatPickService").path
    let values = ([backend] + arguments).map { strdup($0) }
    values.withUnsafeBufferPointer { pointers in
        var argv = Array(pointers) + [nil]
        execv(backend, &argv)
    }
    fputs("无法启动包内服务。\n", stderr)
    exit(1)
}
var port = 8765
var directory = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/ChatPick")
var test = false
var explicitDirectory = false
var index = 0
while index < arguments.count {
    switch arguments[index] {
    case "--port":
        index += 1
        guard index < arguments.count, let value = Int(arguments[index]), (1024...65535).contains(value) else { exit(2) }
        port = value
    case "--data-dir":
        index += 1
        guard index < arguments.count else { exit(2) }
        directory = URL(fileURLWithPath: arguments[index], isDirectory: true)
        explicitDirectory = true
    case "--desktop-test": test = true
    default: exit(2)
    }
    index += 1
}
if test && (!explicitDirectory || port == 8765) { exit(2) }
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let desktop = Desktop(port: port, directory: directory, test: test)
app.delegate = desktop
app.run()
}
}
