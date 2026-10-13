import MCP

enum ToolCatalog {
    /// State-changing tools; each call is written to the action log before it runs.
    /// Mirrors ACTION_TOOLS in e2e/run.py. `agent_display` create/teardown is logged too.
    static let actionTools: Set<String> = [
        "paste", "select_text", "launch_app", "batch", "click", "drag", "type_text", "set_value", "press_key", "scroll",
        "perform_action",
        "move_to_agent_display", "return_window", "browser_open", "browser_navigate", "browser_click",
        "browser_type", "browser_press_key", "browser_scroll", "browser_close",
    ]

    /// Tools that take a `window_id` and must refuse windows owned by blocked apps.
    static let windowScopedTools: Set<String> = [
        "inspect_element", "paste", "select_text", "get_window_state", "batch", "click", "drag", "type_text",
        "set_value", "press_key", "scroll", "perform_action",
        "screenshot", "move_to_agent_display", "return_window",
        "find_elements", "wait_for",
    ]

    /// Native actions that return an observation of their window (see `observe`).
    static let observingTools: Set<String> = [
        "click", "drag", "type_text", "set_value", "press_key", "scroll", "perform_action", "select_text", "paste",
    ]

    /// Browser actions that return an observation of their tab (see `observe`).
    static let observingBrowserTools: Set<String> = [
        "browser_click", "browser_type", "browser_press_key", "browser_scroll", "browser_navigate",
    ]

    /// Browser inputs that can check a postcondition before input and wait for it afterward.
    static let expectingBrowserTools: Set<String> = [
        "browser_click", "browser_type", "browser_press_key", "browser_scroll",
    ]

    /// Actions that take a `caption` for the agent cursor.
    static let captionedTools: Set<String> = observingTools.union(observingBrowserTools).union([
        "batch", "browser_command",
    ])

    /// Every callable tool. `act` is `batch` under another name and description, sharing its schema so that
    /// batch's parameters, present and future, reach it; the router runs it as batch.
    static let tools: [Tool] = catalog.flatMap { tool in
        guard tool.name == "batch" else { return [tool] }
        return [tool, Tool(name: "act", description: actDescription, inputSchema: tool.inputSchema, annotations: tool.annotations)]
    }

    /// Listed by `toolSurface: "compact"`, for clients with tool-count limits or weaker models: act in place of batch
    /// and the single-action tools, browser_repl for web flows, and what acting on a native window needs.
    static let compactTools: Set<String> = [
        "status", "list_windows", "launch_app", "get_window_state", "find_elements", "act", "operation_status",
        "browser_open", "browser_snapshot", "browser_repl",
    ]

    /// What tools/list shows: every tool but act (the default, or "full"), the same without browser_repl
    /// ("discrete"), `compactTools` ("compact"), or browser_repl alone ("repl"). Models offered browser_repl beside the
    /// discrete browser tools with no say on which to use mixed the two and ran slower than with either, so their
    /// descriptions divide the work: browser_repl for multi-step flows, the browser_ tools for single steps.
    /// `expand` is listed only when reads are condensed (`condenseOutlines`), since nothing is hidden otherwise.
    /// Session ownership, retry IDs and deadlines are normally managed by hosts.
    /// Outcome reconciliation stays discoverable because an uncertain result can
    /// require the model to inspect a journaled operation before choosing its next action.
    static func advertised(surface: String?, condensing: Bool = false) -> [Tool] {
        let listed: [Tool]
        switch surface {
        case "repl": listed = tools.filter { $0.name == "browser_repl" }
        case "compact": listed = tools.filter { compactTools.contains($0.name) }
        default:
            let hidden: Set<String> = surface == "discrete" ? ["act", "browser_repl"] : ["act"]
            listed = tools.filter { !hidden.contains($0.name) && !hostTools.contains($0.name) }
        }
        return listed.map { tool in
            guard case .object(var schema) = tool.inputSchema, case .object(var properties)? = schema["properties"]
            else { return tool }
            var dropped = hostParameters
            if tool.name == "operation_status" { dropped.remove("operation_id") }
            // wait_for's timeout_ms bounds its wait, and browser_repl's a script the model wrote.
            if ["wait_for", "browser_repl"].contains(tool.name) { dropped.remove("timeout_ms") }
            if !condensing { dropped.insert("expand") }
            for key in dropped { properties.removeValue(forKey: key) }
            schema["properties"] = .object(properties)
            return Tool(name: tool.name, description: tool.description, inputSchema: .object(schema), annotations: tool.annotations)
        }
    }

    static func required(_ name: String) -> [String] {
        tools.first { $0.name == name }?.inputSchema.objectValue?["required"]?.arrayValue?.compactMap(\.stringValue) ?? []
    }

    private static let hostParameters: Set<String> = ["session_id", "operation_id", "observation_id", "timeout_ms"]
    private static let hostTools: Set<String> = ["session", "bind_target", "cancel_operation"]

    private static let expand = stringArray(
        "Ids of hidden parts of a condensed read (its '… N lines hidden (expand \"r3\")' lines) to return whole, as they "
            + "were read, without reading again.")

