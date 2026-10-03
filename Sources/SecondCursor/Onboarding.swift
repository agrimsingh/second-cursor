import AppKit
import SCShared

/// Floating panel that walks the user through Accessibility and Screen Recording, one at a time. The app
/// icon is a drag source carrying the bundle's file URL, so it can be dropped straight into the list in
/// System Settings. Status is polled every second; the panel advances when a permission flips on and closes
/// once both are granted.
@MainActor
final class OnboardingPanel: NSObject, NSWindowDelegate {
    /// Posted by `SecondCursor --show-onboarding` when this instance is already running. The object is the
    /// instance's support directory, so a test instance (SC_HOME) and the user's app never answer each other.
    nonisolated static let requestName = Notification.Name("dev.secondcursor.show-onboarding")

    enum Step: CaseIterable {
        case accessibility, screenRecording

        var name: String {
            switch self {
            case .accessibility: "Accessibility"
            case .screenRecording: "Screen Recording"
            }
        }

        var isGranted: Bool {
            switch self {
            case .accessibility: Permissions.accessibility
            case .screenRecording: Permissions.screenRecording
            }
        }

        var reason: String {
            switch self {
            case .accessibility:
                "Second Cursor reads and operates app windows through Accessibility, in the background, without moving your cursor."
            case .screenRecording:
                "Second Cursor takes screenshots of single windows so your agent can see what it is working on."
            }
        }

        var hint: String {
            switch self {
            case .accessibility:
                "Drag the icon into the list in System Settings, or switch Second Cursor on if it is already there."
            case .screenRecording:
                "Drag the icon into the list in System Settings, or switch Second Cursor on. If it stays off, choose Quit & Reopen when System Settings asks."
            }
        }
    }

    private var panel: NSPanel?
    private var timer: Timer?
    private var step: Step = .accessibility
    private var requestedStep: Step?
    private var sawMissing = false
    private var completionWorkItem: DispatchWorkItem?
    private var settingsFrame: CGRect?
    private let titleLabel = NSTextField(labelWithString: "")
    private let reasonLabel = NSTextField(wrappingLabelWithString: "")
    private let hintLabel = NSTextField(wrappingLabelWithString: "")
    private let openButton = NSButton(title: "Open System Settings", target: nil, action: nil)
    private var rows: [Step: (icon: NSImageView, label: NSTextField)] = [:]

    static var needed: Bool {
        !Step.allCases.allSatisfy(\.isGranted)
    }

