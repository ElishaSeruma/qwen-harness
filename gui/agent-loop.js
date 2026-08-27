// gui/agent-loop.js — tool-calling chat loop for the GUI.
//
// Bridges the browser's chat composers (Home, Code, Agent Hub console) to
// the same tool registry the Python agents use (tools/__init__.py), via
// gui/server.py's /api/tools/list + /api/tools/call. When the "Agent tools"
// capability-bar toggle is off, this degrades to a plain one-shot chat call
// (no `tools=` sent), so it's a drop-in replacement for the old callOllama().

import { apiToolsList, apiToolCall, apiSkillsList, apiFeedbackSummary, apiContextPlan, getToolsEnabled } from './capabilities.js';
import { EFFORT_LEVELS, parseEffortEscalation, wantsToolUse } from './effort.js';
import {
  TIERS, tierFor, tierName, toolsForTier, weighTask, routeTask,
} from './weighbridge.js';

// Ollama binds 127.0.0.1 only, and on Windows `localhost` resolves to ::1
// first. Measured: a request to http://localhost:11434 costs 2133 ms because
// the ::1 attempt has to fail before the IPv4 fallback is tried, against 8 ms
// to http://127.0.0.1:11434 — a 266x penalty, and this is the URL every single
// inference request goes to. Browsers vary in how aggressively they fall back;
// the literal address removes the question entirely.
const OLLAMA_ORIGIN = 'http://127.0.0.1:11434';
const OLLAMA_URL = `${OLLAMA_ORIGIN}/v1/chat/completions`;
// TIME is the primary limit, not tokens.
//
// On this hardware tokens are a bad proxy for what you actually care about.
// At ~7 tok/s the old budgets allowed, worst case: Quick 28 min, Focus 94 min,
// Deep 4.4 hours, Max 5.6 hours — so the token caps never bound anything
// useful, they just truncated the model mid-thought when a single reply ran
// long. A clock does what the caps were being asked to do, and does it in the
// unit you'd actually set.
//
// max_tokens survives only as a RUNAWAY GUARD: high enough never to interrupt
// legitimate work, low enough to stop a model looping forever inside one call.
const RUNAWAY_MAX_TOKENS = 16384;
const DEFAULT_DEADLINE_MS = 10 * 60 * 1000;
const WEB_TOOLS = ['web_search', 'scrape_url', 'fetch_data'];
const WEB_INTENT = /\b(web|internet|online|google|search|browse|news|url|website|look\s?up)\b/i;

const HANDOFF_TOOL = {
  type: 'function',
  function: {
    name: 'request_stronger_model',
    description:
      'Hand this task to a more capable model. Call this when the task needs '
      + 'more reasoning, more context, or more precise tool use than you can '
      + 'reliably provide — a correct handoff is far better than a confident '
      + 'wrong answer. Do not call it for tasks you can simply do.',
    parameters: {
      type: 'object',
      properties: { reason: { type: 'string', description: 'Briefly, what makes this too heavy for you.' } },
      required: ['reason'],
    },
  },
};

const EFFORT_ESCALATION_TOOL = {
  type: 'function',
  function: {
    name: 'request_max_effort',
    description: 'Raise this run to Max effort when the task is materially more complex or risky than the current Focus/Deep budget. Use only when additional reasoning, tool work, or verification is genuinely required.',
    parameters: {
      type: 'object',
      properties: { reason: { type: 'string', description: 'Concise reason Max effort is necessary.' } },
      required: ['reason'],
    },
  },
};

// Ollama's OpenAI-compatible /v1 endpoint CANNOT set num_ctx — it silently
// uses Ollama's default, which is 4k on any machine under 24 GiB of VRAM
// (docs.ollama.com/context-length). Measured here: a /v1 request reloaded a
// model that had been loaded at 8192 back down to 4096.
//
// 4k cannot hold tool schemas + a source file + the conversation, and Ollama
// does not error on overflow — it drops the oldest content. The model then
// answers a question it can no longer see, which looks like a wild
// hallucination rather than truncation.
//
// So the loop talks to the NATIVE /api/chat endpoint, which does accept
// per-request options. The response shape differs from OpenAI's and is
// normalised back to it here so nothing downstream has to care.
const OLLAMA_CHAT_URL = `${OLLAMA_ORIGIN}/api/chat`;

