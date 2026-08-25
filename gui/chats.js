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

// The context the SERVER actually runs with (OLLAMA_CONTEXT_LENGTH in
// wake-up.sh and main.swift), not what a model advertises. qwen3:4b claims
// 262144, so compaction was triggering at ~196k tokens — i.e. never, while
// every turn silently overflowed the real 8192 window instead.
// Keep this in sync with OLLAMA_CONTEXT_LENGTH.
const EFFECTIVE_CONTEXT_TOKENS = 8192;

// Replay caps. One tool-result-laden message can be enormous, so bound by
// characters as well as count. ~16k chars is roughly 4k tokens, which leaves
// room for tools + system prompt + the reply inside 8192.
const MAX_REPLAY_MESSAGES = 20;
const MAX_REPLAY_CHARS = 16000;

/** Keep the most recent messages that fit both caps, oldest-first. */
function capMessages(messages) {
  const kept = [];
  let chars = 0;
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    const len = (m.content || '').length;
    if (kept.length >= MAX_REPLAY_MESSAGES || chars + len > MAX_REPLAY_CHARS) break;
    kept.push(m);
    chars += len;
  }
  return kept.reverse();
}

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

  // Clamp to the real server context — the advertised number is what made
  // this condition unreachable.
  const advertised = await getModelContextLimit(modelKey);
  const contextLimit = Math.min(advertised ?? EFFECTIVE_CONTEXT_TOKENS, EFFECTIVE_CONTEXT_TOKENS);
  if (totalTokens < contextLimit * COMPACTION_THRESHOLD) return;

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
    // Independent calls — allSettled rather than all, so a failed compaction
    // lookup degrades to "full (capped) history" instead of discarding a
    // perfectly good message list and returning no continuity at all.
    const [compactionResult, messagesResult] = await Promise.allSettled([
      apiChatCompaction(chatId),
      apiChatMessages(chatId),
    ]);
    if (messagesResult.status !== 'fulfilled') return [];
    const messages = (messagesResult.value.messages || []).filter(m => m.content);
    const compaction = compactionResult.status === 'fulfilled' ? compactionResult.value.compaction : null;

    if (compaction) {
      const idx = messages.findIndex(m => m.id === compaction.covers_up_to_message_id);
      const recent = idx >= 0 ? messages.slice(idx + 1) : messages;
      return [
        { role: 'system', content: `Earlier conversation summary: ${compaction.summary_text}` },
        ...capMessages(recent).map(m => ({ role: m.role, content: m.content })),
      ];
    }
    // Previously unbounded: a long chat replayed its ENTIRE history every turn.
    return capMessages(messages).map(m => ({ role: m.role, content: m.content }));
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
