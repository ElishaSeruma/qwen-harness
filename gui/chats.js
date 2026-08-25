// gui/chats.js — persisted chats, context continuity, and auto-compaction.
//
// All of this degrades to a silent no-op when Supabase isn't connected
// (isSupabaseConnected() caches one status check) — Home keeps working
// exactly as before, just without cross-session memory, until credentials
// are set up via the Connectors panel.

import {
  apiSupabaseStatus, apiChatsList, apiChatCreate, apiChatMessages, apiChatAddMessage,
  apiChatTouch, apiChatTokens, apiChatCompact, apiChatCompaction, apiWorkDone,
} from './capabilities.js';
import { getModelContextLimit } from './agent-loop.js';

const ACTIVE_CHATS_KEY = 'qh-active-chats-v1'; // agentId -> chatId
const COMPACTION_THRESHOLD = 0.75;

let supabaseConnected = null;
export async function isSupabaseConnected() {
  if (supabaseConnected === null) {
    try { supabaseConnected = (await apiSupabaseStatus()).connected; }
    catch { supabaseConnected = false; }
  }
  return supabaseConnected;
}
export function invalidateSupabaseStatus() { supabaseConnected = null; }

function loadActiveChats() {
  try { return JSON.parse(localStorage.getItem(ACTIVE_CHATS_KEY) || '{}'); } catch { return {}; }
}
function saveActiveChats(map) { localStorage.setItem(ACTIVE_CHATS_KEY, JSON.stringify(map)); }

export function getActiveChatId(agentId) { return loadActiveChats()[agentId] || null; }
export function setActiveChatId(agentId, chatId) {
  const map = loadActiveChats();
  if (chatId) map[agentId] = chatId; else delete map[agentId];
  saveActiveChats(map);
}

/** Get (or lazily create) the active chat for this agent. Returns null if Supabase isn't connected. */
export async function ensureChat(agent) {
  if (!(await isSupabaseConnected())) return null;
  const existing = getActiveChatId(agent.id);
  if (existing) return existing;
  try {
    const contextLimit = await getModelContextLimit(agent.modelKey);
    const { chat } = await apiChatCreate({
      agent_id: agent.id, agent_name: agent.name, model_key: agent.modelKey, context_limit: contextLimit,
    });
    setActiveChatId(agent.id, chat.id);
    return chat.id;
  } catch { return null; }
}

export async function persistMessage(chatId, role, content, extra = {}) {
  if (!chatId) return;
  try { await apiChatAddMessage({ chat_id: chatId, role, content, ...extra }); } catch { /* best-effort */ }
}

export async function touchChat(chatId, title) {
  if (!chatId) return;
  try { await apiChatTouch(chatId, title); } catch { /* best-effort */ }
}

/**
 * Update the chat's token tally and run auto-compaction if it crosses 75%
 * of the model's real context window. `summarize` is a caller-provided
 * async function (prompt) => summaryText — kept decoupled from agent-loop
 * internals so this module doesn't need to know how to call the model.
 */
export async function trackUsageAndMaybeCompact(chatId, modelKey, totalTokens, summarize) {
  if (!chatId) return;
  try { await apiChatTokens(chatId, totalTokens); } catch { /* best-effort */ }

  const contextLimit = await getModelContextLimit(modelKey);
  if (!contextLimit || totalTokens < contextLimit * COMPACTION_THRESHOLD) return;

  try {
    const { messages } = await apiChatMessages(chatId);
    if (!messages || messages.length < 4) return; // not enough to bother compacting
    const transcript = messages.map(m => `${m.role}: ${m.content || ''}`).join('\n');
    const summary = await summarize(
      `Summarize this conversation concisely (under 250 words), preserving key facts, decisions, and open threads:\n\n${transcript}`
    );
    const lastId = messages[messages.length - 1]?.id;
    await apiChatCompact({
      chat_id: chatId, summary_text: summary, covers_up_to_message_id: lastId,
      token_count_before: totalTokens, token_count_after: Math.ceil(summary.length / 4),
    });
  } catch { /* best-effort — a failed compaction just means we keep sending full history */ }
}

/** Build the {role,content} history to prepend for continuing a chat — using the latest compaction if one exists. */
export async function buildPriorMessages(chatId) {
  if (!chatId) return [];
  try {
    const { compaction } = await apiChatCompaction(chatId);
    const { messages } = await apiChatMessages(chatId);
    if (compaction) {
      const idx = messages.findIndex(m => m.id === compaction.covers_up_to_message_id);
      const recent = idx >= 0 ? messages.slice(idx + 1) : messages.slice(-10);
      return [
        { role: 'system', content: `Earlier conversation summary: ${compaction.summary_text}` },
        ...recent.filter(m => m.content).map(m => ({ role: m.role, content: m.content })),
      ];
    }
    return messages.filter(m => m.content).map(m => ({ role: m.role, content: m.content }));
  } catch { return []; }
}

export async function listAllChats() {
  try { return (await apiChatsList()).chats || []; } catch { return []; }
}
export async function loadChatMessages(chatId) {
  try { return (await apiChatMessages(chatId)).messages || []; } catch { return []; }
}

export async function logWorkDone(agentId, actionType, filePath, chatId, summary) {
  try { await apiWorkDone({ agent_id: agentId, action_type: actionType, file_path: filePath, chat_id: chatId, summary }); }
  catch { /* best-effort */ }
}
