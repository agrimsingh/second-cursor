/**
 * Jev (TypeSafe AI) typed decisions for scripts an agent writes during e2e/jev_triage.py. Dependency-free.
 * Key: TYPESAFE_API_KEY, or ~/.config/typesafe/key. Every call is appended to jev-calls.jsonl beside this file.
 */
import { appendFileSync, readFileSync } from "node:fs";
import { homedir } from "node:os";

const ENDPOINT = "https://api.typesafe.ai/v1/systemone";
const MODEL = "jev-1.13.0";
const LOG = new URL("./jev-calls.jsonl", import.meta.url);
// A decision normally takes ~0.3 s; early access occasionally stalls a request for ~30 s, so give up and resend.
const TIMEOUT_MS = 3000;
const ATTEMPTS = 4;

const key = () =>
  process.env.TYPESAFE_API_KEY || readFileSync(`${homedir()}/.config/typesafe/key`, "utf8").trim();

export const choice = (instructions, criteria) => ({ type: "choice", instructions, criteria });
export const score = (instructions, criteria) => ({ type: "score", instructions, criteria });
export const noul = (instructions) => ({ type: "noul", instructions });

export async function decide(state, questions) {
  const body = JSON.stringify({ model: MODEL, state, questions });
  const started = Date.now();
  for (let attempt = 1; ; attempt++) {
    let response;
    try {
      response = await fetch(ENDPOINT, {
        method: "POST",
        body,
        signal: AbortSignal.timeout(TIMEOUT_MS),
        headers: { Authorization: `Bearer ${key()}`, "Content-Type": "application/json" },
      });
    } catch (error) {
      if (error.name === "TimeoutError" && attempt < ATTEMPTS) continue;
      throw error;
    }
    if ((response.status === 429 || response.status >= 500) && attempt < ATTEMPTS) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      continue;
    }
    const reply = await response.json();
    if (!response.ok) throw new Error(`Jev HTTP ${response.status}: ${JSON.stringify(reply).slice(0, 300)}`);
    appendFileSync(
      LOG,
      JSON.stringify({ ms: Date.now() - started, attempts: attempt, model: reply.model, usage: reply.usage, state,
                       questions, answers: reply.answers }) + "\n",
    );
    return reply.answers;
  }
}
