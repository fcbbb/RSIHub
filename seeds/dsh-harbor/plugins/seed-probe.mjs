/**
 * seed-probe: gen-0's wiring-proof plugin. Being loaded is its only job; it
 * registers no behavior.
 *
 * Rules for writing new plugins (important):
 * - Pure ESM, exporting { name, inject?, apply(ctx, config) }.
 * - Never import '@deepseek-ai/cordis' or any dsh package — external plugins
 *   sharing the runtime's cordis instance is unresolved; take every capability
 *   from the ctx that apply() receives.
 * - List the services you need in the inject array (e.g. ['tools']); cordis
 *   guarantees they are injected before apply runs.
 * - To explore the available API, read the installed `@deepseek-ai/dsh-*`
 *   packages in the runtime tree. The mutation session runs headless and
 *   mounts no Host or inspector plugins, so `cordis_inspect_*`,
 *   `cordis_define`, and `cordis_run` are unavailable here.
 * - At rollout time this plugin loads inside the Harbor task container, in the
 *   same process as the candidate's local bash tool.
 */
export const name = 'evolve-seed-probe'

export function apply(ctx) {
  // Intentionally empty: proves the profile repo's local-plugin channel works.
}
