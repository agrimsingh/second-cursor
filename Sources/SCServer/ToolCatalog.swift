import MCP

enum ToolCatalog {
    /// State-changing tools; each call is written to the action log before it runs.
    /// Mirrors ACTION_TOOLS in e2e/run.py. `agent_display` create/teardown is logged too.
    static let actionTools: Set<String> = [
        "launch_app", "batch", "click", "drag", "type_text", "set_value", "press_key", "scroll", "perform_action",
        "move_to_agent_display", "return_window", "browser_open", "browser_navigate", "browser_click",
        "browser_type", "browser_press_key", "browser_scroll", "browser_close",
    ]

    /// Tools that take a `window_id` and must refuse windows owned by blocked apps.
    static let windowScopedTools: Set<String> = [
        "get_window_state", "batch", "click", "drag", "type_text", "set_value", "press_key", "scroll", "perform_action",
        "screenshot", "move_to_agent_display", "return_window",
    ]

    static let tools: [Tool] = [
        Tool(
            name: "status",
            description: "Check Second Cursor's health: macOS permissions, whether the user paused the agent, the agent display, and the Chrome extension. Call this first when other tools fail.",
            inputSchema: schema([:]),
            annotations: .init(readOnlyHint: true)
        ),
        Tool(
            name: "list_apps",
            description: "List running apps (name, bundle_id, pid). Set include_not_running to also list installed apps. Use it to find the exact name or bundle id for launch_app.",
            inputSchema: schema(["include_not_running": boolean("Also list installed apps that are not running.", default: false)]),
            annotations: .init(readOnlyHint: true)
        ),
        Tool(
            name: "list_windows",
            description: "List top-level windows with id, title, frame (global screen points), Space ids, and whether each is on the agent display. The window id is the handle every window tool needs.",
            inputSchema: schema(["app": string("Filter by app name or bundle id (case-insensitive substring).")]),
            annotations: .init(readOnlyHint: true)
        ),
        Tool(
            name: "launch_app",
            description: "Launch an app in the background without activating it, then wait up to 10s for its window. Windows stay where the app puts them; every tool works on them in place. Use new_instance=true to get a separate process instead of sharing the user's running copy.",
            inputSchema: schema([
                "app": string("App name, bundle id, or path to the .app."),
                "urls": stringArray("Files or URLs to open with the app."),
                "new_instance": boolean("Start a separate process even if the app is already running.", default: false),
                "on_agent_display": boolean("Opt-in: move the app's new windows to the invisible agent display.", default: false),
            ], required: ["app"])
        ),
        Tool(
            name: "get_window_state",
            description: "Read a window: an indented accessibility outline where actionable elements carry [index] markers and window-local frames, plus a screenshot. The first read of a window returns the whole outline; later reads return only what changed since the previous read (is_diff=true): lines starting with + are new or changed, lines starting with - are gone, and \"(no changes)\" means the UI is the same. An element keeps its index across reads as long as it keeps its place in the UI, so indexes from earlier reads stay usable. Pass full=true to get the whole outline again, and scope=<index> to read everything inside one element at full depth when the outline is truncated or a region needs detail.",
            inputSchema: schema([
                "window_id": windowID,
                "include_screenshot": boolean("Attach a PNG screenshot of the window.", default: true),
                "include_menu_bar": boolean("Include the app's menu bar in the outline.", default: false),
                "full": boolean("Return the whole outline instead of a diff from the previous read.", default: false),
                "scope": integer("Return only the subtree under this element index, at full depth. Not a diff."),
            ], required: ["window_id"]),
            annotations: .init(readOnlyHint: true)
        ),
        Tool(
            name: "batch",
            description: "Run several actions on one window in a single call, strictly in order, then return one observation after the UI settles. Batch predictable sequences where you already know every step: pressing a known series of buttons, filling known fields, typing followed by a shortcut, keyboard navigation. Do not batch past a step whose outcome you need to see first (a menu, dialog, or search results opening, or anything whose element indexes you do not know yet): end the batch there and look at the observation, or call get_window_state. Element indexes come from get_window_state and are not refreshed between steps. Pixel steps (x/y clicks, drags, scrolls at a point) stop the batch if the window has changed size since you last observed it, because the coordinates would be stale. Each step's result reports ok, route, and error; by default the batch stops at the first failure. The observation is a diff like get_window_state unless full=true.",
            inputSchema: schema([
                "window_id": windowID,
                "steps": .object([
                    "type": "array",
                    "minItems": .int(1),
                    "maxItems": .int(50),
                    "description": "Actions to run in order. Each step is {tool, args}; args are the standalone tool's arguments. window_id is inherited from the batch unless a step sets its own. wait takes {ms}.",
                    "items": .object([
                        "type": "object",
                        "properties": .object([
                            "tool": string("Step action.", enumValues: ["click", "type_text", "press_key", "set_value", "scroll", "drag", "perform_action", "wait"]),
                            "args": .object(["type": "object", "description": "Arguments for the step's tool."]),
                        ]),
                        "required": .array(["tool"]),
                    ]),
                ]),
                "observe": string("What to return after the last step.", enumValues: ["tree", "screenshot", "both", "none"], default: "tree"),
                "stop_on_error": boolean("Skip the remaining steps after the first failure.", default: true),
                "stop_on_layout_change": boolean("Stop before a pixel step when the window's size changed since it was last observed.", default: true),
                "full": boolean("Return the whole outline in the observation instead of a diff.", default: false),
            ], required: ["window_id", "steps"])
        ),
        Tool(
            name: "click",
            description: "Click an element by element_index (preferred; uses accessibility actions), or at x,y in window-local points (= screenshot pixels). Works in place on any window, including windows on other Spaces or behind other apps; the user's cursor and focus are untouched.",
            inputSchema: schema([
                "window_id": windowID,
                "element_index": elementIndex,
                "x": number("Window-local x in points. Use with y instead of element_index."),
                "y": number("Window-local y in points."),
                "button": string("Mouse button.", enumValues: ["left", "right", "middle"], default: "left"),
                "count": integer("Click count; 2 for a double-click.", default: 1),
            ], required: ["window_id"])
        ),
        Tool(
            name: "drag",
            description: "Press at (from_x, from_y), move, and release at (to_x, to_y), in window-local points. Use for selecting ranges, drawing, moving sliders, resizing, or drag-and-drop inside a window. Works in place like click.",
            inputSchema: schema([
                "window_id": windowID,
                "from_x": number("Start x, window-local points."),
                "from_y": number("Start y, window-local points."),
                "to_x": number("End x, window-local points."),
                "to_y": number("End y, window-local points."),
            ], required: ["window_id", "from_x", "from_y", "to_x", "to_y"])
        ),
        Tool(
            name: "type_text",
            description: "Type text into the window's focused element, or into element_index when given. Does not press Return; follow with press_key if needed. If the text shows in get_window_state but not on screen (common in custom-drawn apps such as DAWs and games), retry with keystrokes_only=true.",
            inputSchema: schema([
                "window_id": windowID,
                "text": string("Text to insert."),
                "element_index": elementIndex,
                "keystrokes_only": boolean("Send real key events instead of an accessibility insert.", default: false),
            ], required: ["window_id", "text"])
        ),
        Tool(
            name: "set_value",
            description: "Replace an element's entire value (text field, text area, slider) in one step. More reliable than typing when filling a field from scratch.",
            inputSchema: schema([
                "window_id": windowID,
                "element_index": elementIndex,
                "value": string("New value."),
            ], required: ["window_id", "element_index", "value"])
        ),
        Tool(
            name: "press_key",
            description: "Send a key or chord to the window without focusing it, e.g. \"return\", \"escape\", \"tab\", \"down\", \"cmd+a\", \"cmd+shift+t\".",
            inputSchema: schema([
                "window_id": windowID,
                "keys": string("A +-separated chord of modifiers and one key."),
            ], required: ["window_id", "keys"])
        ),
        Tool(
            name: "scroll",
            description: "Scroll the window, over element_index or at x,y when given.",
            inputSchema: schema([
                "window_id": windowID,
                "direction": string("Scroll direction.", enumValues: ["up", "down", "left", "right"]),
                "amount": integer("Number of scroll steps.", default: 3),
                "element_index": elementIndex,
                "x": number("Window-local x in points."),
                "y": number("Window-local y in points."),
            ], required: ["window_id", "direction"])
        ),
        Tool(
            name: "perform_action",
            description: "Invoke a named accessibility action on an element, e.g. AXShowMenu, AXIncrement, AXDecrement, AXConfirm, AXCancel. Use when a plain click is not the right verb.",
            inputSchema: schema([
                "window_id": windowID,
                "element_index": elementIndex,
                "action": string("Accessibility action name, such as AXShowMenu."),
            ], required: ["window_id", "element_index", "action"])
        ),
        Tool(
            name: "screenshot",
            description: "Capture a PNG of one window, even when it is covered or on another Space. Divide image pixels by scale to get window-local points.",
            inputSchema: schema(["window_id": windowID], required: ["window_id"]),
            annotations: .init(readOnlyHint: true)
        ),
        Tool(
            name: "agent_display",
            description: "Optional private workspace: an invisible virtual monitor. Not needed for input, since every tool works in place. \"status\" reports it, \"create\" ensures it exists, \"teardown\" returns every hosted window to the user's screen and removes it.",
            inputSchema: schema([
                "action": string("What to do.", enumValues: ["status", "create", "teardown"]),
            ], required: ["action"])
        ),
        Tool(
            name: "move_to_agent_display",
            description: "Rarely needed. Moves a window onto the invisible agent display, taking it off the user's screen. Input already works in place; use this only when a custom-drawn app stops repainting while off-screen and you need fresh screenshots. Tell the user, and undo with return_window as soon as possible.",
            inputSchema: schema(["window_id": windowID], required: ["window_id"])
        ),
        Tool(
            name: "return_window",
            description: "Move a window from the agent display back to where it was before move_to_agent_display.",
            inputSchema: schema(["window_id": windowID], required: ["window_id"])
        ),
        Tool(
            name: "browser_status",
            description: "Check whether the Second Cursor Chrome extension is connected. Browser tools need it.",
            inputSchema: schema([:]),
            annotations: .init(readOnlyHint: true)
        ),
        Tool(
            name: "browser_tabs",
            description: "List the Chrome tabs the agent opened. The user's own tabs are never listed or touched.",
            inputSchema: schema([:]),
            annotations: .init(readOnlyHint: true)
        ),
        Tool(
            name: "browser_open",
            description: "Open a URL in a new background Chrome tab owned by the agent. Use the returned tab id with the other browser tools.",
            inputSchema: schema(["url": string("URL to open.")], required: ["url"])
        ),
        Tool(
            name: "browser_navigate",
            description: "Load a URL in an agent tab.",
            inputSchema: schema(["tab_id": tabID, "url": string("URL to load.")], required: ["tab_id", "url"])
        ),
        Tool(
            name: "browser_snapshot",
            description: "Read an agent tab as a compact accessibility outline; interactive nodes carry [ref] markers for browser_click and browser_type. Call again after the page changes.",
            inputSchema: schema([
                "tab_id": tabID,
                "include_screenshot": boolean("Attach a PNG of the viewport.", default: false),
            ], required: ["tab_id"]),
            annotations: .init(readOnlyHint: true)
        ),
        Tool(
            name: "browser_click",
            description: "Click an element in an agent tab by ref from browser_snapshot (preferred), or at x,y CSS pixels in the viewport.",
            inputSchema: schema([
                "tab_id": tabID,
                "ref": string("Element ref from browser_snapshot."),
                "x": number("Viewport x in CSS pixels. Use with y instead of ref."),
                "y": number("Viewport y in CSS pixels."),
            ], required: ["tab_id"])
        ),
        Tool(
            name: "browser_type",
            description: "Type text into the element with ref, or into the focused element. submit=true presses Enter afterwards.",
            inputSchema: schema([
                "tab_id": tabID,
                "text": string("Text to type."),
                "ref": string("Element ref from browser_snapshot."),
                "submit": boolean("Press Enter after typing.", default: false),
            ], required: ["tab_id", "text"])
        ),
        Tool(
            name: "browser_press_key",
            description: "Send a key or chord to an agent tab, e.g. \"enter\", \"escape\", \"tab\", \"cmd+a\".",
            inputSchema: schema([
                "tab_id": tabID,
                "keys": string("A +-separated chord of modifiers and one key."),
            ], required: ["tab_id", "keys"])
        ),
        Tool(
            name: "browser_scroll",
            description: "Scroll an agent tab's page.",
            inputSchema: schema([
                "tab_id": tabID,
                "direction": string("Scroll direction.", enumValues: ["up", "down", "left", "right"]),
                "amount": integer("Number of scroll steps.", default: 3),
            ], required: ["tab_id", "direction"])
        ),
        Tool(
            name: "browser_screenshot",
            description: "Capture a PNG of an agent tab's viewport, even while the tab is in the background.",
            inputSchema: schema(["tab_id": tabID], required: ["tab_id"]),
            annotations: .init(readOnlyHint: true)
        ),
        Tool(
            name: "browser_close",
            description: "Close an agent tab.",
            inputSchema: schema(["tab_id": tabID], required: ["tab_id"])
        ),
    ]

    private static let windowID = integer("Window id from list_windows or launch_app.")
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

    private static func string(_ description: String, enumValues: [String]? = nil, default value: String? = nil) -> Value {
        var object: [String: Value] = ["type": "string", "description": .string(description)]
        if let enumValues { object["enum"] = .array(enumValues.map { .string($0) }) }
        if let value { object["default"] = .string(value) }
        return .object(object)
    }

    private static func stringArray(_ description: String) -> Value {
        ["type": "array", "items": ["type": "string"], "description": .string(description)]
    }
}
