# Optional bounded SDK subtasks

`@second-cursor/client/agent` can constrain a subtask to fresh host-supplied actions and exact literal inputs. The adapter still owns the model connection. This adds no provider dependency and does not assume an MCP server can access its client's model.

```js
import { runAgent } from '@second-cursor/client/agent';

const result = await runAgent({
  client,
  task: 'Save the supplied note in the selected fixture document',
  contract: {
    id: 'save-note-v1', // Bump when choice construction or verifier semantics change.
    inputs: { note: 'The exact caller-authorized note' },
    criteria: ['The selected document contains the supplied note on disk'],
    maxActions: 12,
    maxNoProgress: 3,
    maxRepeatedFailures: 3,
  },
  async getDecisionSpace({ contract }) {
    const state = await readFreshFixtureState(client); // Host-owned, read-only helper.
    return {
      // Task state, not snapshot IDs, timestamps, blink, or model progress notes.
      key: JSON.stringify([state.documentId, state.text, state.saved, state.modal]),
      observation: state.summary,
      choices: state.editable ? [{
        id: 'enter-note', label: 'Enter the supplied note', kind: 'act',
        call: { name: 'type_text', arguments: {
          window_id: state.windowId, element_index: state.editorIndex,
          observation_id: state.snapshotId,
        } },
        inputBindings: { text: 'note' },
      }, ...state.otherPermittedChoices] : state.otherPermittedChoices,
    };
  },
  adapter: {
    async next(context) {
      // Provider-specific code receives context.contract and context.decisionSpace.
      // Return { choice: 'enter-note' }, { text: 'Done' }, or a handoff:
      // { handoff: 'needs_input', text: 'The requested destination is missing' }.
      return askYourConfiguredModel(context);
    },
  },
  async verifyCompletion({ contract, reason }) {
    const savedText = await readPersistedFixtureDocument(); // Independent evidence.
    return { complete: savedText === contract.inputs.note,
      evidence: { savedText, reason } };
  },
  // Optional: the existing workflow object adds operation reconciliation and
  // checkpoints. Its private checkpoint contains inputs, UI data and evidence.
  // workflow: { id: 'note-job', targets: [{ windowId: 42 }], store },
});
```

The helper functions above are application-specific: the SDK does not infer which controls are authorized or build a verifier from model assertions. A host must construct concrete choices from the current observation, retain runtime freshness fields, and only offer calls it permits. `inputBindings` maps top-level argument names to supplied input keys, replacing host defaults. The adapter can select one offered choice per turn; arbitrary calls, invented choice IDs, and combinations of a choice with a handoff are rejected before dispatch. Choice arguments are cloned before reaching the adapter and approval hook.

`kind: 'observe'` consumes the call budget but does not count as a failed attempt to change the task state. `kind: 'act'` causes the next semantic key to be compared with the previous one. Three unchanged acted-on states normally return `no_progress`; three consecutive known failures of the same call return `repeated_failure`. These guards hand control back rather than retrying an uncertain operation. A changed semantic key resets the no-progress counter. Hosts should use a key that captures meaningful progress, including persisted receipts when available, rather than unstable IDs or only pixels.

Before a no-progress, repeated-failure, or action-limit handback, the SDK asks the verifier whether the task actually finished. This lets a quiet successful save complete even when the visible interface stayed unchanged. A no-call model turn is also only a completion proposal: false, malformed, or throwing verification returns `completion_unverified`, and the durable checkpoint stays active. Successful verification evidence is included in the result and completed checkpoint. Hooks must read state rather than perform mutations; the SDK does not journal arbitrary work inside a hook.

All selected calls count toward `maxActions`, including reads and known failures. Recovery observations are host work, not model-selected calls. Durable checkpoints preserve budgets, literals, contract identity, and the requirement for verification across resumes. Pending operations are reconciled first; an unknown or uncertain result stops with `recovery_required` before model work or verification. Recovered operations count once and are never replayed. Completed workflows return their stored result without re-running verification; start a new workflow to check a later state. Changing contract inputs, budgets, or ID requires a new workflow. The host is responsible for preserving or versioning callback semantics.

The bounds are on action count, not wall time. Model requests, host callbacks, and read-only observations need the host's own cancellation/deadline policy. Existing `maxSteps` remains a per-invocation model-turn limit; it also bounds repeated host denials. Omit `contract` to retain the free-form API. `verifyCompletion` can also be used on its own to require evidence at completion without constraining action choices.

## Compare with the original SDK

`python3 e2e/arc_agent_model.py --list` previews the experiment without launching apps or making model requests. A real run uses the same candidate runtime for both SDK modes, fresh Sol 6.1 high conversations, independently judged messy invoice fixtures, and reversed arm order on the second seed:

```sh
SC_E2E_APP=build/arc-eval/candidate/SecondCursor.app \
SC_E2E_EXTENSION=build/arc-eval/candidate/extension \
python3 e2e/arc_agent_model.py --takeover --seed 301 --rounds 2 \
  --baseline-sdk build/arc-eval/baseline/sdk
```

Preserve the original SDK directory before changing it. The run fingerprints both SDKs, the loaded app/extension and its fixture/host sources. The model derives form values from observed context; an independent ledger judge checks correctness, exactly one commit and unchanged unrelated records. This track has no native coexistence or injected recovery faults; use `e2e/cross_app_model.py` for those. Run GUI suites serially. See the [matched verification record](verification-arc-adaptations-2026-10-06.md) for measurements and their limits.
