/** Arguments exposed by the runtime. Unknown fields remain available through call(). */
export type Arguments = Record<string, unknown>;
export type Point = readonly [number, number];
export type ElementTarget = number | Point;
export type Direction = "up" | "down" | "left" | "right";
export interface RequestOptions {
  /** Aborting stops the wait, not necessarily the action. Reconcile before retrying. */
  signal?: AbortSignal;
  /** Client transport timeout, independent of the runtime's timeout_ms argument. */
  timeoutMs?: number;
}
export interface ImageContent {
  type: "image";
  data: string;
  mimeType: string;
}
export interface Element extends Arguments {
  index: number;
  role?: string;
  title?: string;
  value?: string;
}
export interface Observation extends Arguments {
  snapshot_id: string;
  tree?: string;
  text?: string;
  elements?: Element[];
  freshness?: {
    revision: string;
    captured_at_ms: number;
    age_ms: number;
    status: "current" | "changed_during_capture";
  };
}
export interface ToolResult extends Arguments {
  ok?: boolean;
  observation?: Observation | null;
  observation_error?: string;
  observation_error_code?: ErrorCode;
  snapshot_id?: string;
  images?: ImageContent[];
  outcome?: { state: string; verified: boolean; reason?: string; verification?: string; expected?: unknown; actual?: unknown };
}
export interface OperationReference {
  operationId: string;
  sessionId?: string | null;
}
export interface OperationStatus extends ToolResult {
  operation_id: string;
  state: "queued" | "dispatched" | "completed" | "failed" | "cancelled" | "uncertain" | "interrupted";
  outcome_known: boolean;
  result_expired?: boolean;
  cancel_requested?: boolean;
  result?: ToolResult;
  error?: string;
}
export type ErrorCode = "ABORTED" | "TIMEOUT" | "NETWORK_ERROR" | "HTTP_ERROR" | "RPC_ERROR" | "TOOL_ERROR" | "INVALID_RESPONSE" | "INVALID_ARGUMENT" | "SESSION_MISMATCH" | "STALE_OBSERVATION" | "UNTRACKED_OPERATION" | "CALLBACK_ERROR";
export class SecondCursorError extends Error {
  constructor(message: string, options?: {
    code?: ErrorCode;
    operationId?: string;
    sessionId?: string | null;
    tool?: string;
    outcomeUncertain?: boolean;
    status?: number;
    result?: unknown;
    cause?: unknown;
  });
  name: "SecondCursorError";
  code?: ErrorCode;
  operationId?: string;
  sessionId?: string | null;
  tool?: string;
  /** True means the action may have executed. Never infer failure from a lost response. */
  outcomeUncertain: boolean;
  status?: number;
  /** CALLBACK_ERROR includes the successfully returned tool result. */
  result?: unknown;
  /** Controls outside the operation journal require inspecting their current state. */
  recovery: "operation_status" | "task_state" | "session_state" | "target_state";
}
export interface ClientOptions {
  url?: string;
  token?: string;
  /** Host-issued session to resume. Unknown/closed sessions fail rather than resetting. */
  sessionId?: string;
  transport?: "http" | "mcp";
  timeoutMs?: number;
  onResult?: (event: { tool: string; args: Arguments; data: ToolResult }) => unknown | Promise<unknown>;
}
export interface WindowInfo extends Arguments { id: number; }
export interface BrowserInfo extends Arguments { id: string; }
export class SecondCursor {
  constructor(options?: ClientOptions);
  static local(options?: ClientOptions): Promise<SecondCursor>;
  url: string;
  token?: string;
  sessionId?: string | null;
  transport: "http" | "mcp";
  timeoutMs: number;
  onResult?: ClientOptions["onResult"];
  connect(options?: RequestOptions): Promise<this>;
  /** Calls connect automatically; never retries a mutation after transport failure. */
  call<T extends object = ToolResult>(tool: string, args?: Arguments, options?: RequestOptions): Promise<T>;
  state(): Promise<{ apps: Arguments[]; windows: WindowInfo[]; browsers: BrowserInfo[] }>;
  app(query: string, options?: { windowId?: number; newInstance?: boolean }): Promise<App>;
  window(id: number): Promise<App>;
  browser(id: string): Browser;
  status(id: string, options?: RequestOptions): Promise<OperationStatus>;
  cancel(id: string, options?: RequestOptions): Promise<OperationStatus>;
  /** Reads recorded operation state in this session; does not replay the original action. */
  reconcile(operation: string | OperationReference | SecondCursorError, options?: RequestOptions): Promise<OperationStatus>;
  tasks(options?: { include_finished?: boolean }): Promise<TaskSnapshot[]>;
  createTask(options?: TaskUpdate): Promise<Task>;
  /** Bind a recovered interrupted task to this session. */
  bindTask(id: string): Promise<Task>;
  task(id?: string): Task;
  close(): Promise<void>;
}
export type TaskStatus = "queued" | "running" | "thinking" | "needs_user" | "paused" | "completed" | "failed" | "cancelled" | "interrupted";
export interface TaskSnapshot extends Arguments {
  id: string;
  title: string;
  client_name?: string;
  sessions?: string[];
  status: TaskStatus;
  created_at?: string;
  updated_at?: string;
  last_heartbeat_at?: string;
  targets?: unknown[];
  last_verified_outcome?: unknown;
  in_flight_count?: number;
  requires_fresh_observation?: boolean;
  control_generation?: number;
}
export interface TaskUpdate extends Arguments {
  title?: string;
  client_name?: string;
  status?: TaskStatus;
}
export class Task {
  constructor(client: SecondCursor, id?: string, snapshot?: TaskSnapshot | null);
  client: SecondCursor;
  id?: string;
  snapshot: TaskSnapshot | null;
  command(action: "get" | "update" | "pause" | "takeover" | "resume" | "cancel", args?: Arguments, options?: RequestOptions): Promise<TaskSnapshot>;
  get(options?: RequestOptions): Promise<TaskSnapshot>;
  update(update: TaskUpdate, options?: RequestOptions): Promise<TaskSnapshot>;
  pause(options?: RequestOptions): Promise<TaskSnapshot>;
  takeover(options?: RequestOptions): Promise<TaskSnapshot>;
  resume(options?: RequestOptions): Promise<TaskSnapshot>;
  cancel(options?: RequestOptions): Promise<TaskSnapshot>;
}
export class App {
  constructor(client: SecondCursor, id: number);
  client: SecondCursor;
  id: number;
  observation: Observation | null;
  observe(options?: Arguments, requestOptions?: RequestOptions): Promise<Observation>;
  act(tool: string, args?: Arguments, options?: RequestOptions): Promise<ToolResult>;
  click(target: ElementTarget, options?: Arguments): Promise<ToolResult>;
  typeText(text: string, index?: number): Promise<ToolResult>;
  pressKey(keys: string): Promise<ToolResult>;
  setValue(index: number, value: unknown): Promise<ToolResult>;
  selectText(index: number, text: string, options?: Arguments): Promise<ToolResult>;
  paste(text: string, format?: string): Promise<ToolResult>;
  inspect(index: number, options?: Arguments): Promise<ToolResult>;
  scroll(target: ElementTarget, direction: Direction, amount?: number): Promise<ToolResult>;
  drag(from: Point, to: Point): Promise<ToolResult>;
  performAction(index: number, action: string): Promise<ToolResult>;
  screenshot(): Promise<ToolResult>;
  batch(steps: Arguments[], options?: Arguments): Promise<ToolResult>;
  release(): Promise<ToolResult>;
}
export interface LocatorSpec extends Arguments {
  selector?: string;
  role?: string;
  name?: string;
  label?: string;
  text?: string;
  placeholder?: string;
  test_id?: string;
  frame?: string | Arguments;
  nth?: number;
}
export class Browser {
  constructor(client: SecondCursor, id: string);
  client: SecondCursor;
  id: string;
  call<T extends object = ToolResult>(tool: string, args?: Arguments, options?: RequestOptions): Promise<T>;
  command(operation: string, args?: Arguments): Promise<ToolResult>;
  tabs(): Promise<ToolResult>;
  userTabs(): Promise<ToolResult>;
  history(options?: Arguments): Promise<ToolResult>;
  open(url: string): Promise<Tab>;
  claim(id: number): Promise<Tab>;
  tab(id: number): Promise<Tab>;
}
export class Tab {
  constructor(browser: Browser, id: number);
  browser: Browser;
  id: number;
  observation: Observation | null;
  command(operation: string, args?: Arguments, options?: RequestOptions): Promise<ToolResult>;
  observe(options?: Arguments, requestOptions?: RequestOptions): Promise<Observation>;
  domSnapshot(): Promise<ToolResult>;
  locator(spec: string | LocatorSpec): Locator;
  getByRole(role: string, options?: Arguments & { name?: string }): Locator;
  getByLabel(label: string, options?: Arguments): Locator;
  getByText(text: string, options?: Arguments): Locator;
  getByPlaceholder(placeholder: string, options?: Arguments): Locator;
  getByTestId(testId: string): Locator;
  goto(url: string): Promise<ToolResult>;
  back(): Promise<ToolResult>;
  forward(): Promise<ToolResult>;
  reload(): Promise<ToolResult>;
  close(): Promise<ToolResult>;
  release(): Promise<ToolResult>;
  markDeliverable(): Promise<ToolResult>;
  markHandoff(): Promise<ToolResult>;
  screenshot(options?: Arguments): Promise<ToolResult>;
  logs(options?: Arguments): Promise<ToolResult>;
  viewport(options: Arguments): Promise<ToolResult>;
  dialog(options?: Arguments): Promise<ToolResult>;
  wait(options: Arguments): Promise<ToolResult>;
  export(format?: string): Promise<ToolResult>;
  assets(options?: Arguments): Promise<ToolResult>;
  cdp(method: string, params?: Arguments): Promise<ToolResult>;
  clipboard(action?: "read" | "write", text?: string): Promise<ToolResult>;
  batch(steps: Arguments[]): Promise<ToolResult>;
  click(point: Point, options?: Arguments): Promise<ToolResult>;
  hover(point: Point, options?: Arguments): Promise<ToolResult>;
  drag(from: Point, to: Point): Promise<ToolResult>;
  pressKey(keys: string): Promise<ToolResult>;
  typeText(text: string): Promise<ToolResult>;
}
export interface InspectResult extends ToolResult { text: string; attributes: Record<string, string>; }
export class Locator {
  constructor(tab: Tab, spec: LocatorSpec);
  tab: Tab;
  spec: LocatorSpec;
  command(operation: string, args?: Arguments): Promise<ToolResult>;
  frame(frame: string | Arguments): Locator;
  nth(nth: number): Locator;
  click(options?: Arguments): Promise<ToolResult>;
  dblclick(options?: Arguments): Promise<ToolResult>;
  hover(options?: Arguments): Promise<ToolResult>;
  fill(text: string): Promise<ToolResult>;
  type(text: string): Promise<ToolResult>;
  press(keys: string): Promise<ToolResult>;
  check(checked?: boolean): Promise<ToolResult>;
  uncheck(): Promise<ToolResult>;
  selectOption(value: string | string[]): Promise<ToolResult>;
  selectText(text: string, options?: Arguments): Promise<ToolResult>;
  paste(text: string, format?: string): Promise<ToolResult>;
  inspect(options?: Arguments): Promise<InspectResult>;
  innerText(): Promise<string>;
  getAttribute(name: string): Promise<string | null>;
  performAction(action: string): Promise<ToolResult>;
  setFiles(files: string | string[]): Promise<ToolResult>;
  waitFor(state?: string, timeout_ms?: number): Promise<ToolResult>;
  scroll(direction: Direction, amount?: number): Promise<ToolResult>;
}
