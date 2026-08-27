// gui/weighbridge.js — weigh the load, then pick a model that can carry it.
//
// Two problems this solves, both observed in this harness:
//
//  1. **Tools handed to models that cannot use them.** qwen3:0.6b was given 16
//     tool schemas and lost the system prompt among them — it answered "what is
//     my name" without the name that was in the first line of its own context.
//     llama3.2:1b emitted a tool call with malformed arguments. Capability is
//     not uniform, so the tool list should not be either.
//
//  2. **No way to escalate.** A small model that cannot do the job had only two
//     options: produce something wrong, or refuse. Now it can hand the task to
//     a stronger model, and the harness can pre-empt that by weighing the task
//     before it starts.
//
// The name is literal: a weighbridge weighs the load and decides whether the
// vehicle can carry it.

// ── capability tiers ─────────────────────────────────────────────────────
// Drawn from parameter count and Ollama's reported capabilities. The
// boundaries are where behaviour actually changed in testing, not round
// numbers: below ~1.5B tool arguments start coming back malformed, and below
// ~3B a long system prompt stops surviving alongside a large tool list.
export const TIERS = { none: 0, light: 1, standard: 2, strong: 3 };

export function tierFor({ paramBillions, capabilities = [] } = {}) {
  if (!capabilities.includes('tools')) return TIERS.none;
  if (paramBillions == null) return TIERS.light;   // unknown: assume the worst
  if (paramBillions < 1.5) return TIERS.light;
  if (paramBillions < 3.5) return TIERS.standard;
  return TIERS.strong;
}

export function tierName(tier) {
  return Object.keys(TIERS).find(key => TIERS[key] === tier) || 'unknown';
}

// ── which tools a tier may be given ──────────────────────────────────────
// Minimum tier per tool. Anything unlisted defaults to `standard`: a new tool
// is assumed non-trivial until someone decides otherwise, which fails safe.
//
// `light` gets tools whose arguments are a single obvious string, or none at
// all. It does NOT get anything destructive, anything with structured or
// multi-field arguments, or anything whose misuse is expensive.
export const TOOL_MIN_TIER = {
  // Zero or one trivial argument — safe for a small model to get right.
  date_time: TIERS.light,
  read_file: TIERS.light,
  list_dir: TIERS.light,
  tool_search: TIERS.light,
  web_search: TIERS.light,
  scrape_url: TIERS.light,
  memory_search: TIERS.light,
  memory_list: TIERS.light,
  sandbox_status: TIERS.light,
  github_status: TIERS.light,

  // Structured arguments, or a mistake costs something.
  search_files: TIERS.standard,
  find_files: TIERS.standard,
  write_file: TIERS.standard,
  append_file: TIERS.standard,
  memory_write: TIERS.standard,
  fetch_data: TIERS.standard,
  sandbox_verify: TIERS.standard,
  create_display: TIERS.standard,

  // Exact-match edits, shell, promotion to real files, driving a browser or
  // the machine itself. A model that fumbles arguments must not have these.
  edit_file: TIERS.strong,
  run_shell: TIERS.strong,
  sandbox_promote: TIERS.strong,
  sandbox_discard: TIERS.strong,
  memory_forget: TIERS.strong,
  browser_navigate: TIERS.strong,
  browser_click: TIERS.strong,
  browser_type: TIERS.strong,
  browser_evaluate: TIERS.strong,
  click: TIERS.strong,
  double_click: TIERS.strong,
  type_text: TIERS.strong,
  press_key: TIERS.strong,
  scroll: TIERS.strong,
};

const DEFAULT_TOOL_TIER = TIERS.standard;

export function toolAllowedFor(toolName, tier) {
  return tier >= (TOOL_MIN_TIER[toolName] ?? DEFAULT_TOOL_TIER);
}

/** Filter a tool list to what this tier can actually be trusted with. */
export function toolsForTier(tools, tier) {
  return (tools || []).filter(t => toolAllowedFor(t.function?.name, tier));
}

