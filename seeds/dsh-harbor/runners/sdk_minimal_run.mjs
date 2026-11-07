/**
 * One-shot driver for `dsh --profile sdk-minimal` running INSIDE the task
 * container. The host-side Harbor adapter copies this file, the candidate
 * tree, and the materialized cordis patches into the container, then runs:
 *
 *   node /tmp/evolve-sdk-run.mjs
 *
 * The dsh runtime therefore owns the model loop and the bash tool locally in
 * the task container. No host process participates in tool execution and no
 * docker-exec shell bridge is involved (parity with installed Codex/MiniSWE
 * agents).
 *
 * Wire protocol: newline-delimited JSON-RPC 2.0 (dsh-sdk-protocol).
 *   initialize -> session/prompt -> session/wait -> shutdown
 *
 * Environment:
 *   DSH_DBIN              path to dsh lib/bin.js
 *   DSH_PATCHES           colon-separated --patch files, applied in order
 *   DSH_CWD               session working directory (task container cwd)
 *   DSH_PROVIDER          provider route (default deepseek-official)
 *   DSH_MODEL             model id (from the evaluator's pinned model)
 *   DSH_CONTEXT_WINDOW    optional adapter fallback capacity
 *   DSH_SESSION_ID        session id (default one-shot uuid)
 *   DSH_TASK_FILE         instruction file
 *   DSH_FINAL_FILE        where to write the final assistant text
 *   DSH_EVENTS_FILE       optional JSONL copy of session events
 *   DSH_RUN_TIMEOUT_SEC   overall turn budget (default 1800)
 */
import { spawn } from "node:child_process";
import { createWriteStream, readFileSync, writeFileSync, writeSync } from "node:fs";
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";

const env = process.env;
const dbin = env.DSH_DBIN;
const cwd = env.DSH_CWD || "/app";
const model = env.DSH_MODEL || "deepseek-v4-flash";
const provider = env.DSH_PROVIDER || "deepseek-official";
const sessionId = env.DSH_SESSION_ID || `task-${randomUUID()}`;
const taskFile = env.DSH_TASK_FILE;
const finalFile = env.DSH_FINAL_FILE;
const eventsFile = env.DSH_EVENTS_FILE;
const timeoutSec = Number(env.DSH_RUN_TIMEOUT_SEC || "1800");
const patches = (env.DSH_PATCHES || "").split(":").filter(Boolean);

if (!dbin) {
  console.error("DSH_DBIN is required");
  process.exit(64);
}
if (!taskFile) {
  console.error("DSH_TASK_FILE is required");
  process.exit(64);
}
// dsh's lib/bin.js self-dispatches through `import.meta.main`, which older
// Node releases leave undefined: the child then exits 0 without reading the
// JSON-RPC stream and the await below never settles (Node exit 13). Same test,
// same runtime, so this catches exactly what the child would hit.
if (import.meta.main === undefined) {
  console.error(
    `Node ${process.versions.node} does not expose import.meta.main; dsh needs >= 22.19 (or >= 24.2)`,
  );
  process.exit(64);
}

const instruction = readFileSync(taskFile, "utf8");
const args = [dbin, "--profile", "sdk-minimal"];
for (const patch of patches) args.push("--patch", patch);

const child = spawn(process.execPath, args, {
  cwd,
  stdio: ["pipe", "pipe", "inherit"],
  env: { ...env, DSH_HOME: env.DSH_HOME || "/tmp/evolve-dsh-home" },
});

let nextId = 0;
const pending = new Map();
let idleResolve = null;
let idle = false;
let turnEnd = null;
let lastAssistant = "";
let finishing = false;
const events = eventsFile ? createWriteStream(eventsFile) : null;

function writeFrame(frame) {
  child.stdin.write(`${JSON.stringify(frame)}\n`);
}

function request(method, params) {
  const id = ++nextId;
  const frame = { jsonrpc: "2.0", id, method };
  if (params !== undefined) frame.params = params;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject, method });
    writeFrame(frame);
  });
}