async function streamCompletion(body, signal, onToken, onReasoning) {
  const { model, messages, tools, max_tokens, temperature, think, num_ctx } = body;
  const payload = {
    model,
    messages: messages.map(toNativeMessage),
    stream: true,
    think: Boolean(think),
    options: {
      // The real fix. Everything else in this file is downstream of it.
      ...(num_ctx ? { num_ctx } : {}),
      ...(max_tokens ? { num_predict: max_tokens } : {}),
      ...(temperature != null ? { temperature } : {}),
    },
  };
  if (tools?.length) payload.tools = tools;

  const res = await fetch(OLLAMA_CHAT_URL, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload), signal,
  });
  if (!res.ok) throw new Error(`Ollama responded ${res.status}: ${(await res.text()).slice(0, 200)}`);
  if (!res.body) throw new Error('Ollama returned no response body');

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '', content = '', reasoning = '', usage = null;
  const calls = [];

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // Native /api/chat streams newline-delimited JSON, not SSE `data:` frames.
    const lines = buffer.split('\n'); buffer = lines.pop() || '';
    for (const raw of lines) {
      const line = raw.trim();
      if (!line) continue;
      let chunk;
      try { chunk = JSON.parse(line); } catch { continue; }
      if (chunk.error) throw new Error(String(chunk.error).slice(0, 300));

      const message = chunk.message || {};
      if (message.content) {
        content += message.content;
        onToken?.(content, message.content);
      }
      // Native calls the chain-of-thought field `thinking`; /v1 called it
      // `reasoning`. Accept either so this keeps working if Ollama changes.
      const thought = message.thinking ?? message.reasoning;
      if (thought) { reasoning += thought; onReasoning?.(reasoning, thought); }

      for (const call of message.tool_calls || []) {
        calls.push({
          id: call.id || `native_${calls.length}`,
          type: 'function',
          function: {
            name: call.function?.name || '',
            // Native returns arguments as an OBJECT; the rest of the loop
            // (and the salvage path) expects the OpenAI string form.
            arguments: typeof call.function?.arguments === 'string'
              ? call.function.arguments
              : JSON.stringify(call.function?.arguments ?? {}),
          },
        });
      }

      if (chunk.done) {
        usage = {
          prompt_tokens: chunk.prompt_eval_count ?? null,
          completion_tokens: chunk.eval_count ?? null,
          total_tokens: (chunk.prompt_eval_count ?? 0) + (chunk.eval_count ?? 0),
          eval_duration_ms: chunk.eval_duration != null ? Math.round(chunk.eval_duration / 1e6) : null,
          load_duration_ms: chunk.load_duration != null ? Math.round(chunk.load_duration / 1e6) : null,
          done_reason: chunk.done_reason || null,
          num_ctx: num_ctx || null,
        };
      }
    }
  }

  return {
    choices: [{ message: { role: 'assistant', content, reasoning, tool_calls: calls.length ? calls : undefined } }],
    usage,
  };
}

/** OpenAI-shaped message -> what native /api/chat expects. */
function toNativeMessage(message) {
  if (message.role === 'tool') {
    // Native uses tool_name rather than tool_call_id.
    return { role: 'tool', content: String(message.content ?? ''), ...(message.tool_name ? { tool_name: message.tool_name } : {}) };
  }
  if (message.role === 'assistant' && message.tool_calls?.length) {
    return {
      role: 'assistant',
      content: message.content || '',
      tool_calls: message.tool_calls.map(call => ({
        function: {
          name: call.function?.name,
          arguments: (() => {
            try { return JSON.parse(call.function?.arguments || '{}'); }
            catch { return {}; }
          })(),
        },
      })),
    };
  }
  return { role: message.role, content: String(message.content ?? '') };
}

// Preference rules derived from what the user rated well or badly, plus the
// profile block. Refetched on a short TTL rather than cached forever, so a
// rating or a profile edit takes effect on roughly the next turn.
let guidanceCache = { text: '', profile: '', at: 0 };
const GUIDANCE_TTL_MS = 30_000;
export function invalidateGuidanceCache() {
  guidanceCache = { text: '', profile: '', at: 0 };
}
window.addEventListener('qh-profile-saved', invalidateGuidanceCache);
window.addEventListener('qh-rating-changed', invalidateGuidanceCache);

async function learnedGuidance() {
  if (Date.now() - guidanceCache.at < GUIDANCE_TTL_MS) return guidanceCache;
  try {
    const summary = await apiFeedbackSummary();
    guidanceCache = {
      text: summary.guidance || '',
      profile: summary.profile_block || '',
      at: Date.now(),
    };
  } catch {
    guidanceCache = { ...guidanceCache, at: Date.now() };
  }
  return guidanceCache;
}

let toolDefsPromise = null;
function loadToolDefs() {
  if (!toolDefsPromise) toolDefsPromise = apiToolsList().then(r => r.tools);
  return toolDefsPromise;
}

let skillDefsPromise = null;
export function invalidateSkillDefs() { skillDefsPromise = null; }
function loadSkills() {
  if (!skillDefsPromise) skillDefsPromise = apiSkillsList().then(r => r.skills || []);
  return skillDefsPromise;
}

// A slash skill is explicit and deterministic: only a leading /name activates
// it, avoiding accidental prompt injection from prose that happens to mention a
// skill name. Its instructions and requested tools are independent of effort.
async function activateSlashSkill(text) {
  const match = /^\s*\/([a-z0-9]+(?:-[a-z0-9]+)*)\b\s*/i.exec(text || '');
  if (!match) return { text, skill: null };
  const name = match[1].toLowerCase();
  const skill = (await loadSkills()).find(item => item.name === name);
  if (!skill) return { text, skill: null, unknown: name };
  return { text: text.slice(match[0].length).trim() || `Follow the ${skill.command} workflow.`, skill };
}