// ── weighing a task ──────────────────────────────────────────────────────
// ~3.6 chars per token is a reasonable English+code average. This is an
// estimate used for routing, never for truncation, so being a little wrong is
// cheap — and it is far better than routing blind.
const CHARS_PER_TOKEN = 3.6;

export function estimateTokens(text) {
  return Math.ceil(String(text || '').length / CHARS_PER_TOKEN);
}

// Signals that a task needs real capability rather than a quick reply.
const HEAVY_SIGNALS = [
  /\b(refactor|migrate|rewrite|redesign|architect|implement|port|overhaul)\b/i,
  /\b(across|throughout|every|all)\b[^.?!]{0,30}\b(files?|modules?|components?|tests?)\b/i,
  /\b(debug|root cause|why (is|does|did)|trace|diagnose)\b/i,
  /\b(multi[- ]step|step \d|then\b.*\bthen\b)/i,
];

/**
 * Weigh a task against a model.
 * @returns {{tokens:number, contextUse:number, heavy:boolean, verdict:string, reasons:string[]}}
 *   verdict: 'ok' | 'stretch' | 'overloaded'
 */
export function weighTask({
  systemPrompt = '', userText = '', priorMessages = [], tools = [],
  numCtx = 4096, tier = TIERS.standard,
} = {}) {
  const promptTokens = estimateTokens(systemPrompt) + estimateTokens(userText)
    + priorMessages.reduce((sum, m) => sum + estimateTokens(m.content), 0);
  // Tool schemas are the quiet majority of many prompts: ~90 tokens each.
  const toolTokens = (tools?.length || 0) * 90;
  const tokens = promptTokens + toolTokens;

  // Leave room for the model to actually answer, not just to read.
  const usableCtx = Math.max(1, numCtx * 0.75);
  const contextUse = tokens / usableCtx;

  const heavy = HEAVY_SIGNALS.some(pattern => pattern.test(userText));
  const reasons = [];
  if (contextUse >= 1) reasons.push(`prompt is ~${tokens} tokens against a ${numCtx} context`);
  else if (contextUse >= 0.7) reasons.push(`prompt fills ~${Math.round(contextUse * 100)}% of usable context`);
  if (heavy && tier <= TIERS.light) reasons.push('multi-step task on a light model');

  let verdict = 'ok';
  if (contextUse >= 1 || (heavy && tier <= TIERS.light)) verdict = 'overloaded';
  else if (contextUse >= 0.7 || (heavy && tier < TIERS.strong)) verdict = 'stretch';

  return { tokens, contextUse: Number(contextUse.toFixed(2)), heavy, verdict, reasons };
}

// ── routing ──────────────────────────────────────────────────────────────
/**
 * The strongest available model, excluding one that is already too weak.
 * @param {Array<{key:string, tier:number, paramBillions:number|null}>} roster
 */
export function strongestModel(roster, { above = -1 } = {}) {
  const eligible = (roster || [])
    .filter(m => m.tier > above && m.tier > TIERS.none)
    .sort((a, b) => (b.tier - a.tier) || ((b.paramBillions || 0) - (a.paramBillions || 0)));
  return eligible[0] || null;
}

/**
 * Decide which model should actually run this turn.
 * Returns the original when nothing better exists — a handoff that lands on an
 * equally weak model is worse than none, because it costs a whole extra turn.
 */
export function routeTask({ requested, roster, weight }) {
  const current = (roster || []).find(m => m.key === requested);
  if (!current || weight.verdict === 'ok') {
    return { model: requested, handoff: false };
  }
  const stronger = strongestModel(roster, { above: current.tier });
  if (!stronger) {
    return {
      model: requested, handoff: false,
      note: `${requested} is ${weight.verdict} for this task, but no stronger model is available.`,
    };
  }
  return {
    model: stronger.key,
    handoff: true,
    from: requested,
    reason: weight.reasons.join('; ') || weight.verdict,
  };
}
