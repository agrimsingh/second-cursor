/** Optional host-independent loop. The adapter owns provider-specific message/image encoding.
 * adapter.next({task, tools, history}) -> {text?, calls?:[{id,name,arguments}]}.
 * onApproval can apply the host's semantic policy before a tool changes external state.
 * Execution remains in Second Cursor; no model-generated JavaScript is evaluated here.
 */
export async function runAgent({
  adapter,
  client,
  task,
  maxSteps = 50,
  onApproval,
  onTurn,
}) {
  if (!adapter?.next) throw new Error("adapter.next is required");
  const response = await fetch(client.url + "/api", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${client.token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ tool: "tools/list" }),
  });
  if (!response.ok) throw new Error("Could not load tool definitions");
  const tools = await response.json(),
    history = [];
  for (let step = 0; step < maxSteps; step++) {
    const turn = await adapter.next({ task, tools, history });
    await onTurn?.(turn);
    history.push({ role: "assistant", ...turn });
    if (!turn.calls?.length) return { text: turn.text, history };
    // Preserve the model's order; batch tools serialize steps but do not roll back effects.
    for (const call of turn.calls) {
      try {
        if (onApproval && !(await onApproval(call)))
          throw new Error("Host denied this action");
        const result = await client.call(call.name, call.arguments ?? {});
        history.push({ role: "tool", id: call.id, name: call.name, result });
      } catch (error) {
        history.push({
          role: "tool",
          id: call.id,
          name: call.name,
          error: error.message,
          operation_id: error.operationId,
        });
      }
    }
  }
  return { stopped: "step_limit", history };
}
