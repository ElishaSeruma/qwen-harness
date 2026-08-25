// gui/agent-loop.js — tool-calling chat loop for the GUI.
//
// Bridges the browser's chat composers (Home, Code, Agent Hub console) to
// the same tool registry the Python agents use (tools/__init__.py), via
// gui/server.py's /api/tools/list + /api/tools/call. When the "Agent tools"
// capability-bar toggle is off, this degrades to a plain one-shot chat call
// (no `tools=` sent), so it's a drop-in replacement for the old callOllama().

import { apiToolsList, apiToolCall, getToolsEnabled } from './capabilities.js';
import { EFFORT_LEVELS, parseEffortEscalation } from './effort.js';

const OLLAMA_URL = 'http://localhost:11434/v1/chat/completions';
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

async function streamCompletion(body, signal, onToken) {
  const res = await fetch(OLLAMA_URL, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, stream: true }), signal,
  });
  if (!res.ok) throw new Error(`Ollama responded ${res.status}`);
  if (!res.body || !(res.headers.get('content-type') || '').includes('text/event-stream')) return res.json();

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '', content = '', usage = null;
  const calls = new Map();
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n'); buffer = lines.pop() || '';
    for (const raw of lines) {
      const line = raw.trim();
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') continue;
      let chunk;
      try { chunk = JSON.parse(payload); } catch { continue; }
      usage = chunk.usage || usage;
      const delta = chunk.choices?.[0]?.delta || chunk.choices?.[0]?.message || {};
      if (delta.content) {
        content += delta.content;
        onToken?.(content, delta.content);
      }
      for (const part of delta.tool_calls || []) {
        const index = part.index ?? calls.size;
        const current = calls.get(index) || { id: '', type: 'function', function: { name: '', arguments: '' } };
        if (part.id) current.id = part.id;
        if (part.function?.name) current.function.name += part.function.name;
        if (part.function?.arguments) current.function.arguments += part.function.arguments;
        calls.set(index, current);
      }
    }
  }
  return { choices: [{ message: { role: 'assistant', content, tool_calls: calls.size ? [...calls.values()] : undefined } }], usage };
}

let toolDefsPromise = null;
function loadToolDefs() {
  if (!toolDefsPromise) toolDefsPromise = apiToolsList().then(r => r.tools);
  return toolDefsPromise;
}

// All 35 tool schemas is ~3k tokens of prompt on EVERY request — enough to
// stall a 4B model on an 8GB CPU-only machine before it emits anything.
// Each surface therefore sends only the tools it plausibly needs; agents can
// still reach the rest via tool_search, which is always included.
const TOOL_PROFILES = {
  code: [
    'read_file', 'write_file', 'edit_file', 'append_file', 'list_dir',
    'search_files', 'find_files', 'run_shell',
    'github_status', 'github_list_repos', 'github_list_dir', 'github_get_file',
    'memory_write', 'memory_search', 'tool_search',
  ],
  project: [
    'read_file', 'write_file', 'edit_file', 'append_file', 'list_dir',
    'search_files', 'find_files', 'run_shell',
    'github_status', 'github_list_repos', 'github_list_dir', 'github_get_file',
    'web_search', 'scrape_url', 'memory_write', 'memory_search', 'tool_search',
  ],
  home: [
    'read_file', 'write_file', 'edit_file', 'list_dir', 'search_files',
    'find_files', 'run_shell', 'web_search', 'scrape_url', 'fetch_data',
    'memory_write', 'memory_search', 'memory_list', 'create_display', 'tool_search',
  ],
  hub: [
    'read_file', 'write_file', 'edit_file', 'list_dir', 'search_files',
    'run_shell', 'web_search', 'scrape_url', 'memory_write', 'memory_search', 'tool_search',
  ],
};

function selectTools(allTools, profile) {
  if (profile === 'all' || !TOOL_PROFILES[profile]) return allTools;
  const wanted = new Set(TOOL_PROFILES[profile]);
  const picked = allTools.filter(t => wanted.has(t.function?.name));
  return picked.length ? picked : allTools;
}

