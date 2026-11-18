import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const packageRoot = '/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai';
const packageNames = ['dsh-plugin-manager', 'dsh-tools', 'dsh-agent', 'dsh-agent-loop'];
const versions = {};
for (const name of packageNames) {
  const manifestPath = path.join(packageRoot, name, 'package.json');
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  versions[name] = manifest.version;
  assert.equal(manifest.version, '0.2.1-rc.5', name + ' version drift');
}

const toolsApiPath = path.join(packageRoot, 'dsh-tools/lib/index.js');
const toolsApi = await import(pathToFileURL(toolsApiPath).href);
assert.equal(typeof toolsApi.assertSupportedJsonSchema, 'function');
assert.equal(typeof toolsApi.defineTool, 'function');

const agentSource = await fs.readFile(path.join(packageRoot, 'dsh-agent/lib/index.js'), 'utf8');
const expectedEvents = [
  'system-prompt/assemble',
  'agent/request',
  'agent/pre-step',
  'agent/disposed',
  'workspace/session-activity',
  'workspace/session-stop'
];
for (const eventName of expectedEvents) {
  assert.ok(agentSource.includes(eventName), 'missing runtime event: ' + eventName);
}

const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'dsh-plugin-smoke-'));
const pluginPath = path.join(tempRoot, 'fixture.mjs');
const nl = String.fromCharCode(10);
const pluginSource = [
  "export const name = 'rsi-dsh-api-smoke';",
  "export const inject = ['tools'];",
  'export function apply(ctx, config) {',
  '  return ctx.tools.register({',
  "    name: 'rsi_api_smoke_echo',",
  "    description: 'Offline DSH plugin API smoke tool.',",
  '    parameters: {',
  "      type: 'object',",
  "      properties: { text: { type: 'string' } },",
  "      required: ['text'],",
  '      additionalProperties: false',
  '    },',
  '    output: {',
  "      schema: { type: 'string' },",
  "      render: (_args, value) => [{ type: 'text', text: value }]",
  '    },',
  '    async execute(args, exec) {',
  "      if (exec.signal.aborted) throw new Error('aborted');",
  "      return config.prefix + args.text;",
  '    }',
  '  });',
  '}'
].join(nl);

try {
  await fs.writeFile(pluginPath, pluginSource);
  const plugin = await import(pathToFileURL(pluginPath).href);
  assert.equal(plugin.name, 'rsi-dsh-api-smoke');
  assert.deepEqual(plugin.inject, ['tools']);

  const registered = new Map();
  const ctx = {
    tools: {
      register(definition) {
        assert.equal(typeof definition.name, 'string');
        assert.equal(typeof definition.description, 'string');
        assert.equal(typeof definition.output.render, 'function');
        toolsApi.assertSupportedJsonSchema(definition.parameters);
        toolsApi.assertSupportedJsonSchema(definition.output.schema);
        registered.set(definition.name, definition);
        return () => registered.delete(definition.name);
      }
    }
  };

  const dispose = plugin.apply(ctx, { prefix: 'ok:' });
  const tool = registered.get('rsi_api_smoke_echo');
  assert.ok(tool, 'plugin did not register its tool');
  const value = await tool.execute({ text: 'trace' }, { signal: new AbortController().signal });
  assert.equal(value, 'ok:trace');
  assert.deepEqual(tool.output.render({ text: 'trace' }, value), [{ type: 'text', text: 'ok:trace' }]);
  dispose();
  assert.equal(registered.size, 0, 'plugin disposer did not unregister its tool');

  console.log(JSON.stringify({
    status: 'passed',
    versions,
    checkedEvents: expectedEvents,
    checks: ['ESM plugin exports', 'ctx.tools.register contract', 'parameter/output schemas', 'tool execution', 'rendering', 'disposal']
  }, null, 2));
} finally {
  await fs.rm(tempRoot, { recursive: true, force: true });
}
