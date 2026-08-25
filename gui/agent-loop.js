// gui/agent-loop.js — tool-calling chat loop for the GUI.
//
// Bridges the browser's chat composers (Home, Code, Agent Hub console) to
// the same tool registry the Python agents use (tools/__init__.py), via
// gui/server.py's /api/tools/list + /api/tools/call. When the "Agent tools"
// capability-bar toggle is off, this degrades to a plain one-shot chat call
// (no `tools=` sent), so it's a drop-in replacement for the old callOllama().

import { apiToolsList, apiToolCall, getToolsEnabled } from './capabilities.js';

const OLLAMA_URL = 'http://localhost:11434/v1/chat/completions';
const MAX_TOOL_ITERATIONS = 12; // coding tasks routinely need read → search → edit → verify chains

let toolDefsPromise = null;
function loadToolDefs() {
  if (!toolDefsPromise) toolDefsPromise = apiToolsList().then(r => r.tools);
  return toolDefsPromise;
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

function stripThink(text) {
  return (text || '').replace(/<think>[\s\S]*?<\/think>/g, '').trim();
}

/**
 * Run one user turn to completion, following any tool calls the model makes.
 * @param {{modelKey:string, systemPrompt:string, userText:string, priorMessages?:object[], onEvent?:(e:object)=>void}} opts
 *   priorMessages: earlier turns to give the model continuity — either raw
 *   {role,content} pairs, or [{role:'system', content: compactedSummary}]
 *   when continuing a compacted chat. Spliced in between system and the new
 *   user turn.
 * @returns {Promise<string>} the final assistant reply text
 */
export async function runAgentTurn({ modelKey, systemPrompt, userText, priorMessages = [], cwd = null, onEvent }) {
  const wantsTools = getToolsEnabled();
  const canUseTools = wantsTools && await modelSupportsTools(modelKey);
  if (wantsTools && !canUseTools) {
    onEvent?.({ type: 'system', text: `${modelKey} doesn't support tool-calling — replying without tools.` });
  }
  const tools = canUseTools ? await loadToolDefs().catch(() => null) : null;

  // When tools are live, teach the model how to code well with them —
  // small local models won't infer this workflow on their own, and without
  // the cwd they don't know where relative paths land.
  let fullSystemPrompt = systemPrompt;
  if (tools) {
    fullSystemPrompt += '\n\nTool usage guidance:\n'
      + (cwd ? `- Your working directory is ${cwd}. Use absolute paths or paths relative to it.\n` : '')
      + '- To change existing code: read_file first, then edit_file with an exact unique old_string. Only use write_file for brand-new files or full rewrites.\n'
      + '- To locate code: search_files (content grep) or find_files (filename glob) before guessing paths.\n'
      + '- After editing, verify when possible (read_file the changed region, or run_shell a quick check).\n'
      + '- Chain as many tool calls as the task needs before answering.';
  }
  const messages = [
    { role: 'system', content: `/no_think\n\n${fullSystemPrompt}` },
    ...priorMessages,
    { role: 'user', content: userText },
  ];

  for (let i = 0; i < MAX_TOOL_ITERATIONS; i++) {
    const res = await fetch(OLLAMA_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: modelKey,
        messages,
        ...(tools ? { tools, tool_choice: 'auto' } : {}),
        max_tokens: 2048,
        temperature: 0.6,
        think: false, // Ollama-native flag; harmless if this endpoint/model ignores it
      }),
    });
    if (!res.ok) throw new Error(`Ollama responded ${res.status}`);
    const data = await res.json();
    const message = data.choices?.[0]?.message;
    if (!message) throw new Error('No response from model');

    const toolCalls = message.tool_calls;
    if (!toolCalls || toolCalls.length === 0) {
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
        const result = await apiToolCall(name, args);
        resultText = typeof result === 'string' ? result : JSON.stringify(result);
      } catch (err) {
        resultText = JSON.stringify({ error: err.message });
      }
      onEvent?.({ type: 'tool_result', text: resultText });
      messages.push({ role: 'tool', tool_call_id: call.id || `call_${i}_${idx}`, content: resultText });
    }
    // loop: give the model the tool results and let it respond again
  }
  return '(stopped after too many tool-call steps — the model kept calling tools without finishing)';
}