    private static let actDescription =
        "Act on one window or agent tab: run a list of steps strictly in order, then get one observation after the UI settles. This is the way to act, with one step or many: put every step you already know in one call (pressing a known series of buttons, filling known fields, typing followed by a shortcut), and end the list before any step whose outcome you need to see first (a menu, dialog, or search results opening, or anything whose element indexes you do not know yet); then look at the observation and send the next list. Each step is a tool name (click, type_text, press_key, set_value, scroll, drag, perform_action, wait) plus that action's arguments. Element indexes come from get_window_state or find_elements and are not refreshed between steps. Pixel steps (x/y) take coordinate_space and stop the list if the window has changed size since you last observed it, because the coordinates would be stale. Each step's result reports ok, route, and error; by default the list stops at the first failure. The observation is a diff like get_window_state unless full=true. \(tabBatch)"

    private static let tabBatch =
        "With tab_id instead of window_id, the steps act in that agent tab: browser_click (ref, or x and y), browser_type (text, optional ref and submit), browser_press_key, browser_scroll and wait, with refs from the tab's latest outline (not refreshed between steps, so end the list where a step re-renders what later steps target); the observation is the tab's diff. expect checks once before the batch and waits after its last step: text, or ref with exact value for tabs (element_index with value for windows). Any failed or skipped step makes the expectation failed."