// Ollama hard-errors (400) if you send `tools=` to a model that doesn't
// support tool-calling (e.g. gemma3/gemma2 in the current light roster) —
// it does NOT just ignore the param. So capability must be checked per
// model before ever including `tools` in the request.
let modelInfoPromise = null;
async function loadModelInfo() {
  if (!modelInfoPromise) {
    modelInfoPromise = fetch('http://localhost:11434/api/tags', { cache: 'no-store' })
      .then(r => r.json())
      .then(data => new Map((data.models || []).map(m => [
        m.name,
        { capabilities: m.capabilities || [], contextLength: m.details?.context_length ?? null },
      ])))
      .catch(() => new Map());
  }
  return modelInfoPromise;
}
async function modelSupportsTools(modelKey) {
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
export async function runAgentTurn({ modelKey, systemPrompt, userText, priorMessages = [], cwd = null, toolProfile = 'home', signal = null, runId = null, onEvent, onToken, onApprovalRequest, onEffortEscalation, toolsEnabled = null, effort = 1 }) {
  let activeEffort = Math.max(1, Math.min(4, Number(effort) || 1));
  const wantsTools = toolsEnabled == null ? getToolsEnabled() : Boolean(toolsEnabled);
  const canUseTools = wantsTools && await modelSupportsTools(modelKey);
  if (wantsTools && !canUseTools) {
    onEvent?.({ type: 'system', text: `${modelKey} doesn't support tool-calling — replying without tools.` });
  }
  const allTools = canUseTools ? await loadToolDefs().catch(() => null) : null;
  let tools = allTools ? selectTools(allTools, toolProfile) : null;
  if (tools && activeEffort >= 2 && activeEffort <= 3) tools = [...tools, EFFORT_ESCALATION_TOOL];

  // When tools are live, teach the model how to code well with them —
  // small local models won't infer this workflow on their own, and without
  // the cwd they don't know where relative paths land.
  let fullSystemPrompt = systemPrompt;
  const effortConfig = EFFORT_LEVELS[activeEffort];
  fullSystemPrompt += `\n\nThinking effort: ${effortConfig.name} (${activeEffort}/4) — ${effortConfig.detail}.`;
  if (activeEffort === 1) fullSystemPrompt += '\nAnswer directly. Avoid unnecessary planning or tool calls.';
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
      + '- To change existing code: read_file first, then edit_file with an exact unique old_string. Only use write_file for brand-new files or full rewrites.\n'
      + '- To locate code: search_files (content grep) or find_files (filename glob) before guessing paths.\n'
      + '- After editing, verify when possible (read_file the changed region, or run_shell a quick check).\n'
      + '- Chain as many tool calls as the task needs before answering.';
  }
  const messages = [
    { role: 'system', content: `${activeEffort >= 3 ? '/think' : '/no_think'}\n\n${fullSystemPrompt}` },
    ...priorMessages,
    { role: 'user', content: userText },
  ];

  for (let i = 0; i < EFFORT_LEVELS[activeEffort].toolIterations; i++) {
    const currentConfig = EFFORT_LEVELS[activeEffort];
    const data = await streamCompletion({
        model: modelKey,
        messages,
        ...(tools ? { tools, tool_choice: 'auto' } : {}),
        max_tokens: currentConfig.maxTokens,
        temperature: currentConfig.temperature,
        think: currentConfig.think,
      }, signal, onToken);
    const message = data.choices?.[0]?.message;
    if (!message) throw new Error('No response from model');

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
      if (data.usage?.total_tokens != null) onEvent?.({ type: 'usage', totalTokens: data.usage.total_tokens });
      return stripThink(message.content);
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
        if (name === 'request_max_effort') {
          const reason = String(args.reason || 'Task requires additional rigor').slice(0, 500);
          if (activeEffort < 4) {
            const previous = activeEffort; activeEffort = 4;
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
          const prompt = `Allow ${err.scope || name}?\n\n${JSON.stringify(args, null, 2).slice(0, 1200)}`;
          const approved = onApprovalRequest
            ? await onApprovalRequest({ tool: name, scope: err.scope, args, prompt })
            : window.confirm(prompt);
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
