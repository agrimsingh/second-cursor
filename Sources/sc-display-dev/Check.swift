import AppKit
import SCDisplay
import SCShared

final class Checks {
    private(set) var failures = 0

    func check(_ label: String, _ ok: Bool, _ detail: @autoclosure () -> String = "") {
        print(ok ? "PASS \(label)" : "FAIL \(label): \(detail())")
        if !ok { failures += 1 }
    }

    /// Runs `body` and checks it left the cursor, frontmost app and active Space alone. Skipped
    /// when real user input happened meanwhile, since that legitimately changes all three.
    func userUntouched<T>(_ phase: String, _ body: () async throws -> T) async rethrows -> T {
        let before = userState()
        let started = Date()
        let result = try await body()
        let after = userState()
        if secondsSinceUserInput() < Date().timeIntervalSince(started) {
            print("SKIP \(phase) left user state alone: user input during the phase")
        } else {
            check("\(phase) left cursor, frontmost app and active Space alone", after == before, "\(before) -> \(after)")
        }
        return result
    }
}

/// Full lifecycle against a scratch TextEdit: create, move, re-move, return, move, teardown.
func check() async -> Bool {
    let checks = Checks()
    guard AXIsProcessTrusted() else {
        print("FAIL this process (or the terminal hosting it) needs Accessibility permission")
        return false
    }
    let baseline = activeDisplays()
    let startSpace = CGSGetActiveSpace(CGSMainConnectionID())
    let agent = AgentDisplay()
    checks.check("starts inactive", !(await agent.status()).isActive)

    let status: AgentDisplayStatus
    do {
        let started = Date()
        status = try await checks.userUntouched("creating the display") { try await agent.ensureDisplay() }
        print(String(format: "     display created in %.2fs", Date().timeIntervalSince(started)))
    } catch {
        checks.check("ensureDisplay succeeds", false, error.localizedDescription)
        return false
    }
    printStatus(status)
    guard let displayID = status.displayID, let bounds = status.bounds?.cgRect else {
        checks.check("ensureDisplay reports an id and bounds", false, "\(status)")
        return false
    }
    let displays = activeDisplays()
    let main = CGDisplayBounds(CGMainDisplayID())
    checks.check("display count went up by one", displays.count == baseline.count + 1, "\(baseline.count) -> \(displays.count)")
    checks.check("reported bounds match the window server", displays[displayID] == bounds, "\(displays[displayID].map(describe) ?? "missing") vs \(describe(bounds))")
    checks.check("display is 1440x900 points", bounds.size == CGSize(width: 1440, height: 900), describe(bounds))
    checks.check("top-left corner sits on the main display's bottom-right corner", bounds.origin == CGPoint(x: main.maxX, y: main.maxY), "\(describe(bounds)) vs main \(describe(main))")
    let agentSpace = currentSpace(of: displayID)
    checks.check("display has its own Space", agentSpace != nil && agentSpace != startSpace, "\(agentSpace.map(String.init) ?? "none")")
    checks.check("status reports the cursor clamp enabled", status.cursorClampEnabled)
    checks.check("clamp's pointer-motion event tap is installed and enabled", clampTapInstalled())
    let again = try? await agent.ensureDisplay()
    checks.check("ensureDisplay is idempotent", again?.displayID == displayID, "\(again.map { "\($0)" } ?? "threw")")

    let scratch: (pid: pid_t, window: CGWindowID)
    do {
        scratch = try await launchScratchTextEdit("check-\(getpid()).txt", text: "agent display check")
    } catch {
        checks.check("scratch TextEdit window appeared", false, error.localizedDescription)
        await agent.teardown()
        return false
    }
    defer { kill(scratch.pid, SIGKILL) }
    let (pid, wid) = scratch
    guard let initial = await settledWindow(wid) else {
        checks.check("scratch window is readable", false)
        await agent.teardown()
        return false
    }
    print("     scratch window \(wid) pid \(pid): \(describe(initial))")
    var original = initial.frame

    do {
        let started = Date()
        let placement = try await checks.userUntouched("moving the window") { try await agent.moveToAgentDisplay(windowID: wid, pid: pid) }
        print(String(format: "     moved in %.2fs: agent frame %@", Date().timeIntervalSince(started), describe(placement.agentFrame.cgRect)))
        let hosted = windowFacts(wid)
        checks.check("window origin is inside the display bounds", hosted.map { originInside($0.frame, bounds) } ?? false, describe(hosted))
        checks.check("window is on screen there", hosted?.onScreen == true, describe(hosted))
        checks.check("window is on the display's Space", agentSpace.map { hosted?.spaces.contains($0) == true } ?? false, describe(hosted))
        // TextEdit autosaves window frames, so a scratch window can open on the agent display
        // itself; the original frame then falls back to the main display.
        if originInside(initial.frame, bounds) {
            checks.check("window opened on the agent display gets an original frame on the main display", originInside(placement.originalFrame.cgRect, main), "\(placement.originalFrame)")
        } else {
            checks.check("placement reports the original frame", placement.originalFrame.cgRect == initial.frame, "\(placement.originalFrame) vs \(describe(initial.frame))")
        }
        original = placement.originalFrame.cgRect
        checks.check("status lists the hosted window", (await agent.status()).hostedWindowIDs == [wid])
        let repeated = try await agent.moveToAgentDisplay(windowID: wid, pid: pid)
        checks.check("moving a hosted window again keeps its original frame", repeated.originalFrame == placement.originalFrame, "\(repeated)")

        let second = try await launchScratchTextEdit("check-\(getpid())-2.txt", text: "second window")
        defer { kill(second.pid, SIGKILL) }
        _ = await settledWindow(second.window)
        let cascaded = try await agent.moveToAgentDisplay(windowID: second.window, pid: second.pid)
        let offset = CGPoint(x: cascaded.agentFrame.x - placement.agentFrame.x, y: cascaded.agentFrame.y - placement.agentFrame.y)
        checks.check("second hosted window cascades 30pt", offset == CGPoint(x: 30, y: 30), "offset \(offset)")
        checks.check("status lists both hosted windows", (await agent.status()).hostedWindowIDs == [wid, second.window].sorted())
        try await agent.returnWindow(windowID: second.window)

        try await checks.userUntouched("returning the window") { try await agent.returnWindow(windowID: wid) }
        let returned = windowFacts(wid)
        print("     returned: \(describe(returned)) (active Space \(CGSGetActiveSpace(CGSMainConnectionID())))")
        checks.check("returnWindow moved it off the display", returned.map { !originInside($0.frame, bounds) } ?? false, describe(returned))
        checks.check("returnWindow restored the original frame", sameOrigin(returned?.frame, original) && returned?.frame.size == original.size, "\(describe(returned)) vs \(describe(original))")
        checks.check("status no longer lists it", (await agent.status()).hostedWindowIDs.isEmpty)

        _ = try await agent.moveToAgentDisplay(windowID: wid, pid: pid)
    } catch {
        checks.check("move/return round trip", false, error.localizedDescription)
    }

    await checks.userUntouched("tearing down") { await agent.teardown() }
    let after = windowFacts(wid)
    checks.check("teardown returned the hosted window to its original frame", sameOrigin(after?.frame, original), "\(describe(after)) vs \(describe(original))")
    checks.check("teardown removed the display", activeDisplays().count == baseline.count, "\(activeDisplays().count) displays, baseline \(baseline.count)")
    checks.check("clamp's event tap is gone", !clampTapInstalled())
    checks.check("status is inactive after teardown", !(await agent.status()).isActive)

    print(checks.failures == 0 ? "all checks passed" : "\(checks.failures) check(s) failed")
    return checks.failures == 0
}