    private static let catalog: [Tool] =
        ([
            Tool(
                name: "status",
                description:
                    "Check Second Cursor's health: macOS permissions, whether the user paused the agent, whether an agent task is active (agent_activity), the agent display, and the Chrome extension. Call this first when other tools fail.",
                inputSchema: schema([:]),
                annotations: .init(readOnlyHint: true)
            ),
            Tool(
                name: "list_apps",
                description:
                    "List running apps (name, bundle_id, pid). Set include_not_running to also list installed apps. Use it to find the exact name or bundle id for launch_app.",
                inputSchema: schema([
                    "include_not_running": boolean("Also list installed apps that are not running.", default: false)
                ]),
                annotations: .init(readOnlyHint: true)
            ),
            Tool(
                name: "list_windows",
                description:
                    "List top-level windows with id, title, frame (global screen points), Space ids, and whether each is on the agent display. The window id is the handle every window tool needs.",
                inputSchema: schema(["app": string("Filter by app name or bundle id (case-insensitive substring).")]),
                annotations: .init(readOnlyHint: true)
            ),
            Tool(
                name: "launch_app",
                description:
                    "Launch an app in the background without activating it, then wait up to 10s for its window. Windows stay where the app puts them; every tool works on them in place. Use new_instance=true to get a separate process instead of sharing the user's running copy.",
                inputSchema: schema(
                    [
                        "app": string("App name, bundle id, or path to the .app."),
                        "urls": stringArray("Files or URLs to open with the app."),
                        "new_instance": boolean(
                            "Start a separate process even if the app is already running.", default: false),
                        "on_agent_display": boolean(
                            "Opt-in: move the app's new windows to the invisible agent display. While the user is in a full-screen app, a new window can open on a desktop Space they are not viewing, where macOS hides it from every tool; the note says so, and the window moves once the user shows that desktop.",
                            default: false),
                    ], required: ["app"])
            ),
            Tool(
                name: "get_window_state",
                description:
                    "Read a window: an indented accessibility outline where actionable elements carry [index] markers and window-local frames, with an optional screenshot (off by default). The first read of a window returns the whole outline; later reads return only what changed since the previous read (is_diff=true): lines starting with + are new or changed (with current frames), lines starting with - are gone, and \"(no changes)\" means the UI is the same. Elements that only moved (a scroll or resize) are not resent; a closing line \"~ N elements moved\" counts them. An element generally keeps its index while its label and place in the UI stay the same; use the latest observation after a label or structure change. Pass full=true to get the whole outline again. The outline lists up to max_elements elements; a truncated one ends with a line naming the containers left out: raise max_elements, or pass scope=<index or container label> to read everything inside one element at full depth. Full outlines end with the focused element and any selected text. A screenshot that fails to capture is reported as screenshot_error; the outline is still returned. Action tools already return this diff for their window, so call this for a first look, a full view, or after waiting. When you can see text the outline lacks (custom-drawn apps, canvases, games), read again with ocr=true; its text lines are clicked with x/y at their center.",
                inputSchema: schema(
                    [
                        "window_id": windowID,
                        "include_screenshot": boolean(
                            "Attach a PNG screenshot of the window (long edge at most 1024 px; see screenshot).",
                            default: false),
                        "include_menu_bar": boolean("Include the app's menu bar in the outline.", default: false),
                        "full": boolean(
                            "Return the whole outline instead of a diff from the previous read.", default: false),
                        "max_elements": integer(
                            "Most elements to list, 50-3000. Later reads and action observations of this window keep the last value.",
                            default: 400),
                        "scope": .object([
                            "anyOf": .array([["type": "integer"], ["type": "string"]]),
                            "description": .string(
                                "Return only one element's subtree, at full depth: its [index], or a container's label such as \"Formatting\" (exact match first, then case-insensitive). Not a diff."),
                        ]),
                        "structured": boolean(
                            "Also return an elements JSON array on full and scoped reads, for code consumers.",
                            default: false),
                        "ocr": boolean(
                            "Also recognize the text drawn in the window (on device) and merge what the outline lacks. Use it when you can see text the outline does not have: custom-drawn apps (DAWs, games), canvases, images of text. Text already in the outline is left out; text on an element is added to its line as ocr=\"...\"; other text becomes a line such as text \"Export\" (412,88 40x14), which has no index: click it with x/y at the center of its frame. Returns ocr_ms and ocr_count, or ocr_error with the outline when recognition fails. Costs a few hundred ms, more on large windows.",
                            default: false),
                        "visual_settle": boolean(
                            "For delayed custom drawing after an action: probe target-window pixels before reading (1.6 s reaction allowance, 2.2 s probe budget plus capture overhead). Returns visual_settle status; quiet pixels do not prove success. Off by default.",
                            default: false),
                        "expand": expand,
                    ]),
                annotations: .init(readOnlyHint: true)
            ),
            Tool(
                name: "find_elements",
                description:
                    "Search the window's latest observation for elements by text and/or role, instead of reading a big window's whole outline. Text matches each element's label, value and description, ignoring case: exact matches first, then prefixes, then substrings. The search covers every element of that observation, including ones a truncated outline (max_elements) left out. Returns matching outline lines with [index] and frames, like get_window_state's; use the indexes with act or the other actions. Native windows only: a page in an agent tab is read with browser_snapshot. It does not read the app again: it sees the UI as of the latest get_window_state or action observation of this window (observation_id). After the UI changed without an observation (observe=none, or time passing), call get_window_state first. A window this session has not observed yet is read once, and that read becomes its observation.",
                inputSchema: schema([
                    "window_id": windowID,
                    "text": string("Text to look for in labels, values and descriptions."),
                    "role": string("Accessibility role, such as AXButton or button."),
                    "limit": integer("Most matches to return, 1-100.", default: 20),
                ]),
                annotations: .init(readOnlyHint: true)
            ),
            Tool(
                name: "batch",
                description:
                    "Run several actions on one window in a single call, strictly in order, then return one observation after the UI settles. Batch predictable sequences where you already know every step: pressing a known series of buttons, filling known fields, select_text followed by type_text to replace a match or insert at its caret, typing followed by a shortcut, keyboard navigation. Do not batch past a step whose outcome you need to see first (a menu, dialog, or search results opening, or anything whose element indexes you do not know yet): end the batch there and look at the observation, or call get_window_state. Element indexes come from get_window_state and are not refreshed between steps. Pixel steps (x/y clicks, drags, scrolls at a point) take coordinate_space like the standalone tools, and stop the batch if the window has changed size since you last observed it, because the coordinates would be stale. Each step's result reports ok, route, and error; by default the batch stops at the first failure or uncertain result. The observation is a diff like get_window_state unless full=true. \(tabBatch)",
                inputSchema: schema(
                    [
                        "window_id": windowID,
                        "tab_id": integer("Agent tab id from browser_open, instead of window_id: the steps act in that tab."),
                        "steps": .object([
                            "type": "array",
                            "minItems": .int(1),
                            "maxItems": .int(50),
                            "description":
                                "Actions to run in order. Each step is the tool name plus that tool's own arguments, e.g. {\"tool\": \"click\", \"element_index\": 5} or {\"tool\": \"press_key\", \"keys\": \"cmd+a\"}. window_id is inherited from the batch unless a step sets its own. wait takes ms.",
                            "items": .object([
                                "type": "object",
                                "properties": .object(
                                    batchStepArguments.merging([
                                        "tool": string(
                                            "Step action.",
                                            enumValues: [
                                                "click", "type_text", "select_text", "press_key", "set_value", "scroll", "drag",
                                                "perform_action", "wait", "browser_click", "browser_type",
                                                "browser_press_key", "browser_scroll",
                                            ])
                                    ]) { $1 }),
                                "required": .array(["tool"]),
                            ]),
                        ]),
                        "observe": string(
                            "What to return after the last step.", enumValues: ["tree", "screenshot", "both", "none"],
                            default: "tree"),
                        "stop_on_error": boolean("Skip the remaining steps after the first failure.", default: true),
                        "stop_on_layout_change": boolean(
                            "Stop before a pixel step when the window's size changed since it was last observed.",
                            default: true),
                        "full": boolean(
                            "Return the whole outline in the observation instead of a diff.", default: false),
                    ], required: ["steps"])
            ),
            Tool(
                name: "click",
                description:
                    "Click an element by element_index (preferred; uses accessibility actions), or at x,y: window-local points by default, or pixels of your latest screenshot of this window with coordinate_space=\"screenshot\". Works in place on any window, including windows on other Spaces or behind other apps; the user's cursor and focus are untouched. To press several elements you already know, such as a series of calculator keys, use one batch call instead of separate clicks.",
                inputSchema: schema(
                    [
                        "window_id": windowID,
                        "element_index": elementIndex,
                        "x": number("x, in window points unless coordinate_space says otherwise. Use with y instead of element_index."),
                        "y": number("y, in window points unless coordinate_space says otherwise."),
                        "button": string("Mouse button.", enumValues: ["left", "right", "middle"], default: "left"),
                        "count": integer("Click count; 2 for a double-click.", default: 1),
                    ])
            ),
            Tool(
                name: "drag",
                description:
                    "Press at (from_x, from_y), move, and release at (to_x, to_y), in window-local points (or screenshot pixels with coordinate_space=\"screenshot\"). Use for selecting ranges, drawing, moving sliders, resizing, or drag-and-drop inside a window. Works in place like click.",
                inputSchema: schema(
                    [
                        "window_id": windowID,
                        "from_x": number("Start x."),
                        "from_y": number("Start y."),
                        "to_x": number("End x."),
                        "to_y": number("End y."),
                    ], required: ["from_x", "from_y", "to_x", "to_y"])
            ),
            Tool(
                name: "type_text",
                description:
                    "Type text into the window's focused element, or into element_index when given. Does not append Return automatically. For document line breaks, include newline characters in text so the default accessibility route can insert them without a keyboard focus handoff. Use press_key when a control requires an actual Return key, such as submitting a form. If the text shows in get_window_state but not on screen (common in custom-drawn apps such as DAWs and games), retry with keystrokes_only=true.",
                inputSchema: schema(
                    [
                        "window_id": windowID,
                        "text": string("Text to insert."),
                        "element_index": elementIndex,
                        "keystrokes_only": boolean(
                            "Send real key events instead of an accessibility insert.", default: false),
                    ], required: ["text"])
            ),
            Tool(
                name: "set_value",
                description:
                    "Replace an element's entire value (text field, text area, slider) in one step. More reliable than typing when filling a field from scratch.",
                inputSchema: schema(
                    [
                        "window_id": windowID,
                        "element_index": elementIndex,
                        "value": string("New value."),
                    ], required: ["element_index", "value"])
            ),
            Tool(
                name: "press_key",
                description:
                    "Send a key or chord to the window without bringing it to the foreground, e.g. \"return\", \"escape\", \"tab\", \"down\" (also ArrowDown), \"cmd+a\", \"cmd+shift+t\". Background keys may temporarily change the target app's internal active/key state, which can trigger app behavior such as autosave. For a document line break, prefer a newline in type_text. A cmd chord that matches a menu item presses that item (route ax_menu_shortcut). A successful menu press confirms dispatch only; it does not verify the command's effect. Check the resulting observation or supply an expectation to verify the outcome.",
                inputSchema: schema(
                    [
                        "window_id": windowID,
                        "keys": string("A +-separated chord of modifiers and one key."),
                    ], required: ["keys"])
            ),
            Tool(
                name: "scroll",
                description:
                    "Scroll the window, over element_index or at x,y when given (window points, or screenshot pixels with coordinate_space=\"screenshot\").",
                inputSchema: schema(
                    [
                        "window_id": windowID,
                        "direction": string("Scroll direction.", enumValues: ["up", "down", "left", "right"]),
                        "amount": integer("Number of scroll steps.", default: 3),
                        "element_index": elementIndex,
                        "x": number("x, in window points unless coordinate_space says otherwise."),
                        "y": number("y, in window points unless coordinate_space says otherwise."),
                    ], required: ["direction"])
            ),
            Tool(
                name: "perform_action",
                description:
                    "Invoke a named accessibility action on an element: one its outline line lists in actions=[...] (e.g. AXIncrement, AXDecrement, AXCancel, or a custom name), or AXShowMenu / AXConfirm. Use when a plain click is not the right verb.",
                inputSchema: schema(
                    [
                        "window_id": windowID,
                        "element_index": elementIndex,
                        "action": string("Accessibility action name, such as AXShowMenu."),
                    ], required: ["element_index", "action"])
            ),
            Tool(
                name: "screenshot",
                description:
                    "Capture a PNG of one window, even when it is covered or on another Space. The image is scaled down so its long edge is at most 1024 px; scale is image pixels per window point. To act at a pixel you see, pass it to click, drag or scroll with coordinate_space=\"screenshot\", or divide it by scale for window points.",
                inputSchema: schema(["window_id": windowID]),
                annotations: .init(readOnlyHint: true)
            ),
            Tool(
                name: "agent_display",
                description:
                    "Optional private workspace: an invisible virtual monitor. Not needed for input, since every tool works in place. \"status\" reports it, \"create\" ensures it exists, \"teardown\" returns every hosted window to the user's screen and removes it.",
                inputSchema: schema(
                    [
                        "action": string("What to do.", enumValues: ["status", "create", "teardown"])
                    ], required: ["action"])
            ),
            Tool(
                name: "move_to_agent_display",
                description:
                    "Rarely needed. Moves a window onto the invisible agent display, taking it off the user's screen. Input already works in place; use this only when a custom-drawn app stops repainting while off-screen and you need fresh screenshots. Tell the user, and undo with return_window as soon as possible.",
                inputSchema: schema(["window_id": windowID])
            ),
            Tool(
                name: "return_window",
                description: "Move a window from the agent display back to where it was before move_to_agent_display.",
                inputSchema: schema(["window_id": windowID])
            ),
            Tool(
                name: "browser_status",
                description: "Check whether the Second Cursor Chrome extension is connected. Browser tools need it.",
                inputSchema: schema([:]),
                annotations: .init(readOnlyHint: true)
            ),
            Tool(
                name: "browser_tabs",
                description:
                    "List the Chrome tabs the agent opened. Personal tabs require local approval to discover and claim.",
                inputSchema: schema([:]),
                annotations: .init(readOnlyHint: true)
            ),
            Tool(
                name: "browser_open",
                description:
                    "Open a URL in a new background Chrome tab owned by the agent, wait for the page to finish rendering, and return its outline (as browser_snapshot does): read text, prices and refs from it, not from a screenshot or another read. Use the returned tab id with the other browser tools, or browser_repl for several steps. Without browser_id it opens in the Chrome profile the user focused last; call browser_instances only when the page needs a different profile's account.",
                inputSchema: schema(["url": string("URL to open.")], required: ["url"])
            ),
            Tool(
                name: "browser_navigate",
                description:
                    "Load a URL in an agent tab. Returns the page once it settles (observation, like browser_snapshot's).",
                inputSchema: schema(["tab_id": tabID, "url": string("URL to load.")], required: ["tab_id", "url"])
            ),
            Tool(
                name: "browser_snapshot",
                description:
                    "Read an agent tab as a compact accessibility outline; interactive nodes carry [ref] markers for browser_click and browser_type. Waits briefly for content the page is still rendering. Your first read of a tab (browser_open counts) returns the whole outline; later reads return only what changed since your previous read or action (is_diff=true): lines starting with + are new or changed, lines starting with - are gone, and \"(no changes)\" means the page is the same. A large change returns the whole outline again. browser_open and the browser actions already return the outline or this diff, so do not call this right after them; call it for the whole outline (full=true) or after browser_command operation=wait.",
                inputSchema: schema(
                    [
                        "tab_id": tabID,
                        "include_screenshot": boolean("Attach a PNG of the viewport.", default: false),
                        "structured": boolean(
                            "Also return an elements JSON array on full reads, for code consumers.", default: false),
                        "expand": expand,
                    ], required: ["tab_id"]),
                annotations: .init(readOnlyHint: true)
            ),
            Tool(
                name: "browser_click",
                description:
                    "Click an element in an agent tab by ref from browser_snapshot (preferred), or at x,y CSS pixels in the viewport. Returns what changed on the page once it settles (observation, a diff like browser_snapshot's), so there is no need to read the tab after it. Clicks and typing you already know go in one batch with tab_id; a multi-step flow in one browser_repl call.",
                inputSchema: schema(
                    [
                        "tab_id": tabID,
                        "ref": string("Element ref from browser_snapshot."),
                        "x": number("Viewport x in CSS pixels. Use with y instead of ref."),
                        "y": number("Viewport y in CSS pixels."),
                    ], required: ["tab_id"])
            ),
            Tool(
                name: "browser_type",
                description:
                    "Type text into the editable element with ref, or the currently focused editable element. Verifies editability and focus before typing. For a native dropdown, use browser_command with operation=select_option. submit=true sends Enter afterwards only if the target retains focus. Returns what changed on the page once it settles (observation, a diff like browser_snapshot's). Fill several known fields in one batch with tab_id.",
                inputSchema: schema(
                    [
                        "tab_id": tabID,
                        "text": string("Text to type."),
                        "ref": string("Element ref from browser_snapshot."),
                        "submit": boolean("Press Enter after typing only while the target retains editable focus.", default: false),
                    ], required: ["tab_id", "text"])
            ),
            Tool(
                name: "browser_press_key",
                description:
                    "Send a key or chord to an agent tab, e.g. \"enter\", \"escape\", \"tab\", \"cmd+a\". Returns what changed on the page once it settles (observation, a diff like browser_snapshot's).",
                inputSchema: schema(
                    [
                        "tab_id": tabID,
                        "keys": string("A +-separated chord of modifiers and one key."),
                    ], required: ["tab_id", "keys"])
            ),
            Tool(
                name: "browser_scroll",
                description:
                    "Scroll an agent tab's page. Returns what changed on the page once it settles (observation, a diff like browser_snapshot's).",
                inputSchema: schema(
                    [
                        "tab_id": tabID,
                        "direction": string("Scroll direction.", enumValues: ["up", "down", "left", "right"]),
                        "amount": integer("Number of scroll steps.", default: 3),
                    ], required: ["tab_id", "direction"])
            ),
            Tool(
                name: "browser_screenshot",
                description: "Capture a PNG of an agent tab's viewport, even while the tab is in the background. Only for what the outline cannot show: text, prices and refs are in the outline every browser call returns.",
                inputSchema: schema(["tab_id": tabID], required: ["tab_id"]),
                annotations: .init(readOnlyHint: true)
            ),
            Tool(
                name: "browser_close",
                description: "Close a tab the agent opened. A claimed personal tab is released instead, and stays open.",
                inputSchema: schema(["tab_id": tabID], required: ["tab_id"])
            ),
        ] + extraTools).map { tool in
            if var schema = tool.inputSchema.objectValue, var props = schema["properties"]?.objectValue {
                let browserScoped =
                    tool.name.hasPrefix("browser_") && !["browser_instances", "browser_status"].contains(tool.name)
                let action = actionTools.contains(tool.name) || ["browser_command", "browser_repl"].contains(tool.name)
                if browserScoped { props["browser_id"] = string("Instance from browser_instances.") }
                if windowScopedTools.contains(tool.name) {
                    props["app"] = string(
                        tool.name == "get_window_state"
                            ? "App name or bundle id, instead of window_id. Reads and screenshots use the app's main window; actions, inspect_element and scoped reads use the window of that app you observed last."
                            : ["screenshot", "wait_for"].contains(tool.name)
                                ? "App name or bundle id, instead of window_id: that app's main window."
                                : "App name or bundle id, instead of window_id: the window of that app you observed last.")
                }
                if ["click", "drag", "scroll"].contains(tool.name) {
                    props["coordinate_space"] = string(
                        "How to read x/y: window (window-local points) or screenshot (pixels of your latest screenshot of this window; the server divides by its scale).",
                        enumValues: ["window", "screenshot"], default: "window")
                }
                if captionedTools.contains(tool.name) {
                    props["caption"] = string(
                        ["click", "batch", "browser_click"].contains(tool.name)
                            ? "Optional: a few words shown to the user beside the agent's cursor while this runs, e.g. \"Opening the Export menu\". Present tense, under 60 characters."
                            : "Optional: a few words shown beside the agent's cursor while this runs, as in click.")
                }
                if observingTools.contains(tool.name) {
                    props["observe"] = string(
                        tool.name == "click"
                            ? "Returned after the action: tree (what changed since your last read of this window), both (tree and screenshot), screenshot, or none when chaining blind."
                            : "As in click.",
                        enumValues: ["tree", "both", "screenshot", "none"], default: "tree")
                }
                if observingBrowserTools.contains(tool.name) {
                    props["observe"] = string(
                        tool.name == "browser_click"
                            ? "Returned after the action: tree (what changed since your last read of this tab), both (tree and screenshot), screenshot, or none when chaining blind."
                            : "As in browser_click.",
                        enumValues: ["tree", "both", "screenshot", "none"], default: "tree")
                }
                if observingTools.contains(tool.name) || tool.name == "batch" {
                    props["expect"] = tool.name == "batch" ? batchExpect : tool.name == "click" ? expect : expectPointer
                }
                if expectingBrowserTools.contains(tool.name) { props["expect"] = browserExpect }
                if tool.name == "browser_snapshot" {
                    props["full"] = boolean(
                        "Return the whole outline instead of a diff; a third unchanged read in a row answers (no changes).", default: false)
                }
                if !["status", "list_apps", "list_windows", "browser_instances", "browser_status"].contains(tool.name) {
                    props["session_id"] = string("Explicit session; omit for compatibility clients.")
                }
                if action {
                    props["operation_id"] = string("Retry ID; retained for one hour by default.")
                    if windowScopedTools.contains(tool.name) || (browserScoped && tool.name != "browser_repl") {
                        props["observation_id"] = string("Reject a superseded observation.")
                    }
                }
                if action || ["get_window_state", "browser_snapshot", "inspect_element", "find_elements"].contains(tool.name) {
                    props["timeout_ms"] = integer(
                        tool.name == "browser_repl"
                            ? "Call deadline, including queue time; at most and by default 55000."
                            : "Call deadline, including approval and queue time; max 60000.")
                }
                schema["properties"] = .object(props)
                return Tool(
                    name: tool.name, description: tool.description, inputSchema: .object(schema),
                    annotations: tool.annotations)
            }
            return tool
        }