// All 35 tool schemas is ~3k tokens of prompt on EVERY request — enough to
// stall a 4B model on an 8GB CPU-only machine before it emits anything.
// Each surface therefore sends only the tools it plausibly needs; agents can
// still reach the rest via tool_search, which is always included.
export const TOOL_PROFILES = {
  code: [
    'read_file', 'write_file', 'edit_file', 'append_file', 'list_dir',
    'search_files', 'find_files', 'run_shell',
    'github_status', 'github_list_repos', 'github_list_dir', 'github_get_file',
    'memory_write', 'memory_search', 'tool_search', 'date_time',
    'sandbox_status', 'sandbox_verify', 'sandbox_promote',
  ],
  project: [
    'read_file', 'write_file', 'edit_file', 'append_file', 'list_dir',
    'search_files', 'find_files', 'run_shell',
    'github_status', 'github_list_repos', 'github_list_dir', 'github_get_file',
    'web_search', 'scrape_url', 'memory_write', 'memory_search', 'tool_search', 'date_time',
    'sandbox_status', 'sandbox_verify', 'sandbox_promote',
  ],
  home: [
    'read_file', 'write_file', 'edit_file', 'list_dir', 'search_files',
    'find_files', 'run_shell', 'web_search', 'scrape_url', 'fetch_data',
    'memory_write', 'memory_search', 'memory_list', 'create_display', 'tool_search', 'date_time',
    'sandbox_status', 'sandbox_verify', 'sandbox_promote',
  ],
  hub: [
    'read_file', 'write_file', 'edit_file', 'list_dir', 'search_files',
    'run_shell', 'web_search', 'scrape_url', 'memory_write', 'memory_search', 'tool_search', 'date_time',
    'sandbox_status', 'sandbox_verify', 'sandbox_promote',
  ],
  // Sub-2B models are the ones fast enough to be useful for agentic work on
  // this hardware, but they degrade badly with a long tool list. Five tools
  // (~450 tokens vs ~1400) covers read/edit/locate/run; tool_search reaches
  // the rest on demand.
  minimal: [
    'read_file', 'edit_file', 'search_files', 'run_shell', 'tool_search', 'date_time',
    'sandbox_verify', 'sandbox_promote',
  ],
};

function selectTools(allTools, profile) {
  if (profile === 'all') return allTools;
  const wanted = TOOL_PROFILES[profile];
  if (!wanted) {
    // Falling back to all ~35 schemas here would silently reintroduce the
    // ~3k-token prompt this whole mechanism exists to avoid, and a typo'd
    // profile name would be invisible. Fail loudly, degrade to minimal.
    console.warn(`[agent-loop] unknown tool profile "${profile}" — using minimal`);
    return selectTools(allTools, 'minimal');
  }
  const set = new Set(wanted);
  const picked = allTools.filter(t => set.has(t.function?.name));
  return picked.length ? picked : allTools;
}

// Ollama hard-errors (400) if you send `tools=` to a model that doesn't
// support tool-calling (e.g. gemma3/gemma2 in the current light roster) —
// it does NOT just ignore the param. So capability must be checked per
// model before ever including `tools` in the request.
// A single call should never need more than this; anything beyond it is a
// loop, not an answer. Deliberately NOT scaled by effort — effort now buys
// time and iterations, which is what actually changes what a model can do.
function tokenBudgetFor() {
  return RUNAWAY_MAX_TOKENS;
}

let contextPlanPromise = null;
function loadContextPlan() {
  if (!contextPlanPromise) {
    contextPlanPromise = apiContextPlan().catch(() => ({ models: {} }));
  }
  return contextPlanPromise;
}
export function invalidateContextPlan() { contextPlanPromise = null; }

/** num_ctx for this model, or null to leave Ollama's default alone. */
async function contextForModel(modelKey) {
  const plan = await loadContextPlan();
  return plan.models?.[modelKey]?.context ?? null;
}

let modelInfoPromise = null;
async function loadModelInfo() {
  if (!modelInfoPromise) {
    modelInfoPromise = fetch(`${OLLAMA_ORIGIN}/api/tags`, { cache: 'no-store' })
      .then(r => r.json())
      .then(data => new Map((data.models || []).map(m => [
        m.name,
        {
          capabilities: m.capabilities || [],
          contextLength: m.details?.context_length ?? null,
          paramBillions: parseParamBillions(m.details?.parameter_size),
        },
      ])))
      .catch(() => new Map());
  }
  return modelInfoPromise;
}
/**
 * Ollama reports parameter_size as a string with MIXED units — "4.0B",
 * "1.2B", but also "751.63M" and "999.89M". A naive parseFloat would read
 * qwen3:0.6b ("751.63M") as a 751-billion-parameter model, i.e. exactly
 * backwards. Normalize to billions; return null when unknown.
 */
function parseParamBillions(raw) {
  if (typeof raw !== 'string') return null;
  const m = raw.trim().match(/^([\d.]+)\s*([BbMm])?$/);
  if (!m) return null;
  const value = parseFloat(m[1]);
  if (!Number.isFinite(value)) return null;
  return (m[2] || 'B').toUpperCase() === 'M' ? value / 1000 : value;
}

/** Every tools-capable local model, with its capability tier. */
async function loadRoster() {
  const info = await loadModelInfo();
  return [...info.entries()].map(([key, meta]) => ({
    key,
    paramBillions: meta.paramBillions,
    capabilities: meta.capabilities,
    tier: tierFor({ paramBillions: meta.paramBillions, capabilities: meta.capabilities }),
  }));
}

