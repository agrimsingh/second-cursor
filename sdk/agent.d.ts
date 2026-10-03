import type { Arguments, SecondCursor, ToolResult, OperationStatus } from './index.js';

export interface AgentCall { id: string; name: string; arguments?: Arguments; }
export interface AgentTurn { text?: string; calls?: AgentCall[]; /** Host/model progress notes; never evidence that an action executed. */ progress?: unknown; }
export type AgentHistoryEntry = (AgentTurn & { role: 'assistant' }) | {
  role: 'tool'; id: string; name: string; result?: ToolResult; error?: string;
  operation_id?: string; recovered?: boolean; discarded?: boolean;
};
export interface AgentAdapter {
  next(input: { task: string; tools: unknown; history: AgentHistoryEntry[]; progress?: unknown }): AgentTurn | Promise<AgentTurn>;
}
export type WorkflowTarget = { windowId: number } | { browserId: string; tabId: number };
/** Durable records contain task and UI content. Use a private location and your own retention policy. */
export interface WorkflowCheckpoint {
  version: 1;
  id: string; task: string; endpoint: string; transport: 'http' | 'mcp';
  sessionId: string; taskId: string; initialTargets: WorkflowTarget[]; targets: WorkflowTarget[];
  status: 'active' | 'completed'; step: number; completedCalls: number;
  history: AgentHistoryEntry[]; queue: AgentCall[];
  pending: { call: AgentCall; operationId: string } | null;
  progress: unknown; createdAt: string; updatedAt: string; result?: { text?: string };
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
  stopped?: 'step_limit' | 'recovery_required' | 'task_paused' | 'task_needs_user' | 'task_interrupted' | 'task_completed' | 'task_failed' | 'task_cancelled';
  operationId?: string;
  reason?: string;
  operation?: OperationStatus;
  error?: string;
  result?: ToolResult;
}
export function runAgent(options: AgentOptions): Promise<AgentResult>;