    private static let extraTools: [Tool] = [
        Tool(
            name: "task",
            description: "Name and supervise this session's task. Use update to report thinking, needs_user, completed or failed; pause/takeover stop new input, and resume requires fresh target observations. Task state is independent of tool-call activity. Cancel never undoes already delivered input.",
            inputSchema: schema([
                "action": string("Task operation", enumValues: ["create", "list", "get", "update", "bind", "pause", "takeover", "resume", "cancel"]),
                "task_id": string("Task ID; defaults to this session's task. Only tasks bound to this session may be controlled."),
                "title": string("Short task name; omit sensitive content."),
                "client_name": string("Host or agent name shown in the task panel."),
                "status": string("Host-reported lifecycle state", enumValues: ["queued", "running", "thinking", "needs_user", "completed", "failed"]),
                "include_finished": boolean("Include completed, failed and cancelled tasks in the list.", default: false),
            ], required: ["action"])),
        Tool(
            name: "capabilities",
            description: "Inspect supported native gestures and current target restrictions before choosing an input route. Unsupported gestures refuse before dispatch; the agent display shares the current GUI session and is not an isolated machine.",
            inputSchema: schema(["window_id": integer("Optional target window for context-specific capabilities.")]),
            annotations: .init(readOnlyHint: true)),
        Tool(
            name: "session",
            description:
                "Create or close an explicit client session. Closing cancels queued work and cleans up ephemeral browser tabs.",
            inputSchema: schema(["action": string("Action", enumValues: ["create", "close"])], required: ["action"])),
        Tool(
            name: "bind_target", description: "Claim a target (window:<id>) for this session or release it.",
            inputSchema: schema(
                ["target": string("Target"), "action": string("claim or release")], required: ["target"])),
        Tool(
            name: "operation_status", description: "Check the operation_id reported by an uncertain or timed-out action without repeating input. Inspect its state and outcome_known, then observe the target before deciding whether another action is needed. An expired result body does not mean the action failed.",
            inputSchema: schema(["operation_id": string("Operation ID from the action result or error")], required: ["operation_id"]),
            annotations: .init(readOnlyHint: true)),
        Tool(
            name: "cancel_operation", description: "Cancel queued work; already delivered input cannot be undone.",
            inputSchema: schema(["operation_id": string("Operation ID")], required: ["operation_id"])),
        Tool(
            name: "inspect_element",
            description: "Read an element's full text in UTF-16 ranges, its selection and supported actions.",
            inputSchema: schema(
                [
                    "window_id": windowID, "element_index": elementIndex, "offset": integer("UTF-16 offset"),
                    "length": integer("Maximum UTF-16 units, up to 100000"),
                ], required: ["element_index"])),
        Tool(
            name: "select_text",
            description:
                "Select exactly one text match or place the caret before/after it. Use prefix/suffix for repeated text.",
            inputSchema: schema(
                [
                    "window_id": windowID, "element_index": elementIndex, "text": string("Exact text"),
                    "prefix": string("Text immediately before"), "suffix": string("Text immediately after"),
                    "selection_type": string("Mode", enumValues: ["text", "cursor_before", "cursor_after"]),
                ], required: ["element_index", "text"])),
        Tool(
            name: "paste",
            description:
                "Paste text or HTML into a native window while preserving the clipboard. md inserts Markdown source.",
            inputSchema: schema(
                [
                    "window_id": windowID, "text": string("Content"),
                    "format": string("Format", enumValues: ["text", "html", "md"]),
                ], required: ["text"])),
        Tool(
            name: "browser_instances",
            description: "List connected browser profiles by signed-in account. default marks the profile calls without browser_id go to (the one the user focused last); tab calls follow the profile that owns the tab.",
            inputSchema: schema([:])),
        Tool(
            name: "browser_command",
            description:
                "Extended browser control. inspect requires arguments.locator; select_option requires arguments.locator and arguments.value. Snapshot refs work with browser_click/browser_type, not as top-level targets for inspect/select_option. Tab operations require an agent-owned or explicitly claimed tab. Call operation=capabilities for other operations and approval requirements.",
            inputSchema: schema(
                [
                    "browser_id": string("Instance from browser_instances; defaults to the tab's profile, else the profile the user focused last"),
                    "tab_id": tabID,
                    "operation": string(
                        "Operation",
                        enumValues: [
                            "capabilities", "user_tabs", "claim", "release", "retain", "dom_snapshot", "inspect",
                            "click", "hover", "fill", "type", "paste", "select_text", "perform_action", "check", "select_option",
                            "press_key", "scroll", "drag", "back", "forward", "reload", "wait", "dialog", "upload",
                            "downloads", "logs", "history", "export", "assets", "viewport", "screenshot", "cdp",
                            "batch", "clipboard",
                        ]),
                    "arguments": browserCommandArguments,
                ], required: ["operation"])),
        Tool(
            name: "browser_repl",
            description:
                "The fastest way through a multi-step web flow: fill a form, search, filter, page through results and return just the text you need, in one call instead of one per click. A single click or read needs only the browser_ tools. A persistent JavaScript REPL with top-level await. Returns what the code printed and its last expression's value. `page` is a Playwright-compatible page on the agent tab you pass as tab_id, else on its own background tab (`context` is its browser context), e.g. await page.goto(url); await page.getByLabel('From').fill('SFO'); await page.getByRole('button', {name: 'Search'}).click(); await page.getByRole('main').innerText(). page.snapshot() returns the page's accessibility outline, whose [e123] refs work as page.locator('aria-ref=e123'). There is no page.evaluate: operate pages through locators.",
            inputSchema: schema(["code": string("JavaScript to run."), "tab_id": tabID], required: ["code"])),
        Tool(
            name: "wait_for",
            description:
                "Wait until text shows up in a window (or element_index has exactly value), or with gone=true until it goes away, then return status and what changed since your last read of the window. It wakes on the app's own change notifications, so it returns as soon as the change lands. Use it for search results, dialogs, saves or progress that take a moment, instead of reading the window again and again. status is verified, or failed when timeout_ms passed; detail says what was last seen.",
            inputSchema: schema(
                conditionFields.merging([
                    "window_id": windowID,
                    "caption": string("Optional: a few words shown beside the agent's cursor while it waits."),
                ]) { $1 }),
            annotations: .init(readOnlyHint: true)),
    ]