/** Parameter count in billions, or null if Ollama didn't report it. */
async function modelParamBillions(modelKey) {
  const info = await loadModelInfo();
  return info.get(modelKey)?.paramBillions ?? null;
}

/** True when the model reasons before answering (qwen3 and friends). */
async function modelIsThinking(modelKey) {
  const info = await loadModelInfo();
  return (info.get(modelKey)?.capabilities || []).includes('thinking');
}

export async function modelSupportsTools(modelKey) {
  const info = await loadModelInfo();
  return (info.get(modelKey)?.capabilities || []).includes('tools');
}
/** The model's real context window (tokens), or null if unknown. */
export async function getModelContextLimit(modelKey) {
  const info = await loadModelInfo();
  return info.get(modelKey)?.contextLength ?? null;
}

// Small local models frequently emit a tool call as plain TEXT instead of
// populating the structured `tool_calls` field — e.g. llama3.2 returns
// {"type":"function","function":{"name":...,"parameters":{...}}} as content.
// Without this salvage the loop sees "no tool calls", returns the JSON as the
// answer, and the tool never runs. Parse the common shapes back into real
// calls so these models are actually usable.
function salvageToolCalls(content, knownNames) {
  if (!content) return null;
  const text = content.trim();
  const candidates = [];
  // <tool_call>{...}</tool_call> (qwen-style) and bare/fenced JSON objects
  for (const m of text.matchAll(/<tool_call>([\s\S]*?)<\/tool_call>/g)) candidates.push(m[1]);
  for (const m of text.matchAll(/```(?:json)?\s*([\s\S]*?)```/g)) candidates.push(m[1]);
  if (text.startsWith('{') || text.startsWith('[')) candidates.push(text);

  const calls = [];
  for (const raw of candidates) {
    let parsed;
    try { parsed = JSON.parse(raw.trim()); } catch { continue; }
    for (const obj of Array.isArray(parsed) ? parsed : [parsed]) {
      if (!obj || typeof obj !== 'object') continue;
      const fn = obj.function && typeof obj.function === 'object' ? obj.function : obj;
      const name = fn.name || obj.name;
      if (!name || (knownNames && !knownNames.has(name))) continue;
      // argument key varies by model: arguments / parameters / _parameters / input
      let args = fn.arguments ?? fn.parameters ?? fn._parameters ?? obj.arguments ?? obj.parameters ?? obj._parameters ?? obj.input ?? {};
      if (typeof args === 'string') { try { args = JSON.parse(args); } catch { args = {}; } }
      // some models nest the real args under a stray "properties" key
      if (args && typeof args === 'object' && args.properties && typeof args.properties === 'object' && !args.path && !args.command) {
        args = { ...args.properties, ...args };
        delete args.properties;
      }
      calls.push({ id: `salvaged_${calls.length}`, type: 'function', function: { name, arguments: JSON.stringify(args) } });
    }
  }
  return calls.length ? calls : null;
}

function stripThink(text) {
  return (text || '').replace(/<think>[\s\S]*?<\/think>/g, '').trim();
}

/**
 * Run one user turn to completion, following any tool calls the model makes.
 * @param {{modelKey:string, systemPrompt:string, userText:string, priorMessages?:object[], cwd?:string, signal?:AbortSignal, runId?:string, onEvent?:(e:object)=>void, onToken?:(text:string,delta:string)=>void, onApprovalRequest?:(request:object)=>Promise<boolean>}} opts
 *   priorMessages: earlier turns to give the model continuity — either raw
 *   {role,content} pairs, or [{role:'system', content: compactedSummary}]
 *   when continuing a compacted chat. Spliced in between system and the new
 *   user turn.
 * @returns {Promise<string>} the final assistant reply text
 */
