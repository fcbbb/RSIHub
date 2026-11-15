#!/usr/bin/env node
/**
 * RSIHub build patch: dsh's DeepSeek Messages API-key adapter authenticates
 * with `x-api-key` only. Gateways that require `Authorization: Bearer`
 * (for example the shared vLLM endpoint behind /api/inference/v1) reject that
 * header with 401. Send both headers instead; targets that ignore one keep
 * working.
 *
 * Usage: node patch-api-key-auth.cjs <path/to/dsh-llm-deepseek-api-key/lib/index.js>
 */
"use strict";

const fs = require("node:fs");

const target = process.argv[2];
if (!target) {
  console.error("usage: patch-api-key-auth.cjs <api-key module index.js>");
  process.exit(2);
}

const substitutions = [
  [
    'resolveAuth: async (connection) => ({ headers: { "x-api-key": await resolveApiKey(connection) } }),',
    'resolveAuth: async (connection) => { const key = await resolveApiKey(connection); return { headers: { "x-api-key": key, Authorization: "Bearer " + key } }; },',
  ],
  [
    'load: (url, apiKey, signal) => requestModelListing(url, { "x-api-key": apiKey }, signal),',
    'load: (url, apiKey, signal) => requestModelListing(url, { "x-api-key": apiKey, Authorization: "Bearer " + apiKey }, signal),',
  ],
];

let source = fs.readFileSync(target, "utf8");
for (const [from, to] of substitutions) {
  if (!source.includes(from)) {
    console.error(`patch-api-key-auth: expected source not found in ${target}:\n  ${from}`);
    process.exit(1);
  }
  source = source.replace(from, to);
}
fs.writeFileSync(target, source);
console.error(`patch-api-key-auth: added Authorization: Bearer to ${target}`);