    /// Describe the public DOM contract at tools/list time; models should not need
    /// a rejected call to discover how a locator differs from an outline ref.
    /// Other extended operations keep their existing fields and capabilities help.
    private static let browserCommandArguments: Value = [
        "type": "object",
        "description":
            "inspect: {\"locator\":{\"role\":\"region\",\"name\":\"Details\"}}. select_option: {\"locator\":{\"label\":\"Route\"},\"value\":\"Priority\"}. inspect/select_option/check require locator, not top-level ref; option labels go in value, not top-level label. Other operation fields remain supported; capabilities lists them.",
        "properties": .object([
            "locator": [
                "type": "object",
                "description": "Target observed page content; refine ambiguous matches to one element.",
                "properties": .object([
                    "selector": string("CSS selector, e.g. #details or section."),
                    "role": string("ARIA role, e.g. button, combobox, region. A named HTML section is region; section is a tag, not an ARIA role."),
                    "name": string("Observed accessible name."),
                    "label": string("Form control's label, not an option label."),
                    "text": string("Element text."),
                    "placeholder": string("Control placeholder."),
                    "test_id": string("Observed data-testid."),
                    "exact": ["type": "boolean", "description": "Complete text/name/label match; false allows substring."],
                    "nth": integer("Zero-based match; observe alternatives first."),
                    "visible": ["type": "boolean"],
                    "frame": stringOrStringArray("Observed frame selector or nested frame selectors."),
                ]),
                "additionalProperties": true,
            ],
            "value": stringOrStringArray("select_option: exact option value or label (normalized whitespace); values take precedence. Multiple options require a multiple-select control. Inspect for available/selected/disabled options."),
            "text": string("fill/type/paste/select_text: text to enter or select."),
        ]),
        "additionalProperties": true,
    ]