    func listenForRequests() {
        DistributedNotificationCenter.default().addObserver(
            forName: Self.requestName, object: SCPaths.supportDir.path, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.show() }
        }
    }

    nonisolated static func requestFromRunningInstance() {
        DistributedNotificationCenter.default().postNotificationName(
            requestName, object: SCPaths.supportDir.path, userInfo: nil, deliverImmediately: true)
    }

    func show(step requestedStep: Step? = nil) {
        let panel = self.panel ?? makePanel()
        self.panel = panel
        completionWorkItem?.cancel()
        completionWorkItem = nil
        self.requestedStep = requestedStep
        sawMissing = false
        step = requestedStep ?? Step.allCases.first { !$0.isGranted } ?? .accessibility
        refresh()
        settingsFrame = Self.settingsWindowFrame()
        position(panel, next: settingsFrame)
        panel.orderFrontRegardless()
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
    }

    func windowWillClose(_ notification: Notification) {
        timer?.invalidate()
        timer = nil
        completionWorkItem?.cancel()
        completionWorkItem = nil
    }

    private func tick() {
        refresh()
        let frame = Self.settingsWindowFrame()
        if frame != settingsFrame, let panel {
            settingsFrame = frame
            position(panel, next: frame)
        }
    }

    private func refresh() {
        let missing = Step.allCases.filter { !$0.isGranted }
        let requestedMissing = requestedStep.map { !$0.isGranted } ?? !missing.isEmpty
        if requestedMissing {
            sawMissing = true
        }
        if requestedStep == nil, step.isGranted, let next = missing.first {
            step = next
        }
        for (candidate, row) in rows {
            let granted = !missing.contains(candidate)
            row.icon.image = NSImage(
                systemSymbolName: granted ? "checkmark.circle.fill" : "circle",
                accessibilityDescription: granted ? "granted" : "not granted")
            row.icon.contentTintColor = granted ? .systemGreen : .tertiaryLabelColor
            row.label.font = candidate == step ? .boldSystemFont(ofSize: 12) : .systemFont(ofSize: 12)
        }
        if !requestedMissing && sawMissing {
            titleLabel.stringValue = requestedStep.map { "\($0.name) is on" } ?? "Permissions are ready"
            reasonLabel.stringValue = requestedStep == nil
                ? "Both permissions are on. Return to Setup to check your agent and browser connection."
                : "The permission is on. Return to Setup to finish connecting Second Cursor."
            hintLabel.stringValue = ""
            timer?.invalidate()
            timer = nil
            if completionWorkItem == nil {
                let work = DispatchWorkItem { [weak self] in self?.panel?.close() }
                completionWorkItem = work
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5, execute: work)
            }
            fitKeepingTop()
            return
        }
        titleLabel.stringValue = step.isGranted ? "\(step.name) is on" : "Allow \(step.name)"
        reasonLabel.stringValue = step.reason
        hintLabel.stringValue = step.hint
        fitKeepingTop()
    }

    private func fitKeepingTop() {
        guard let panel, let content = panel.contentView else { return }
        var frame = panel.frameRect(forContentRect: NSRect(origin: .zero, size: content.fittingSize))
        guard frame.size != panel.frame.size else { return }
        frame.origin = CGPoint(x: panel.frame.minX, y: panel.frame.maxY - frame.height)
        panel.setFrame(frame, display: true)
    }

    @objc private func openSettings() {
        Permissions.openSettings(accessibility: step == .accessibility)
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 440, height: 240),
            styleMask: [.titled, .closable, .utilityWindow, .nonactivatingPanel],
            backing: .buffered, defer: false)
        panel.title = "Second Cursor Permissions"
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.delegate = self

        titleLabel.font = .boldSystemFont(ofSize: 15)
        reasonLabel.font = .systemFont(ofSize: 13)
        hintLabel.font = .systemFont(ofSize: 12)
        hintLabel.textColor = .secondaryLabelColor
        for label in [reasonLabel, hintLabel] {
            label.preferredMaxLayoutWidth = 300
        }
        openButton.target = self
        openButton.action = #selector(openSettings)
        openButton.bezelStyle = .rounded

        let icon = AppIconDragSource(bundleURL: Bundle.main.bundleURL)
        let text = NSStackView(views: [titleLabel, reasonLabel, hintLabel, openButton])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 8
        text.setCustomSpacing(12, after: hintLabel)
        let top = NSStackView(views: [icon, text])
        top.alignment = .top
        top.spacing = 16

        var statusViews: [NSView] = []
        for candidate in Step.allCases {
            let image = NSImageView()
            image.setContentHuggingPriority(.required, for: .horizontal)
            let label = NSTextField(labelWithString: candidate.name)
            rows[candidate] = (image, label)
            let row = NSStackView(views: [image, label])
            row.spacing = 6
            statusViews.append(row)
        }
        let status = NSStackView(views: statusViews)
        status.spacing = 16

        let content = NSStackView(views: [top, status])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 16
        content.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        panel.contentView = content
        return panel
    }

    /// Next to System Settings when it is open (right side, else left), otherwise centered on screen.
    private func position(_ panel: NSPanel, next settings: CGRect?) {
        let size = panel.frame.size
        let screen = settings.flatMap { frame in NSScreen.screens.first { $0.frame.intersects(frame) } } ?? NSScreen.main
        guard let visible = screen?.visibleFrame else { return }
        var origin = CGPoint(x: visible.midX - size.width / 2, y: visible.midY - size.height / 2)
        if let settings {
            let top = settings.maxY - size.height
            if settings.maxX + 12 + size.width <= visible.maxX {
                origin = CGPoint(x: settings.maxX + 12, y: top)
            } else if settings.minX - 12 - size.width >= visible.minX {
                origin = CGPoint(x: settings.minX - 12 - size.width, y: top)
            } else {
                origin = CGPoint(x: settings.midX - size.width / 2, y: settings.minY + 12)
            }
        }
        origin.x = min(max(origin.x, visible.minX), visible.maxX - size.width)
        origin.y = min(max(origin.y, visible.minY), visible.maxY - size.height)
        panel.setFrameOrigin(origin)
    }

    /// System Settings' frontmost normal window in AppKit coordinates. Window bounds need no Screen Recording.
    private static func settingsWindowFrame() -> CGRect? {
        let pids = Set(NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.systempreferences")
            .map(\.processIdentifier))
        guard !pids.isEmpty,
              let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]],
              let info = list.first(where: {
                  pids.contains(($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value ?? 0)
                      && ($0[kCGWindowLayer as String] as? Int) == 0
              }),
              let bounds = info[kCGWindowBounds as String] as? NSDictionary,
              let rect = CGRect(dictionaryRepresentation: bounds)
        else { return nil }
        let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
        return CGRect(x: rect.minX, y: primaryHeight - rect.maxY, width: rect.width, height: rect.height)
    }
}

/// The app icon, draggable as the bundle's file URL.
private final class AppIconDragSource: NSView, NSDraggingSource {
    private let bundleURL: URL
    private let icon: NSImage

    init(bundleURL: URL) {
        self.bundleURL = bundleURL
        icon = NSWorkspace.shared.icon(forFile: bundleURL.path)
        super.init(frame: NSRect(x: 0, y: 0, width: 64, height: 64))
        translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([widthAnchor.constraint(equalToConstant: 64), heightAnchor.constraint(equalToConstant: 64)])
        toolTip = "Drag into the list in System Settings"
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not used")
    }

    override var mouseDownCanMoveWindow: Bool { false }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        icon.draw(in: bounds)
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .openHand)
    }

    override func mouseDragged(with event: NSEvent) {
        let item = NSDraggingItem(pasteboardWriter: bundleURL as NSURL)
        item.setDraggingFrame(bounds, contents: icon)
        beginDraggingSession(with: [item], event: event, source: self)
    }

    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation {
        context == .outsideApplication ? [.copy, .link, .generic] : []
    }

    override func isAccessibilityElement() -> Bool { true }

    override func accessibilityRole() -> NSAccessibility.Role? { .image }

    override func accessibilityLabel() -> String? { "Second Cursor app icon" }

    override func accessibilityHelp() -> String? { "Drag into the list in System Settings to grant the permission." }
}
