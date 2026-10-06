import type { Arguments, SecondCursor, ToolResult, OperationStatus } from './index.js';

export interface AgentCall { id: string; name: string; arguments?: Arguments; }
export interface AgentTurn { text?: string; calls?: AgentCall[]; /** Only current host-supplied choices are accepted in contract mode. */ choice?: string; handoff?: 'needs_input' | 'needs_agent'; /** Host/model progress notes; never evidence that an action executed. */ progress?: unknown; }
export type AgentHistoryEntry = (AgentTurn & { role: 'assistant' }) | {
  role: 'tool'; id: string; name: string; result?: ToolResult; error?: string;
  operation_id?: string; recovered?: boolean; discarded?: boolean;
};
export interface AgentAdapter {
  next(input: AgentContext & { tools: unknown; decisionSpace?: DecisionSpace }): AgentTurn | Promise<AgentTurn>;
}
/** Optional provider-independent subtask boundary. Bump id when host choice/verifier semantics change. */
export interface AgentContract {
  id: string;
  /** Caller-authorized literal strings. The adapter selects a choice; it cannot supply replacement values. */
  inputs: Record<string, string>;
  criteria: string[];
  /** Total selected calls across durable resumes, including observation calls. */
  maxActions: number;
  /** Consecutive acted-on states with the same host-owned semantic key. Default 3. */
  maxNoProgress?: number;
  /** Consecutive known failures of the same call. Default 3. */
  maxRepeatedFailures?: number;
}
export interface AgentContext {
  task: string; history: AgentHistoryEntry[]; progress?: unknown; contract?: AgentContract;
}
export interface DecisionChoice {
  id: string; label: string;
  /** Observations consume the call budget but do not count as unsuccessful progress. */
  kind: 'act' | 'observe';
  /** Host-owned concrete call, bound to the current observation/target by runtime freshness fields. */
  call: Omit<AgentCall, 'id'>;
  /** Top-level argument names mapped to keys in contract.inputs; overrides any host default value. */
  inputBindings?: Record<string, string>;
}
export interface DecisionSpace {
  /** Task-semantic state: exclude timestamps, snapshot IDs, cursor blink and model-written progress notes. */
  key: string;
  observation?: unknown;
  choices: DecisionChoice[];
}
export interface CompletionVerification { complete: boolean; evidence?: unknown; reason?: string; }
export interface ContractControl {
  actions: number; noProgress: number; repeatedFailures: number;
  lastFailure: string | null; lastObservationKey: string | null; awaitingObservation: boolean;
}
export type WorkflowTarget = { windowId: number } | { browserId: string; tabId: number };
/** Durable records contain task and UI content. Use a private location and your own retention policy. */
export interface WorkflowCheckpoint {
  version: 1;
  id: string; task: string; endpoint: string; transport: 'http' | 'mcp';
  sessionId: string; taskId: string; initialTargets: WorkflowTarget[]; targets: WorkflowTarget[];
  status: 'active' | 'completed'; step: number; completedCalls: number;
  history: AgentHistoryEntry[]; queue: AgentCall[];
  pending: { call: AgentCall; operationId: string; kind?: 'act' | 'observe' } | null;
  progress: unknown; createdAt: string; updatedAt: string; result?: { text?: string; verification?: CompletionVerification };
  contract?: AgentContract; control?: ContractControl; verificationRequired?: true;
}
export interface CheckpointLease {
  load(): Promise<WorkflowCheckpoint | null>;
  /** Must durably commit the whole record before resolving. Failures must reject. */
  save(state: WorkflowCheckpoint): Promise<void>;
  release(): Promise<void>;
}
export interface CheckpointStore {
  /** Hold an exclusive lease for the entire run. Never grant two simultaneous writers. */
  acquire(): Promise<CheckpointLease>;
}
export class WorkflowCheckpointError extends Error {
  constructor(message: string, code?: string, options?: ErrorOptions);
  code: string;
}
export function fileCheckpointStore(path: string, options?: { maxBytes?: number }): CheckpointStore;
export interface AgentOptions {
  adapter: AgentAdapter;
  /** Supply an unconnected client or one using the checkpoint's original session. */
  client: SecondCursor;
  task: string;
  /** Maximum new model turns per invocation, including after a resumed checkpoint. */
  maxSteps?: number;
  onApproval?: (call: AgentCall) => boolean | Promise<boolean>;
  onTurn?: (turn: AgentTurn) => unknown | Promise<unknown>;
  contract?: AgentContract;
  /** Required with contract. Read-only host hook; rebuild choices from fresh observations every turn. */
  getDecisionSpace?: (context: AgentContext) => DecisionSpace | Promise<DecisionSpace>;
  /** Required with contract; also available independently. Reads independent task evidence, never model assertions. */
  verifyCompletion?: (context: AgentContext & { reason: 'completion' | 'no_progress' | 'repeated_failure' | 'action_limit' }) => CompletionVerification | Promise<CompletionVerification>;
  workflow?: {
    id: string; store: CheckpointStore; targets: WorkflowTarget[];
    /** Optional expected runtime task identity. The loop never resumes paused/interrupted tasks. */
    taskId?: string;
  };
}
export interface AgentResult {
  text?: string;
  history: AgentHistoryEntry[];
  progress?: unknown;
  control?: ContractControl;
  verification?: CompletionVerification;
  stopped?: 'step_limit' | 'recovery_required' | 'task_paused' | 'task_needs_user' | 'task_interrupted' | 'task_completed' | 'task_failed' | 'task_cancelled' | 'needs_input' | 'needs_agent' | 'no_progress' | 'repeated_failure' | 'action_limit' | 'completion_unverified' | 'invalid_choice';
  operationId?: string;
  reason?: string;
  operation?: OperationStatus;
  error?: string;
  result?: ToolResult;
}
export function runAgent(options: AgentOptions): Promise<AgentResult>;