    private static func stringOrStringArray(_ description: String) -> Value {
        ["description": .string(description),
         "anyOf": [["type": "string"], ["type": "array", "items": ["type": "string"]]]]
    }

    /// Scalars only: Gemini through Cursor stringifies nested free-form objects. The router also takes expect as a
    /// JSON string.
    private static let conditionFields: [String: Value] = [
        "text": string("Some element's title, value or description contains this (case-sensitive)."),
        "element_index": integer("With value: an element [index] from your latest read of this window."),
        "value": string("With element_index: the value that element must have, exactly."),
        "gone": boolean("Wait for the condition to stop holding instead, e.g. a progress message to go away.", default: false),
        "timeout_ms": integer("How long to wait, up to 10000.", default: 2000),
    ]

    private static let expect: Value = [
        "type": "object",
        "description":
            "Optional check that the action worked, instead of reading the window again: text somewhere in the window, or element_index having exactly value. It is checked once before the action, then waited for after it (for batch, after the last step). The result's expect says verified, preexisting (it already held before the action), or failed with what was seen instead; failed is not an error.",
        "properties": .object(conditionFields),
    ]

    private static let expectPointer: Value = [
        "type": "object",
        "description":
            "Optional check that the action worked, as in click: {text} or {element_index, value}, with optional gone and timeout_ms.",
    ]