export async function runAgentTurn({ modelKey, systemPrompt, userText, priorMessages = [], cwd = null, toolProfile = 'home', deadlineMs = DEFAULT_DEADLINE_MS, signal = null, runId = null, onEvent, onToken, onApprovalRequest, onEffortEscalation, onHandoff, toolsEnabled = null, effort = 1, allowHandoff = true, handoffDepth = 0 }) {
  let activeEffort = Math.max(1, Math.min(4, Number(effort) || 1));
  const activated = await activateSlashSkill(userText);
  userText = activated.text;
  if (activated.unknown) onEvent?.({ type: 'system', text: `Unknown skill /${activated.unknown}. Open Skills & Tools to see installed commands.` });
  if (activated.skill) onEvent?.({ type: 'system', text: `Skill active: ${activated.skill.command} — ${activated.skill.description}` });
  // A slash skill must remain usable even when the composer is set to Quick.
  // Focus is the smallest budget that reliably permits an instruction + tool
  // call + result cycle; higher user-selected effort remains unchanged.
  if (activated.skill) activeEffort = Math.max(2, activeEffort);
  const wantsTools = toolsEnabled == null ? getToolsEnabled() : Boolean(toolsEnabled);
  const canUseTools = wantsTools && await modelSupportsTools(modelKey);
  if (wantsTools && !canUseTools) {
    // Models without the `tools` capability (gemma*) can't be made to call one
    // by prompting. When the user explicitly asked for tool work, say so plainly
    // rather than letting the model answer from memory and look like it ignored
    // the request.
    onEvent?.({ type: 'system', text: wantsToolUse(userText)
      ? `${modelKey} doesn't support tool-calling, so it can't run that tool — it will answer from memory only. Switch to a tools-capable model (e.g. qwen3:4b) to actually run it.`
      : `${modelKey} doesn't support tool-calling — replying without tools.` });
  }
  const allTools = canUseTools ? await loadToolDefs().catch(() => null) : null;
  // Downgrade to the minimal profile for small models regardless of which
  // surface asked. Unknown size falls back to the REQUESTED profile, never to
  // minimal — an unrecognised model shouldn't lose capability.
  let effectiveProfile = toolProfile;
  if (allTools && toolProfile !== 'all') {
    const billions = await modelParamBillions(modelKey);
    if (billions != null && billions < 2) effectiveProfile = 'minimal';
  }
  const explicitToolRequest = wantsToolUse(userText);
  const isThinkingModel = await modelIsThinking(modelKey).catch(() => false);
  const numCtx = await contextForModel(modelKey);
  let tools = allTools ? selectTools(allTools, effectiveProfile) : null;
  // Skills declare their tool needs. Add those exact schemas after profile
  // selection so Quick/minimal effort can never silently disable a slash skill.
  if (tools && allTools && activated.skill?.tools?.length) {
    const present = new Set(tools.map(t => t.function?.name));
    tools = [...tools, ...allTools.filter(t => activated.skill.tools.includes(t.function?.name) && !present.has(t.function?.name))];
  }
  // The code/minimal profiles omit the web tools to keep the prompt small, so
  // "search the web for X" on the Code surface previously had no web_search to
  // call at all. When the prompt is plainly about the internet, add them back
  // for this turn rather than making the model detour through tool_search.
  if (tools && allTools && explicitToolRequest && WEB_INTENT.test(userText)) {
    const present = new Set(tools.map(t => t.function?.name));
    const missing = allTools.filter(t => WEB_TOOLS.includes(t.function?.name) && !present.has(t.function?.name));
    if (missing.length) tools = [...tools, ...missing];
  }
  // ── weighbridge: gate tools by what this model can actually handle ──
  // A tool list is not one-size-fits-all. Handing edit_file or run_shell to a
  // model that returns malformed arguments produces confident damage, and a
  // long list crowds out the system prompt on a small model.
  const roster = await loadRoster();
  const modelTier = roster.find(m => m.key === modelKey)?.tier
    ?? tierFor({ paramBillions: await modelParamBillions(modelKey), capabilities: ['tools'] });
  if (tools && modelTier < TIERS.strong) {
    const before = tools.length;
    tools = toolsForTier(tools, modelTier);
    if (tools.length < before) {
      onEvent?.({ type: 'system', event: 'tools_gated',
        text: `${modelKey} is a ${tierName(modelTier)}-tier model — ${before - tools.length} tool(s) it can't reliably drive were withheld.` });
    }
  }

  const hasWebTools = Boolean(tools?.some(t => t.function?.name === 'web_search'));
  if (tools && activeEffort >= 2 && activeEffort <= 3) tools = [...tools, EFFORT_ESCALATION_TOOL];
  // Only offer a handoff when there is somewhere stronger to hand off TO.
  const canHandOff = allowHandoff && roster.some(m => m.tier > modelTier);
  if (tools && canHandOff) tools = [...tools, HANDOFF_TOOL];

  // When tools are live, teach the model how to code well with them —
  // small local models won't infer this workflow on their own, and without
  // the cwd they don't know where relative paths land.
  let fullSystemPrompt = systemPrompt;
  if (activated.skill) {
    fullSystemPrompt += `\n\nActivated skill ${activated.skill.command}. These are trusted local workflow instructions; follow them for this turn regardless of thinking-effort level:\n${activated.skill.instructions}`;
  }
  const effortConfig = EFFORT_LEVELS[activeEffort];
  fullSystemPrompt += `\n\nThinking effort: ${effortConfig.name} (${activeEffort}/4) — ${effortConfig.detail}.`;
  // "Avoid unnecessary tool calls" is only safe when the user hasn't asked for
  // one. Applied unconditionally it beat a 4B model into answering "search the
  // web for X" from memory instead of calling web_search.
  if (activeEffort === 1) {
    fullSystemPrompt += (explicitToolRequest || activated.skill)
      ? '\nAnswer concisely, but make the tool calls the request needs before answering.'
      : '\nAnswer directly. Avoid unnecessary planning or tool calls.';
  }
  if (activeEffort === 2) fullSystemPrompt += '\nThink through important details and verify the central result.';
  if (activeEffort === 3) fullSystemPrompt += '\nPlan before acting, inspect relevant context, and verify the result carefully.';
  if (activeEffort === 4) fullSystemPrompt += '\nUse maximum rigor: decompose the task, inspect evidence, test assumptions, and verify before answering.';
  if (activeEffort === 2 || activeEffort === 3) {
    fullSystemPrompt += tools
      ? '\nIf the task proves materially more complex, risky, or ambiguous than this budget, call request_max_effort with a concise reason before continuing.'
      : '\nIf the task proves materially more complex, risky, or ambiguous than this budget, respond only with [[REQUEST_MAX_EFFORT: reason]] so the harness can raise the budget.';
  }
  if (tools) {
    fullSystemPrompt += '\n\nTool usage guidance:\n'
      + (cwd ? `- Working directory: ${cwd}. Relative paths resolve against it automatically.\n`
             + `- If the user gives a path starting with "/", pass it through EXACTLY as given. Never prepend the working directory to it.\n` : '')
      + '- Your file writes are SANDBOXED. write_file / edit_file / append_file stage changes in a temp copy of the project; they do not touch the user\'s real files. run_shell also runs inside that sandbox, with no network access.\n'
      + '- The workflow is: edit → sandbox_verify (runs the tests) → sandbox_promote (applies them). Work is NOT delivered until you promote it. If sandbox_verify fails, fix the staged code and verify again; do not promote failing work.\n'
      + '- To change existing code: read_file first, then edit_file with an exact unique old_string. Only use write_file for brand-new files or full rewrites.\n'
      + '- To locate code: search_files (content grep) or find_files (filename glob) before guessing paths.\n'
      + '- After editing, verify when possible (read_file the changed region, or run_shell a quick check).\n'
      + (hasWebTools
          ? '- To answer anything about the live internet — a person, company, product, event, price, news, or any fact you are not certain of — call web_search first, then scrape_url on a promising result for the full text. Answer from the results, and cite the URLs you used.\n'
            + '- You DO have internet access through these tools. Never reply that you cannot browse, search, or access the web, and never ask the user to search for you.\n'
            + '- NEVER state a date, day of the week, or time without calling date_time first. You have no clock and your training data is stale, so any date you produce from memory is fabricated — even when it looks plausible.\n'
            + '- scrape_url handles JavaScript-rendered sites itself (Instagram, X, Reddit, modern web apps) — do not reach for browser_* to read a page. If it returns an error saying the page could not be read, say so plainly. Never invent page contents.\n'
          : '')
      + '- Never restate, rephrase, or acknowledge the request instead of acting on it. Emit the tool call itself.\n'
      + (canHandOff
          ? '- If this task needs more reasoning, more context, or more precise tool use than you can reliably provide, call request_stronger_model instead of guessing. A correct handoff beats a confident wrong answer.\n'
          : '')
      + '- Chain as many tool calls as the task needs before answering.'
      + (explicitToolRequest
          ? '\n\nThe user has explicitly asked for tool work in this turn. Your FIRST action must be a tool call, not prose. If they named a tool, call that tool. Do not answer from memory.'
          : '');
  }
  const learned = await learnedGuidance();
  // The profile goes FIRST, before tool guidance and effort rules: who the
  // person is frames everything else, and a model that never sees it falls
  // back to "user".
  if (learned.profile) fullSystemPrompt = `${learned.profile}\n\n${fullSystemPrompt}`;
  if (learned.text) fullSystemPrompt += `\n\n${learned.text}`;

  // No /no_think prefix: measured against qwen3:4b it does not suppress
  // reasoning (546-816 chars emitted regardless, whether placed in the system
  // prompt, the user turn, or via chat_template_kwargs). It only added noise
  // to every prompt. Thinking is controlled by the `think` option below.
  const messages = [
    { role: 'system', content: fullSystemPrompt },
    ...priorMessages,
    { role: 'user', content: userText },
  ];

  // ── weighbridge: weigh the load before setting off ──────────────────
  // Catching an overloaded task here costs one routing decision. Catching it
  // after the fact costs a whole wasted turn plus whatever wrong answer the
  // model produced in the meantime.
  if (allowHandoff && handoffDepth < 1) {
    const weight = weighTask({
      systemPrompt: fullSystemPrompt, userText, priorMessages,
      tools: tools || [], numCtx: numCtx || 4096, tier: modelTier,
    });
    if (weight.verdict !== 'ok') {
      const route = routeTask({ requested: modelKey, roster, weight });
      if (route.handoff) {
        onEvent?.({ type: 'system', event: 'handoff',
          from: route.from, to: route.model, reason: route.reason,
          text: `Handing off to ${route.model}: ${route.reason}.` });
        await onHandoff?.({ from: route.from, to: route.model, reason: route.reason, weight });
        return runAgentTurn({
          modelKey: route.model, systemPrompt, userText, priorMessages, cwd, toolProfile,
          deadlineMs, signal, runId, onEvent, onToken, onApprovalRequest, onEffortEscalation,
          onHandoff, toolsEnabled, effort, allowHandoff, handoffDepth: handoffDepth + 1,
        });
      }
      if (route.note) onEvent?.({ type: 'system', text: route.note });
    }
  }

  // Wall-clock budget. Token budgets alone don't bound elapsed time: at
  // qwen3:4b's measured 7.3 tok/s, Max effort (6144 tokens x 18 iterations)
  // is ~2.8 hours. Stop and return partial work instead of hanging.
  // The effort level's time budget is the real limit. An explicit deadlineMs
  // from the caller still wins, so orchestration can bound a whole graph.
  const effectiveDeadlineMs = deadlineMs === DEFAULT_DEADLINE_MS
    ? (EFFORT_LEVELS[activeEffort].budgetMs || DEFAULT_DEADLINE_MS)
    : deadlineMs;
  let deadlineExtensionMs = 0;
  let budgetExpired = false;
  const deadline = Date.now() + effectiveDeadlineMs;
  let lastAssistantText = '';
  let retriedAfterTruncation = false;
  let truncationRetryBudget = 0;
  for (let i = 0; i < EFFORT_LEVELS[activeEffort].toolIterations; i++) {
    if (Date.now() > deadline + deadlineExtensionMs) {
      const mins = Math.round(effectiveDeadlineMs / 60000);
      onEvent?.({ type: 'system', text: `Stopped after ${mins} min time budget. Ask again to continue.` });
      return lastAssistantText
        || `(stopped after the ${mins} minute time budget — the model was still working. Try a smaller/faster model, or ask again to continue.)`;
    }
    const currentConfig = EFFORT_LEVELS[activeEffort];
    // The deadline has to bound the CALL, not just the gap between calls.
    // Checking it only at the top of the loop meant one long generation could
    // run indefinitely past the budget — observed: a 12-minute Deep budget
    // still thinking at 18 minutes, because it never finished an iteration.
    const remainingMs = (deadline + deadlineExtensionMs) - Date.now();
    const callAbort = new AbortController();
    const onOuterAbort = () => callAbort.abort();
    signal?.addEventListener('abort', onOuterAbort, { once: true });
    const budgetTimer = setTimeout(() => {
      budgetExpired = true;
      callAbort.abort();
    }, Math.max(1000, remainingMs));

    let data;
    try {
      data = await streamCompletion({
        model: modelKey,
        messages,
        ...(tools ? { tools, tool_choice: 'auto' } : {}),
        max_tokens: truncationRetryBudget || tokenBudgetFor(),
        num_ctx: numCtx,
        temperature: currentConfig.temperature,
        think: currentConfig.think,
      }, callAbort.signal, onToken, (full, delta) => onEvent?.({ type: 'reasoning', text: full, delta }));
    } catch (err) {
      if (budgetExpired && err.name === 'AbortError') {
        // Our own deadline, not the user's Stop button.
        const mins = Math.round(effectiveDeadlineMs / 60000);
        onEvent?.({ type: 'system', text: `Stopped mid-generation after the ${mins} min budget. Raise effort, or ask for a smaller step.` });
        return lastAssistantText
          || `(stopped after the ${mins} minute budget — ${modelKey} was still generating. Try a smaller request, a higher effort level, or a faster model.)`;
      }
      throw err;
    } finally {
      clearTimeout(budgetTimer);
      signal?.removeEventListener('abort', onOuterAbort);
    }
    const message = data.choices?.[0]?.message;
    if (!message) throw new Error('No response from model');
    // Report usage for EVERY call in the loop, not just the last. A ten-step
    // tool chain otherwise showed nothing until it finished.
    if (data.usage) onEvent?.({ type: 'usage', totalTokens: data.usage.total_tokens, usage: data.usage });
    const inlineThought = /<think>([\s\S]*?)<\/think>/.exec(message.content || '')?.[1]?.trim();
    if (inlineThought) onEvent?.({ type: 'reasoning', text: inlineThought });
    onEvent?.({ type: 'step', iteration: i + 1, effort: activeEffort });
    if (message.content?.trim()) lastAssistantText = stripThink(message.content);

    let toolCalls = message.tool_calls;
    if ((!toolCalls || toolCalls.length === 0) && tools) {
      const salvaged = salvageToolCalls(message.content, new Set(tools.map(t => t.function?.name)));
      if (salvaged) {
        onEvent?.({ type: 'system', text: `${modelKey} emitted a tool call as text — recovered it.` });
        toolCalls = salvaged;
      }
    }
    if (!toolCalls || toolCalls.length === 0) {
      const escalation = parseEffortEscalation(message.content);
      if (escalation && activeEffort < 4) {
        const previous = activeEffort; activeEffort = 4;
        await onEffortEscalation?.({ level: 4, from: previous, reason: escalation.reason });
        onEvent?.({ type: 'system', event: 'effort_escalated', from: previous, level: 4, reason: escalation.reason, text: `Effort raised to Max: ${escalation.reason}` });
        messages.push({ role: 'assistant', content: message.content });
        messages.push({ role: 'system', content: 'Max effort granted. Continue the original task with maximum rigor and return the actual answer.' });
        continue;
      }
      if (data.usage) onEvent?.({ type: 'usage', totalTokens: data.usage.total_tokens, usage: data.usage });
      const answer = stripThink(message.content);
      if (answer) return answer;

      // Thinking models (qwen3) put chain-of-thought in `reasoning` and the
      // answer in `content`. An empty content with a full reasoning buffer
      // means the budget ran out mid-thought.
      //
      // Measured on qwen3:4b, same prompt, five runs: reasoning ran 642-1801
      // completion tokens — a 2.8x spread against a 2048 cap. Every run
      // eventually produced the right tool call when it had room, so the fix
      // is to give it another pass, not to give up and print its notes.
      const thought = stripThink(message.reasoning || '');
      if (thought && !retriedAfterTruncation) {
        retriedAfterTruncation = true;
        onEvent?.({ type: 'system', text: `${modelKey} ran out of budget mid-thought — retrying with a bigger budget and a nudge to act.` });
        // Already at the runaway ceiling; the retry's value is the nudge to act,
        // not more room. (It used to double a per-level cap that no longer exists.)
        truncationRetryBudget = RUNAWAY_MAX_TOKENS;
        // The abandoned reasoning is deliberately NOT added to `messages`.
        // Feeding a half-finished deliberation back in made the model treat
        // its own musing as an action it had already taken.
        messages.push({ role: 'system', content:
          'Your previous attempt ran out of budget while deliberating and produced no answer. '
          + 'Do not plan further. Take the single next concrete action now: emit one tool call, '
          + 'or give the final answer. Be brief.' });
        continue;
      }
      if (thought) {
        // Second failure. Report it as what it is — an incomplete turn — and
        // flag it so the caller does not persist deliberation as an answer.
        onEvent?.({ type: 'incomplete', reason: 'token budget exhausted while reasoning', thought });
        return `(No answer — ${modelKey} used its whole token budget deliberating, twice, without acting. `
          + `Try a higher effort level, a smaller request, or a larger model.)`;
      }
      return answer;
    }

    messages.push({ role: 'assistant', content: message.content || '', tool_calls: toolCalls });

    for (const [idx, call] of toolCalls.entries()) {
      const name = call.function?.name;
      let args = {};
      try { args = JSON.parse(call.function?.arguments || '{}'); } catch { /* leave {} */ }
      onEvent?.({ type: 'tool_call', name, args });

      let resultText;
      try {
        let result;
        if (name === 'request_stronger_model') {
          const reason = String(args.reason || 'task exceeds this model').slice(0, 300);
          const stronger = roster.filter(m => m.tier > modelTier)
            .sort((a, b) => (b.tier - a.tier) || ((b.paramBillions || 0) - (a.paramBillions || 0)))[0];
          if (!stronger || handoffDepth >= 1) {
            // Tell the model plainly rather than letting it call again in a
            // loop: there is nowhere to go, so it has to attempt the task.
            result = { status: 'no_stronger_model',
                       instruction: 'No stronger model is available. Do the best you can and say clearly where you are unsure.' };
          } else {
            onEvent?.({ type: 'system', event: 'handoff', from: modelKey, to: stronger.key, reason,
              text: `${modelKey} handed off to ${stronger.key}: ${reason}` });
            await onHandoff?.({ from: modelKey, to: stronger.key, reason, requested: true });
            return runAgentTurn({
              modelKey: stronger.key, systemPrompt, userText, priorMessages, cwd, toolProfile,
              deadlineMs, signal, runId, onEvent, onToken, onApprovalRequest, onEffortEscalation,
              onHandoff, toolsEnabled, effort, allowHandoff, handoffDepth: handoffDepth + 1,
            });
          }
        } else if (name === 'request_max_effort') {
          const reason = String(args.reason || 'Task requires additional rigor').slice(0, 500);
          if (activeEffort < 4) {
            const previous = activeEffort; activeEffort = 4;
            // Escalation has to extend the clock too — raising effort while
            // leaving the old deadline in place would grant nothing.
            deadlineExtensionMs = Math.max(0, (EFFORT_LEVELS[4].budgetMs || 0) - effectiveDeadlineMs);
            await onEffortEscalation?.({ level: 4, from: previous, reason });
            onEvent?.({ type: 'system', event: 'effort_escalated', from: previous, level: 4, reason, text: `Effort raised to Max: ${reason}` });
            result = { status: 'granted', effort_level: 4, instruction: 'Continue with maximum rigor and verify the final result.' };
          } else {
            result = { status: 'already_max', effort_level: 4, instruction: 'Continue the task and return the final result.' };
          }
        } else try {
          result = await apiToolCall(name, args, { cwd, run_id: runId });
        } catch (err) {
          if (!err.approval_required) throw err;
          onEvent?.({ type: 'approval_required', tool: name, scope: err.scope, args });
          // No window.confirm fallback. WKWebView only shows one if the host
          // app implements the confirm panel delegate, so on a host that does
          // not, confirm() returns false immediately and every permission is
          // silently DENIED with no dialog ever appearing. Fail closed loudly
          // instead: without a handler there is no way to ask, so say so.
          if (!onApprovalRequest) {
            onEvent?.({ type: 'system', text: `Denied ${err.scope || name}: no permission dialog is available on this surface.` });
            result = { error: 'No approval handler available', denied: true };
            resultText = JSON.stringify(result);
            onEvent?.({ type: 'tool_result', text: resultText });
            messages.push({ role: 'tool', tool_call_id: call.id || `call_${i}_${idx}`, content: resultText });
            continue;
          }
          const approved = await onApprovalRequest({ tool: name, scope: err.scope, args });
          if (!approved) {
            result = { error: 'User denied approval', denied: true };
          } else {
            result = await apiToolCall(name, args, { cwd, run_id: runId, approved: true });
          }
        }
        resultText = typeof result === 'string' ? result : JSON.stringify(result);
      } catch (err) {
        if (err.name === 'AbortError') throw err;
        resultText = JSON.stringify({ error: err.message });
      }
      onEvent?.({ type: 'tool_result', text: resultText });
      messages.push({ role: 'tool', tool_call_id: call.id || `call_${i}_${idx}`, content: resultText });
    }
    // loop: give the model the tool results and let it respond again
  }
  return '(stopped after too many tool-call steps — the model kept calling tools without finishing)';
}
