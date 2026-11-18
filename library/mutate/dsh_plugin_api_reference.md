# DSH local plugin API reference

This reference is pinned to the DSH runtime images used by the AHE recipes:

- evolve-mutate-dsh:20261117-dsh021rc5-musl-cwfix-compaction — sha256:1d6ea1f8556470e793f681fe4d90f8abd9a8c73363b9bae419ae368631347b97
- evolve-mutate-dsh:20261117-dsh021rc5-cwfix-compaction — sha256:5962e1e9864f2176ce1c4429bf1b0d47d9e951d6f864aab65e85475a44cce606

Both images contain dsh-plugin-manager, dsh-tools, dsh-agent, and dsh-agent-loop version 0.2.1-rc.5. Verify image IDs, versions, API registration, schemas, execution, and event names with scripts/verify_dsh_plugin_api.sh. The script runs offline, with networking disabled.

## Local plugin contract

The candidate profile is a Cordis profile patch over the installed sdk-minimal bundle. Add a local plugin with an insert row:

~~yaml
- insert:
    - id: my-plugin
      name: ./plugins/my-plugin.mjs
~~

The rollout driver resolves the relative path before DSH loads the profile. A plugin module exports a stable name, an optional inject list, and apply(ctx, config):

~~js
export const name = 'my-plugin';
export const inject = ['tools'];

export function apply(ctx, config) {
  // Register through the injected context. Return disposers for owned hooks.
}
~~

Do not import Cordis or DSH packages from the candidate plugin. Runtime module resolution for external plugins is not guaranteed; use injected services on ctx. The seed-probe plugin under seeds/dsh-harbor/plugins is the repository's minimal local-plugin example. This headless runtime does not load Host/inspector capabilities, so cordis_inspect_*, cordis_define, and cordis_run are unavailable.

## Tool registration

ctx.tools.register(definition) registers a tool in the current context/scope and returns a disposer that unregisters it. The required shape includes name, description, parameters, output, and execute. output must provide a JSON Schema in schema and a render(args, value) function returning content blocks.

Use ordinary JSON Schema for parameters and output. The runtime enforces a strict subset: type, oneOf, properties, required, additionalProperties, items, enum, and const, plus annotation fields. Unsupported keywords reject. Object schemas should declare required fields and additionalProperties deliberately.

~~js
ctx.tools.register({
  name: 'summarize_status',
  description: 'Return a short status summary.',
  parameters: {
    type: 'object',
    properties: { path: { type: 'string' } },
    required: ['path'],
    additionalProperties: false
  },
  output: {
    schema: { type: 'string' },
    render: (_args, value) => [{ type: 'text', text: value }]
  },
  async execute(args, exec) {
    if (exec.signal.aborted) throw new Error('cancelled');
    return 'checked: ' + args.path;
  }
});
~~

Keep execution bounded, honor exec.signal, and validate the behavior through the pinned-image smoke before relying on a plugin in a benchmark. dsh-tools also exports defineTool for first-party packages; a local candidate plugin should not add dependencies merely to use that helper.

## Hooks and lifecycle

The installed dsh-agent implementation contains these event names:

- Agent-scoped waterfalls: system-prompt/assemble, agent/request, and agent/pre-step.
- Lifecycle cleanup: agent/disposed.
- Workspace interaction: workspace/session-activity and workspace/session-stop.

These names are not interchangeable event modes. The waterfall listeners must preserve and call next() according to the event contract; cleanup listeners must release resources they own. Confirm the event's context scope and payload in the pinned runtime source before using it. A global plugin context and an agent-scoped context do not observe the same events.

## Source of truth and drift check

The exact installed implementation is inside the pinned image at:

- /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-tools
- /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-plugin-manager
- /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-agent
- /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-agent-loop

Run scripts/verify_dsh_plugin_api.sh after changing runtime images. If an image ID, package version, hook name, or smoke assertion changes, update this reference from the installed image and rerun the smoke. This reference and the prompt excerpt guide future mutate sessions; existing initialized workspaces keep their frozen operator copies.