    private static let browserConditionFields: [String: Value] = [
        "text": string("Case-sensitive substring in visible rendered page text or accessible name/value; excludes hidden content."),
        "ref": string("With value: an element ref from this tab's latest observation, pinned before input."),
        "value": string("With ref: the exact value required of that same element."),
        "gone": boolean("Invert the combined condition. A stale or unreadable ref fails; it does not prove absence.", default: false),
        "timeout_ms": ["type": "integer", "description": "Postcondition wait, separate from the call deadline.",
                       "minimum": 0, "maximum": 10000, "default": 2000],
    ]

    private static let browserExpect: Value = [
        "description": "Optional postcondition, checked before input then waited for after it: text, or ref with value; both must hold when combined. Returns expect.status verified, preexisting or failed, even with observe=none. Failed means reconcile before retrying, not that input did not happen. Accepts a JSON object string too.",
        "anyOf": [["type": "object", "properties": .object(browserConditionFields), "additionalProperties": false],
                  ["type": "string"]],
    ]

    private static let batchExpect: Value = [
        "description": "Check before the batch, then wait after its last step. Native: text or element_index with value, as in click. Tab: text or ref with value, as in browser_click; a failed/skipped tab step makes expect failed. Accepts a JSON object string too.",
        "anyOf": [["type": "object", "properties": .object(conditionFields.merging([
            "ref": browserConditionFields["ref"]!,
            "value": string("The exact value for element_index (native window) or ref (browser tab)."),
            "timeout_ms": integer("Wait after the steps: 0–10000 ms for tabs; capped at 10000 for native windows.", default: 2000),
        ]) { $1 }), "additionalProperties": false], ["type": "string"]],
    ]

