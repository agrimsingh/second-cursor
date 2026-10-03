// Compile-only consumer check: verifies the package export resolves its declaration.
import { SecondCursor } from '@second-cursor/client';
import { runAgent, fileCheckpointStore, WorkflowCheckpointError } from '@second-cursor/client/agent';
import type { AgentAdapter, AgentHistoryEntry, WorkflowTarget, CheckpointStore } from '@second-cursor/client/agent';
const client = new SecondCursor({ token: 'loaded-by-host' });
const targets: WorkflowTarget[] = [{ browserId: 'fixture', tabId: 7 }, { windowId: 42 }];
const store: CheckpointStore = fileCheckpointStore('/private/checkpoint.json', { maxBytes: 1_048_576 });
const adapter: AgentAdapter = { next({ task, history, progress }) {
  const prior: AgentHistoryEntry | undefined = history.at(-1);
  return { text: prior?.role === 'tool' ? task : String(progress ?? ''), calls: [] };
} };
void runAgent({ adapter, client, task: 'Finish the invoice', workflow: { id: 'invoice', store, targets }, onApproval: async call => call.name !== 'forbidden' }).then(result => {
  if (result.stopped === 'recovery_required') console.log(result.operationId);
});
const error: Error = new WorkflowCheckpointError('refused', 'CHECKPOINT_LOCKED');
void error;
// @ts-expect-error A native target requires a numeric window ID.
const invalid: WorkflowTarget = { windowId: '42' };
void invalid;