function textOfBlocks(blocks) {
  return (blocks || [])
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("");
}

const lines = createInterface({ input: child.stdout });
lines.on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch {
    console.error(`non-JSON stdout: ${trimmed.slice(0, 300)}`);
    return;
  }
  if (msg.id !== undefined && msg.method === undefined) {
    const entry = pending.get(msg.id);
    if (!entry) return;
    pending.delete(msg.id);
    if (msg.error) entry.reject(new Error(`${entry.method}: ${JSON.stringify(msg.error)}`));
    else entry.resolve(msg.result);
    return;
  }
  if (msg.method === "session.event") {
    const event = msg.params?.event;
    if (!event) return;
    if (events) events.write(`${JSON.stringify(event)}\n`);
    if (event.type === "assistant/message") {
      const text = textOfBlocks(event.data?.message?.content);
      if (text) lastAssistant = text;
    } else if (event.type === "turn/end") {
      turnEnd = event.data?.reason ?? null;
    }
  } else if (msg.method === "session.status") {
    if (msg.params?.status === "idle" && idleResolve) {
      idle = true;
      const resolve = idleResolve;
      idleResolve = null;
      resolve();
    }
  }
});

function waitForIdle() {
  if (idle) return Promise.resolve();
  return new Promise((resolve) => {
    idleResolve = resolve;
  });
}

function finish(code) {
  if (finishing) return;
  finishing = true;
  if (events) events.end();
  try {
    if (finalFile !== undefined) writeFileSync(finalFile, lastAssistant);
  } catch (error) {
    console.error(`cannot write final response: ${error}`);
  }
  // stderr/stdout are pipes under Harbor, so process.exit() can drop queued
  // console writes. Diagnostics that explain the exit code must be sync.
  if (lastAssistant) writeSync(1, `${lastAssistant}\n`);
  child.stdin.end();
  child.kill("SIGTERM");
  // finish() is also called from the child's own exit handler; waiting for an
  // exit event that already fired would stall for the hard-exit timeout.
  if (child.exitCode !== null || child.signalCode !== null) process.exit(code);
  // Keep the loop alive until the child is gone, then escalate. Without the
  // ref'd fallback a dsh that ignores SIGTERM would hang the Harbor agent
  // until the outer agent timeout instead of reporting this runner's code.
  const hardExit = setTimeout(() => {
    child.kill("SIGKILL");
    process.exit(code);
  }, 2000);
  child.once("exit", () => {
    clearTimeout(hardExit);
    process.exit(code);
  });
}

const timer = setTimeout(() => {
  writeSync(2, `dsh turn exceeded ${timeoutSec}s\n`);
  finish(124);
}, timeoutSec * 1000);
timer.unref();

child.on("exit", (code, signal) => {
  if (finishing || turnEnd !== null) return;
  // A silent exit 0 means dsh never handled `initialize` (on Node < 22.19 its
  // entry point is a no-op); report it instead of leaving the await unsettled.
  if (code === 0) {
    writeSync(2, "dsh runtime exited 0 before turn end without answering initialize\n");
    finish(70);
    return;
  }
  writeSync(2, `dsh runtime exited ${code ?? signal} before turn end\n`);
  finish(code || 70);
});

try {
  await request("initialize", { cwd, provider, model, maxTokens: Number(env.DSH_MAX_TOKENS || "49152") });
  await request("session/prompt", {
    sessionId,
    contentBlocks: [{ type: "text", text: instruction }],
  });
  await waitForIdle();
  await request("session/wait", { sessionId });
  await request("shutdown");
  clearTimeout(timer);
  const reason = turnEnd?.kind;
  if (reason && reason !== "completed") {
    writeSync(2, `turn ended with reason ${JSON.stringify(turnEnd)}\n`);
    finish(2);
  } else {
    finish(0);
  }
} catch (error) {
  writeSync(2, `dsh sdk-minimal run failed: ${error?.stack || error}\n`);
  clearTimeout(timer);
  finish(3);
}