    /// Flat on each step rather than nested under args: Gemini through Cursor stringifies a free-form nested
    /// object and garbles the keys of a declared one. The router still accepts {tool, args}.
    static let batchStepArguments: [String: Value] = [
        "window_id": integer("Only when this step targets another window than the batch."),
        "element_index": elementIndex,
        "x": number("click, scroll: x, in window points unless coordinate_space says otherwise."),
        "y": number("click, scroll: y."),
        "coordinate_space": string("click, drag, scroll: window or screenshot.", enumValues: ["window", "screenshot"]),
        "button": string("click: mouse button.", enumValues: ["left", "right", "middle"]),
        "count": integer("click: 2 for a double-click."),
        "text": string("type_text: text to insert. select_text: exact nonempty text to match."),
        "prefix": string("select_text: text immediately before the match, to disambiguate repeated text."),
        "suffix": string("select_text: text immediately after the match, to disambiguate repeated text."),
        "selection_type": string("select_text: select the match or put the caret before/after it.", enumValues: ["text", "cursor_before", "cursor_after"]),
        "keystrokes_only": ["type": "boolean", "description": "type_text: send real key events instead of an accessibility insert."],
        "keys": string("press_key: chord such as \"return\" or \"cmd+a\"."),
        "value": string("set_value: new value."),
        "direction": string("scroll: direction.", enumValues: ["up", "down", "left", "right"]),
        "amount": integer("scroll: number of steps."),
        "from_x": number("drag: start x."),
        "from_y": number("drag: start y."),
        "to_x": number("drag: end x."),
        "to_y": number("drag: end y."),
        "action": string("perform_action: accessibility action name."),
        "ms": integer("wait: milliseconds."),
        "caption": string("Shown beside the agent's cursor from this step on, replacing the batch's caption."),
        "ref": string("browser_click, browser_type: element ref from the tab's outline, such as e12."),
        "submit": ["type": "boolean", "description": "browser_type: press Enter after typing only while the target retains editable focus."],
    ]

    private static let windowID = integer("Window id from list_windows or launch_app. Omit to target app instead.")
    private static let elementIndex = integer("Element [index] from get_window_state of this window.")
    private static let tabID = integer("Agent tab id from browser_open or browser_tabs.")

    private static func schema(_ properties: [String: Value], required: [String] = []) -> Value {
        var object: [String: Value] = ["type": "object", "properties": .object(properties)]
        if !required.isEmpty {
            object["required"] = .array(required.map { .string($0) })
        }
        return .object(object)
    }

    private static func integer(_ description: String, default value: Int? = nil) -> Value {
        var object: [String: Value] = ["type": "integer", "description": .string(description)]
        if let value { object["default"] = .int(value) }
        return .object(object)
    }

    private static func number(_ description: String) -> Value {
        ["type": "number", "description": .string(description)]
    }

    private static func boolean(_ description: String, default value: Bool) -> Value {
        ["type": "boolean", "description": .string(description), "default": .bool(value)]
    }

    private static func string(_ description: String, enumValues: [String]? = nil, default value: String? = nil)
        -> Value
    {
        var object: [String: Value] = ["type": "string", "description": .string(description)]
        if let enumValues { object["enum"] = .array(enumValues.map { .string($0) }) }
        if let value { object["default"] = .string(value) }
        return .object(object)
    }

    private static func stringArray(_ description: String) -> Value {
        ["type": "array", "items": ["type": "string"], "description": .string(description)]
    }
}
