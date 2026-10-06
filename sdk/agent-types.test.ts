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

void runAgent({
  adapter: { next({ decisionSpace, contract }) {
    if (decisionSpace?.choices.length) return { choice: decisionSpace.choices[0].id };
    return { handoff: 'needs_input', text: String(contract?.criteria) };
  } }, client, task: 'Save the supplied note',
  contract: { id: 'save-note-v1', inputs: { note: 'Exact note' }, criteria: ['A persisted note exists'], maxActions: 8 },
  getDecisionSpace: async () => ({ key: 'saved=false', choices: [{
    id: 'type-note', label: 'Type the supplied note', kind: 'act',
    call: { name: 'type_text', arguments: { window_id: 42 } }, inputBindings: { text: 'note' },
  }] }),
  verifyCompletion: async ({ reason }) => ({ complete: false, evidence: { reason } }),
}).then(result => { if (result.stopped === 'no_progress') console.log(result.control?.actions); });
