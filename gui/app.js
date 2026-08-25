// gui/app.js — Control Room for qwen-harness.
// Home: chat-first instruction dispatch to one agent or all of them.
// Code: real filesystem browsing/editing + terminal + browser preview.
// Agent hub: rename agents/avatars, manage the roster, and a tucked-away
// console for scripted demo runs / direct live chat with one agent.

import { renderFileTree, withPaths } from './filetree.js';
import { runAgentTurn } from './agent-loop.js';
import { createActivityIndicator } from './activity.js';
import { createMessageActions, renderMarkdown } from './message-renderer.js';
import { beginRun, cancelRun, emitRunEvent, failRun, finishRun, listRuns, updateRun } from './runs.js';
import { createTaskPlan, executeTaskGraph } from './orchestration.js';
import { escalateEffort, getEffort, lockEffort, mountEffortGauge } from './effort.js';
import {
  ensureChat, persistMessage, touchChat, trackUsageAndMaybeCompact, buildPriorMessages,
  getActiveChatId, setActiveChatId, isSupabaseConnected, invalidateSupabaseStatus,
  listAllChats, loadChatMessages, logWorkDone,
} from './chats.js';
import {
  apiListDir, apiReadFile, apiWriteFile, apiExec, apiHealth, apiSystemStats,
  apiFsGrants, apiFsGrant, apiCreateFile, apiCreateDir, apiMovePath, apiDeletePath,
  apiProjects, apiProjectCreate, apiProjectAddDocs, apiProjectReadDoc, apiProjectDeleteDoc, apiProjectRenameDoc,
  apiProjectIndex, apiProjectSearch, apiProjectPublishGithub, apiProjectGitAction, apiProjectDelete, apiProjectSettings,
  apiWebSearch, apiWebScrape, apiGithubStatus, apiGithubConnect, apiGithubDisconnect, apiGithubRepos,
  apiSupabaseStatus, apiSupabaseConnect, apiSupabaseDisconnect,
  isDesktopClient, desktopChooseDirectory, desktopChooseFile,
  getDirectory, setDirectory, getHotphrases, setHotphrases,
  getConnectors, setConnectorEnabled, CONNECTOR_DEFS,
  trackComposerFocus, insertIntoLastComposer, getLastComposerValue,
  isScopeApproved, approveScopeForSession,
  getAttachments, clearAttachments, addAttachmentFiles, attachmentsAsPromptSuffix,
  renderCapabilityBar,
} from './capabilities.js';

const manifest = await fetch('data/manifest.json').then(r => r.json());
const modelsConfig = await fetch('data/models.json').then(r => r.json());
const demoScripts = await fetch('data/demo-scripts.json').then(r => r.json());

const avatarBySlug = new Map(manifest.map(a => [a.slug, a]));
const moduleCache = new Map();
async function loadAvatarModule(slug) {
  if (!moduleCache.has(slug)) {
    const entry = avatarBySlug.get(slug);
    moduleCache.set(slug, import(`./${entry.module}`));
  }
  return moduleCache.get(slug);
}

function displayName(agent) {
  return agent.nickname?.trim() || avatarBySlug.get(agent.slug)?.name || agent.slug;
}

const STATE_SEQUENCE = {
  sleeping: 'sleeping', waking: 'waking', idle: 'idle', listening: 'listening',
  thinking: 'thinking', searching: 'searching', working: 'working',
  happy: 'happy', excited: 'excited', curious: 'curious', suspicious: 'suspicious',
  proud: 'proud', confused: 'confused', surprised: 'surprised', angry: 'angry',
  bored: 'bored', drowsy: 'drowsy', scared: 'scared', playful: 'playful',
};
const ALL_STATES = Object.keys(STATE_SEQUENCE);
const $ = sel => document.querySelector(sel);
const sleep = ms => new Promise(r => setTimeout(r, ms));

// Normalize a user-pasted filesystem path: strip shell quotes, backslash
// escapes, file:// prefixes — Terminal drag-and-drop and "Copy as Pathname"
// all produce variants that otherwise read as garbage relative paths.
function cleanPathInput(raw) {
  let p = (raw || '').trim();
  while (p.length >= 2 && p[0] === p[p.length - 1] && (p[0] === '"' || p[0] === "'")) p = p.slice(1, -1).trim();
  p = p.replace(/\\ /g, ' ');
  if (p.startsWith('file://')) p = decodeURIComponent(p.slice(7));
  return p;
}


// ── input hygiene: no surprise auto-capitalization/autocorrect anywhere ────
// WebKit will capitalize the first letter of a plain text input unless told
// not to — which silently turns "cd" into "Cd" in the terminal. Apply the
// off-switch to every text input/textarea, including ones drawers/forms
// create dynamically (hotphrases, GitHub/Supabase tokens, project forms…).
function applyInputDefaults(el) {
  const isTextish = el.tagName === 'TEXTAREA'
    || (el.tagName === 'INPUT' && ['text', 'password', 'search'].includes(el.type));
  if (!isTextish) return;
  if (!el.hasAttribute('autocapitalize')) el.setAttribute('autocapitalize', 'off');
  if (!el.hasAttribute('autocorrect')) el.setAttribute('autocorrect', 'off');
  if (!el.hasAttribute('spellcheck')) el.setAttribute('spellcheck', 'false');
}
function wireAutoResize(el) {
  if (el._qhAutoResizeWired) return;
  el._qhAutoResizeWired = true;
  const resize = () => { el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight, 320)}px`; };
  resize();
  el.addEventListener('input', resize);
}
function applyInputEnhancements(root) {
  if (root.matches?.('input, textarea')) applyInputDefaults(root);
  root.querySelectorAll?.('input, textarea').forEach(applyInputDefaults);
  if (root.matches?.('textarea.composer-textarea')) wireAutoResize(root);
  root.querySelectorAll?.('textarea.composer-textarea').forEach(wireAutoResize);
}
applyInputEnhancements(document.body);
new MutationObserver((mutations) => {
  for (const m of mutations) {
    for (const node of m.addedNodes) {
      if (node instanceof HTMLElement) applyInputEnhancements(node);
    }
  }
}).observe(document.body, { childList: true, subtree: true });

// ── helper toasts: every error gets a "what to do about it" suggestion ────
const ERROR_SUGGESTIONS = [
  [/permission required/i, 'Grant access to that folder first — Code page → Open Folder, or the directory picker in the panel that errored.'],
  [/not a directory|no such file/i, 'Check the path exists and is spelled right. Paste it without quotes, or use a Choose/Open Folder button instead of typing.'],
  [/ollama responded|11434|failed to fetch/i, 'Ollama or the backend looks down — run ./wake-up.sh (or `ollama serve` + `python3 gui/server.py`) and try again.'],
  [/does not support tools/i, 'This model can\'t call tools. Toggle "Agent tools" off for it, or switch to a tool-capable model like qwen3:4b.'],
  [/supabase is not connected/i, 'Connect Supabase first: Connectors → Supabase, paste your Project URL + service_role key.'],
  [/github is not connected|bad credentials/i, 'Connect GitHub first: Connectors → GitHub, paste a Personal Access Token (repo scope).'],
  [/timed out/i, 'The command exceeded the 30s limit. Run long jobs in a real terminal, or split the work into smaller commands.'],
  [/file too large/i, 'This file is over the 3MB read cap — open it in a dedicated editor instead.'],
  [/pyautogui/i, 'The computer-control tools need pyautogui: run `pip3 install --break-system-packages pyautogui`.'],
  [/nothing to commit/i, 'There are no changes to commit — edit or add files first, then commit.'],
  [/not empty/i, 'That directory already has files in it. Pick an empty location or a different project name.'],
  [/blocked dangerous command/i, 'That command pattern is blocked as a safety guard (sudo, rm -rf, mkfs). Run it in a real terminal if you really mean it.'],
];
let lastErrorMessage = null;
function suggestionFor(message) {
  for (const [pattern, tip] of ERROR_SUGGESTIONS) if (pattern.test(message)) return tip;
  return 'Check the Server/Ollama status pills in the top bar — if either is red, ./wake-up.sh restarts everything.';
}
function showToast(message, { kind = 'error', suggestion = null, timeoutMs = 9000 } = {}) {
  const stack = $('#toast-stack');
  if (!stack) return;
  // Collapse duplicate back-to-back toasts (e.g. a poll failing repeatedly).
  if (stack.lastElementChild?.dataset.message === message) return;
  const toast = document.createElement('div');
  toast.className = `toast is-${kind}`;
  toast.dataset.message = message;
  const head = document.createElement('div');
  head.className = 'toast-head';
  const title = document.createElement('span');
  title.className = 'toast-title';
  title.textContent = kind === 'error' ? 'Error' : 'Notice';
  const close = document.createElement('button');
  close.className = 'toast-close'; close.type = 'button'; close.textContent = '×';
  close.addEventListener('click', () => toast.remove());
  head.append(title, close);
  const body = document.createElement('div');
  body.className = 'toast-message';
  body.textContent = message;
  toast.append(head, body);
  const tip = suggestion ?? (kind === 'error' ? suggestionFor(message) : null);
  if (tip) {
    const tipEl = document.createElement('div');
    tipEl.className = 'toast-suggestion';
    tipEl.innerHTML = `<strong>Try:</strong> ${tip}`;
    toast.append(tipEl);
  }
  stack.append(toast);
  while (stack.children.length > 4) stack.firstElementChild.remove();
  setTimeout(() => toast.remove(), timeoutMs);
}
// Persist a completed turn. Detached from the reply path (the user should
// never wait on Supabase), but internally ORDERED: sb_get_messages orders by
// created_at, so firing the user and assistant inserts in parallel can make a
// replayed conversation come back with the answer before the question.
function persistTurn(chatId, userText, assistantText, title) {
  if (!chatId) return;
  persistMessage(chatId, 'user', userText)
    .then(() => persistMessage(chatId, 'assistant', assistantText))
    .then(() => touchChat(chatId, title))
    .catch(err => console.warn('[chats] failed to persist turn:', err?.message || err));
}

// Shared approval handler for gated tools (write/edit/shell/computer).
// Honours the Auto-approve toggle and per-scope session grants so an agent
// can chain many edits without a modal per step, while still asking once
// for anything not yet trusted this session.
async function handleApprovalRequest({ tool, scope, prompt }) {
  const key = scope || tool;
  if (isScopeApproved(key)) return true;
  const choice = window.confirm(
    `${prompt}\n\nOK = allow this and everything else in "${key}" for this session.\nCancel = deny.`
  );
  if (choice) approveScopeForSession(key);
  return choice;
}

window.addEventListener('qh-api-error', (e) => {
  lastErrorMessage = e.detail.message;
  showToast(e.detail.message);
});
window.addEventListener('unhandledrejection', (e) => {
  const msg = e.reason?.message || String(e.reason);
  lastErrorMessage = msg;
  showToast(msg);
});

// ── persistence: agent roster ────────────────────────────────────────────
const AGENTS_KEY = 'qh-agents-v1';
const CODE_CHAT_KEY = 'qh-code-chat-v1';
const THEME_KEY = 'qh-theme-v1';
const ROUTE_KEY = 'qh-route-v1';

let agentSeq = 0;
function makeAgent(slug, name, modelKey, nickname = null) {
  agentSeq += 1;
  return {
    id: `agent-${agentSeq}`, name, slug, nickname, modelKey, state: 'sleeping',
    handle: null, stageHandle: null, hubHandle: null, greetingHandle: null, transcript: [],
  };
}
function persistAgents() {
  localStorage.setItem(AGENTS_KEY, JSON.stringify({
    agentSeq, activeAgentId,
    agents: agents.map(a => ({ id: a.id, name: a.name, slug: a.slug, nickname: a.nickname, modelKey: a.modelKey, state: 'idle' })),
  }));
}
function loadAgents() {
  try {
    const raw = localStorage.getItem(AGENTS_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!Array.isArray(data.agents) || data.agents.length === 0) return null;
    agentSeq = data.agentSeq || data.agents.length;
    const restored = data.agents.map(a => ({ ...makeAgent(a.slug, a.name, a.modelKey, a.nickname ?? null), id: a.id, state: 'idle' }));
    return { agents: restored, activeAgentId: data.activeAgentId };
  } catch { return null; }
}

const restored = loadAgents();
const agents = restored ? restored.agents : [
  makeAgent('strobi', 'hello', modelsConfig.active),
  makeAgent('freddy', 'file-agent', modelsConfig.active),
  makeAgent('nova', 'researcher', modelsConfig.active),
  makeAgent('citrus', 'writer', modelsConfig.active),
  makeAgent('grok-bot', 'critic', modelsConfig.active),
];
let activeAgentId = (restored && agents.some(a => a.id === restored.activeAgentId)) ? restored.activeAgentId : agents[0].id;

// ── durable activity drawer ────────────────────────────────────────────
const ACTIVE_RUN_STATES = new Set(['queued', 'planning', 'running', 'thinking', 'working', 'verifying', 'waiting_permission']);
let retryParentRun = null;
const surfaceActiveRuns = new Map();
const SURFACE_BUTTONS = { home: 'home-send', project: 'project-send', code: null, hub: null };

for (const [surface, input, button] of [
  ['home', $('#home-composer-input'), $('#home-send')],
  ['project', $('#project-composer-input'), $('#project-send')],
  ['project-agent', $('#project-agent-input'), $('#project-agent-send')],
  ['code', $('#code-composer-input'), $('#code-composer button[type="submit"]')],
  ['hub', $('#composer-input'), $('#composer button[type="submit"]')],
]) mountEffortGauge(surface, input, button);

function surfaceButton(surface) {
  if (SURFACE_BUTTONS[surface]) return $(`#${SURFACE_BUTTONS[surface]}`);
  if (surface === 'code') return $('#code-composer button[type="submit"]');
  if (surface === 'hub') return $('#composer button[type="submit"]');
  return null;
}

function markSurfaceRun(surface, run) {
  surfaceActiveRuns.set(surface, run.id);
  lockEffort(surface, true);
  const button = surfaceButton(surface);
  if (button) { button.textContent = 'Stop'; button.classList.add('is-stop'); }
}

function clearSurfaceRun(surface, runId) {
  if (surfaceActiveRuns.get(surface) !== runId) return;
  surfaceActiveRuns.delete(surface);
  lockEffort(surface, false);
  const button = surfaceButton(surface);
  if (button) {
    button.textContent = surface === 'home' ? 'Send →' : 'Send';
    button.classList.remove('is-stop');
  }
}

function effortForPrompt(surface, text) {
  const effort = getEffort(surface, text);
  return { effort, payload: { effort_level: effort.level, effort_source: effort.source } };
}

function effortRunOptions(surface, run) {
  return {
    effort: run.effort_level || 1,
    onEffortEscalation: async ({ level = 4, reason = 'Model requested additional reasoning' } = {}) => {
      escalateEffort(surface, level);
      await updateRun(run.id, { effort_level: level, effort_source: 'model', effort_reason: reason });
      await emitRunEvent(run.id, 'run.effort_escalated', { from: run.effort_level, to: level, reason });
      run.effort_level = level; run.effort_source = 'model';
    },
  };
}

async function stopSurfaceRun(surface) {
  const runId = surfaceActiveRuns.get(surface);
  if (!runId) return false;
  await cancelRun(runId);
  clearSurfaceRun(surface, runId);
  return true;
}

function consumeRunLineage() {
  if (!retryParentRun) return {};
  const lineage = { parent_run_id: retryParentRun.id, attempt: Number(retryParentRun.attempt || 1) + 1 };
  retryParentRun = null;
  return lineage;
}

function relativeRunTime(value) {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

// The badge in the topbar must keep counting while the drawer is CLOSED, but
// rebuilding 40 cards while nobody can see them is pure waste. One fetch,
// badge always, cards only when open.
async function refreshActivity() {
  let runs = [];
  try { runs = await listRuns(null, 40); } catch { return; }
  $('#activity-count').textContent = String(runs.filter(run => ACTIVE_RUN_STATES.has(run.status)).length);
  if ($('#activity-drawer').hidden) return;
  renderActivityDrawer(runs);
}

async function renderActivityDrawer(prefetched = null) {
  const list = $('#activity-list');
  if (!list) return;
  let runs = prefetched;
  if (!runs) {
    try { runs = await listRuns(null, 40); } catch { return; }
    $('#activity-count').textContent = String(runs.filter(run => ACTIVE_RUN_STATES.has(run.status)).length);
  }
  list.replaceChildren();
  if (!runs.length) {
    const empty = document.createElement('p'); empty.className = 'transcript-empty'; empty.textContent = 'No runs yet.'; list.append(empty); return;
  }
  for (const run of runs) {
    const card = document.createElement('article'); card.className = 'run-card'; card.dataset.runId = run.id;
    const head = document.createElement('div'); head.className = 'run-card-head';
    head.append(createActivityIndicator(run.status, { size: 20, detail: run.last_event_type || run.events?.at(-1)?.type || run.surface }));
    const effortNames = ['Quick', 'Focus', 'Deep', 'Max'];
    const meta = document.createElement('span'); meta.className = 'run-card-meta'; meta.textContent = `${effortNames[(run.effort_level || 1) - 1]} ${run.effort_level || 1}/4 · #${run.attempt || 1} · ${relativeRunTime(run.updated_at)}`; head.append(meta);
    const instruction = document.createElement('p'); instruction.className = 'run-card-instruction'; instruction.textContent = run.instruction || '(no instruction)';
    const taskList = document.createElement('div'); taskList.className = 'run-task-list';
    for (const task of run.tasks || []) {
      const row = document.createElement('div'); row.className = 'run-task-row';
      row.append(createActivityIndicator(task.status || 'queued', { size: 14, label: false }));
      const copy = document.createElement('span'); copy.textContent = task.title;
      const owner = document.createElement('small'); owner.textContent = task.agent_id || task.role || '';
      row.append(copy, owner); taskList.append(row);
    }
    const footer = document.createElement('div'); footer.className = 'run-card-actions';
    if (ACTIVE_RUN_STATES.has(run.status)) {
      const stop = document.createElement('button'); stop.type = 'button'; stop.textContent = 'Stop';
      stop.addEventListener('click', async () => { await cancelRun(run.id); renderActivityDrawer(); });
      footer.append(stop);
    } else {
      const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Retry';
      retry.addEventListener('click', () => window.dispatchEvent(new CustomEvent('qh-retry-run', { detail: run })));
      footer.append(retry);
    }
    card.append(head, instruction); if (taskList.childElementCount) card.append(taskList); card.append(footer); list.append(card);
  }
}

function setActivityDrawer(open) {
  $('#activity-drawer').hidden = !open;
  $('#activity-toggle').setAttribute('aria-expanded', String(open));
  if (open) renderActivityDrawer();
}
$('#activity-toggle').addEventListener('click', () => setActivityDrawer($('#activity-drawer').hidden));
$('#activity-close').addEventListener('click', () => setActivityDrawer(false));
// Run events stream in fast during inference; without a trailing debounce the
// drawer rebuilt all 40 cards per event, on top of the interval poll.
let activityRerenderTimer = null;
function scheduleActivityRerender() {
  clearTimeout(activityRerenderTimer);
  activityRerenderTimer = setTimeout(refreshActivity, 250);
}
window.addEventListener('qh-run-changed', scheduleActivityRerender);
window.addEventListener('qh-run-event', scheduleActivityRerender);

// Persisted agents may reference a model key that no longer exists in
// config.py (e.g. after swapping the registry for lighter models) — fall
// back to the current active model rather than silently pointing at a
// dead key.
const knownModelKeys = new Set(modelsConfig.models.map(m => m.key));
let hadStaleModelKey = false;
for (const agent of agents) {
  if (!knownModelKeys.has(agent.modelKey)) {
    agent.modelKey = modelsConfig.active;
    hadStaleModelKey = true;
  }
}
if (hadStaleModelKey) persistAgents();

async function mountInto(el, slug, animation) {
  el.replaceChildren();
  const mod = await loadAvatarModule(slug);
  const host = document.createElement('div');
  host.style.width = '100%'; host.style.height = '100%';
  el.append(host);
  return mod.createAvatar(host, { size: '100%', animation, autoplay: true });
}

function agentBadge(agent, fallbackLabel = 'agent') {
  const wrap = document.createElement('div');
  wrap.className = 'chat-agent-badge';
  if (agent) wrap.dataset.agentId = agent.id;
  const avatar = document.createElement('div');
  avatar.className = 'chat-agent-avatar';
  const meta = document.createElement('div');
  meta.className = 'chat-agent-meta';
  const name = document.createElement('strong');
  name.textContent = agent?.name || fallbackLabel;
  const state = document.createElement('span');
  state.className = 'chat-agent-state';
  state.append(createActivityIndicator(agent?.state || 'idle', { size: 15, label: displayName(agent || { slug: fallbackLabel }) }));
  meta.append(name, state);
  wrap.append(avatar, meta);
  if (agent) mountInto(avatar, agent.slug, STATE_SEQUENCE[agent.state] || 'idle').catch(() => {});
  return wrap;
}

function activeAgent() {
  return agents.find(agent => agent.id === activeAgentId) || agents[0];
}

function stampEntry(entry) {
  if (!entry.created_at) entry.created_at = new Date().toISOString();
  return entry;
}

function renderChatEntry(agent, entry, fallbackLabel = 'agent', surface = currentSurface()) {
  stampEntry(entry);
  const wrap = document.createElement('div');
  wrap.className = 'chat-entry-wrap';
  wrap.dataset.surface = surface;
  if (entry.type === 'user' || entry.type === 'assistant') wrap.append(agentBadge(agent, fallbackLabel));
  const message = renderEntry(entry);
  if (entry.type === 'user' || entry.type === 'assistant') message.append(createMessageActions(entry));
  wrap.append(message);
  return wrap;
}

function setAgentState(agent, state) {
  agent.state = state;
  const seq = STATE_SEQUENCE[state] || 'idle';
  if (agent.handle) agent.handle.play(seq);
  if (agent.hubHandle) agent.hubHandle.play(seq);
  if (agent.greetingHandle) agent.greetingHandle.play(seq);
  if (agent.id === activeAgentId && agent.stageHandle) agent.stageHandle.play(seq);
  if (agent.id === activeAgentId) {
    $('#stage-state').dataset.state = ['idle', 'sleeping', 'waking'].includes(state) ? 'off' : 'ok';
    $('#stage-state-label').textContent = state;
    $('#state-picker').value = state;
  }
  const hubBadge = document.querySelector(`.hub-card[data-agent-id="${agent.id}"] .roster-state`);
  if (hubBadge) hubBadge.replaceChildren(createActivityIndicator(agent.state, { size: 15, label: agent.state }));
  document.querySelectorAll(`.chat-agent-badge[data-agent-id="${agent.id}"] .chat-agent-state`).forEach(el => {
    el.replaceChildren(createActivityIndicator(agent.state || 'idle', { size: 15, label: displayName(agent) }));
  });
  persistAgents();
}

// ── entry / transcript rendering (shared: home feed, code chat, hub console) ──
function renderEntry(entry) {
  if (entry.type === 'system') {
    const div = document.createElement('div');
    div.className = 'msg msg-system';
    div.textContent = entry.text;
    return div;
  }
  if (entry.type === 'tool_call') {
    const div = document.createElement('div');
    div.className = 'msg msg-tool';
    const head = document.createElement('span'); head.className = 'tool-head'; head.textContent = `Tool: ${entry.name || 'unknown'}`;
    const args = document.createElement('span'); args.textContent = ` (${JSON.stringify(entry.args || {})})`;
    div.append(head, args);
    return div;
  }
  if (entry.type === 'tool_result') {
    const div = document.createElement('div');
    div.className = 'msg msg-tool msg-tool-result';
    div.textContent = `→ ${entry.text}`;
    return div;
  }
  const div = document.createElement('div');
  div.className = `msg msg-${entry.type === 'user' ? 'user' : 'assistant'}`;
  if (entry.role) { const role = document.createElement('span'); role.className = 'msg-role'; role.textContent = entry.role; div.append(role); }
  div.append(renderMarkdown(entry.text));
  return div;
}

function updateStreamingMessage(container, agent, key, text, fallbackLabel = 'agent') {
  let wrap = container.querySelector(`.chat-entry-wrap[data-stream-key="${CSS.escape(key)}"]`);
  if (!wrap) {
    container.querySelector('.transcript-empty')?.remove();
    wrap = document.createElement('div'); wrap.className = 'chat-entry-wrap is-streaming'; wrap.dataset.streamKey = key;
    wrap.append(agentBadge(agent, fallbackLabel));
    const message = document.createElement('div'); message.className = 'msg msg-assistant'; wrap.append(message);
    container.append(wrap);
  }
  wrap.querySelector('.msg').replaceChildren(renderMarkdown(text || ' '));
  container.scrollTop = container.scrollHeight;
}

function clearStreamingMessage(container, key) {
  container.querySelector(`.chat-entry-wrap[data-stream-key="${CSS.escape(key)}"]`)?.remove();
}
function appendEntry(agent, entry) {
  stampEntry(entry);
  agent.transcript.push(entry);
  if (agent.id === activeAgentId) {
    $('#transcript').querySelector('.transcript-empty')?.remove();
    $('#transcript').append(renderChatEntry(agent, entry, agent.name, 'hub'));
    $('#transcript').scrollTop = $('#transcript').scrollHeight;
  }
}

function currentSurface() {
  if (!$('#view-projects').hidden) return 'project';
  if (!$('#view-code').hidden) return 'code';
  if (!$('#view-hub').hidden) return 'hub';
  return 'home';
}

function composerForSurface(surface = currentSurface()) {
  return {
    home: $('#home-composer-input'), project: $('#project-composer-input'),
    code: $('#code-composer-input'), hub: $('#composer-input'),
  }[surface];
}

async function submitSurface(surface = currentSurface()) {
  if (surface === 'project') return sendProjectMessage();
  if (surface === 'code') return $('#code-composer').requestSubmit();
  if (surface === 'hub') { setHubMode('live'); return $('#composer').requestSubmit(); }
  return sendHomeMessage();
}

function removeEditedEntry(surface, entry) {
  if (surface === 'code') {
    const index = codeChatHistory.indexOf(entry);
    if (index >= 0) codeChatHistory.splice(index, 1);
    saveCodeChat();
  } else if (surface === 'project') {
    const project = activeProject();
    if (project) {
      projectMainFeed[project.id] = (projectMainFeed[project.id] || []).filter(row => row.entry !== entry);
      projectAgentFeed[project.id] = (projectAgentFeed[project.id] || []).filter(row => row.entry !== entry);
      persistProjectMainFeed();
      persistProjectAgentFeed();
    }
  }
  for (const agent of agents) agent.transcript = agent.transcript.filter(item => item !== entry);
  persistAgents();
}

async function openInlinePromptEditor(entry, source) {
  const wrap = source?.closest('.chat-entry-wrap');
  const message = source?.closest('.msg');
  if (!wrap || !message || wrap.classList.contains('is-editing')) return;
  const surface = wrap.dataset.surface || currentSurface();
  await stopSurfaceRun(surface);
  wrap.classList.add('is-editing');

  const editor = document.createElement('div');
  editor.className = 'message-inline-editor';
  const textarea = document.createElement('textarea');
  textarea.value = String(entry.text || '');
  textarea.setAttribute('aria-label', 'Edit prompt');
  const controls = document.createElement('div');
  controls.className = 'message-inline-editor-actions';
  const cancel = document.createElement('button');
  cancel.type = 'button'; cancel.textContent = '×'; cancel.title = 'Cancel edit'; cancel.setAttribute('aria-label', 'Cancel edit');
  const send = document.createElement('button');
  send.type = 'button'; send.textContent = '↑'; send.title = 'Send edited prompt'; send.setAttribute('aria-label', 'Send edited prompt');
  controls.append(cancel, send); editor.append(textarea, controls);

  const restore = () => {
    wrap.classList.remove('is-editing');
    message.replaceChildren(renderMarkdown(entry.text), createMessageActions(entry));
  };
  cancel.addEventListener('click', restore);
  const submitEdit = async () => {
    const text = textarea.value.trim();
    if (!text) return;
    entry.text = text;
    removeEditedEntry(surface, entry);
    wrap.remove();
    const input = composerForSurface(surface);
    if (!input) return;
    input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await submitSurface(surface);
  };
  send.addEventListener('click', submitEdit);
  textarea.addEventListener('keydown', event => {
    if (event.key === 'Escape') restore();
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') submitEdit();
  });
  message.replaceChildren(editor);
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

window.addEventListener('qh-message-action', async ({ detail: { action, entry, source } }) => {
  const text = String(entry?.text || '');
  if (action === 'copy') return navigator.clipboard.writeText(text);
  if (action === 'edit') return openInlinePromptEditor(entry, source);
  const input = composerForSurface();
  if (!input) return;
  if (action === 'quote') input.value = `${input.value}${input.value ? '\n\n' : ''}> ${text.replace(/\n/g, '\n> ')}`;
  if (action === 'retry') input.value = text;
  input.focus();
  input.dispatchEvent(new Event('input', { bubbles: true }));
  if (action === 'retry') await submitSurface();
});

let selectedMessageText = '';
document.addEventListener('mouseup', event => {
  if (event.target.closest('#selection-actions')) return;
  const selection = window.getSelection();
  const text = selection?.toString().trim() || '';
  const insideMessage = selection?.anchorNode?.parentElement?.closest?.('.msg');
  const toolbar = $('#selection-actions');
  if (!text || !insideMessage) { toolbar.hidden = true; selectedMessageText = ''; return; }
  selectedMessageText = text.slice(0, 8000);
  const rect = selection.getRangeAt(0).getBoundingClientRect();
  toolbar.style.left = `${Math.max(8, Math.min(window.innerWidth - 280, rect.left))}px`;
  toolbar.style.top = `${Math.max(8, rect.top - 42)}px`;
  toolbar.hidden = false;
});

$('#selection-actions').addEventListener('click', async event => {
  const action = event.target.closest('button')?.dataset.selectionAction;
  if (!action || !selectedMessageText) return;
  if (action === 'copy') await navigator.clipboard.writeText(selectedMessageText);
  else {
    const prefix = action === 'quote' ? '> ' : action === 'explain' ? 'Explain this selection:\n\n' : 'Improve this selection:\n\n';
    const body = action === 'quote' ? selectedMessageText.replace(/\n/g, '\n> ') : selectedMessageText;
    const input = composerForSurface();
    input.value = `${input.value}${input.value ? '\n\n' : ''}${prefix}${body}`;
    input.focus(); input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  $('#selection-actions').hidden = true;
  window.getSelection()?.removeAllRanges();
});

// ═══════════════════════════════════════════════════════════════════════
// HOME — chat-first instruction dispatch
// ═══════════════════════════════════════════════════════════════════════
function computeGreeting() {
  const h = new Date().getHours();
  if (h < 5) return 'Good night';
  if (h < 12) return 'Good morning';
  if (h < 17) return 'Good afternoon';
  if (h < 21) return 'Good evening';
  return 'Good night';
}
function renderTOD() {
  $('#home-greeting-text').textContent = computeGreeting();
  $('#home-date').textContent = new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
}

function renderAttachmentChips() {
  const list = $('#home-attachment-list');
  list.replaceChildren();
  for (const [i, att] of getAttachments().entries()) {
    const chip = document.createElement('span');
    chip.className = 'attachment-chip';
    chip.append(document.createTextNode(`📎 ${att.name}`));
    const rm = document.createElement('button');
    rm.type = 'button'; rm.textContent = '×';
    rm.addEventListener('click', () => { getAttachments().splice(i, 1); renderAttachmentChips(); });
    chip.append(rm);
    list.append(chip);
  }
}

function populateHomeSelectors() {
  const targetSel = $('#home-agent-target');
  targetSel.replaceChildren();
  const autoOpt = document.createElement('option');
  autoOpt.value = '__auto__'; autoOpt.textContent = 'Auto plan & assign';
  targetSel.append(autoOpt);
  const broadcastOpt = document.createElement('option');
  broadcastOpt.value = '__all__'; broadcastOpt.textContent = 'Broadcast to all';
  targetSel.append(broadcastOpt);
  for (const a of agents) {
    const opt = document.createElement('option');
    opt.value = a.id; opt.textContent = a.name;
    if (a.id === activeAgentId) opt.selected = true;
    targetSel.append(opt);
  }
  const modelSel = $('#home-model-select');
  modelSel.replaceChildren();
  for (const m of modelsConfig.models) {
    const opt = document.createElement('option');
    opt.value = m.key; opt.textContent = m.key;
    if (m.key === modelsConfig.active) opt.selected = true;
    modelSel.append(opt);
  }
}

// Avatar(s) shown top-left of the Home TOD greeting — the currently
// selected "Send to" agent (large, live state), or every agent at once
// (small, each in its own live state) when "Broadcast to all" is picked.
async function renderHomeGreetingAvatars() {
  const container = $('#home-greeting-avatars');
  if (!container) return;
  for (const agent of agents) agent.greetingHandle = null;
  container.replaceChildren();

  const targetId = $('#home-agent-target').value;
  if (targetId === '__all__') {
    await Promise.all(agents.map(async (agent) => {
      const cell = document.createElement('div');
      cell.className = 'home-greeting-avatar-mini';
      const face = document.createElement('div');
      face.className = 'home-greeting-avatar-mini-face';
      const label = document.createElement('span');
      label.className = 'home-greeting-avatar-mini-label';
      label.textContent = agent.name;
      cell.append(face, label);
      container.append(cell);
      agent.greetingHandle = await mountInto(face, agent.slug, STATE_SEQUENCE[agent.state] || 'idle');
    }));
    return;
  }

  const agent = agents.find(a => a.id === targetId) || agents[0];
  if (!agent) return;
  const face = document.createElement('div');
  face.className = 'home-greeting-avatar-single';
  container.append(face);
  agent.greetingHandle = await mountInto(face, agent.slug, STATE_SEQUENCE[agent.state] || 'idle');
}
$('#home-agent-target').addEventListener('change', renderHomeGreetingAvatars);

function homeFeedAppend(agentLabel, entry) {
  stampEntry(entry);
  const feed = $('#home-feed');
  feed.querySelector('.transcript-empty')?.remove();
  const agent = agents.find(a => a.name === agentLabel || a.id === agentLabel);
  feed.append(renderChatEntry(agent, entry, agentLabel, 'home'));
  feed.scrollTop = feed.scrollHeight;
}

function feedForSurface(surface) {
  return surface === 'project' ? $('#project-main-feed')
    : surface === 'code' ? $('#code-transcript')
      : surface === 'hub' ? $('#transcript') : $('#home-feed');
}

function appendSurfaceEntry(surface, project, agent, entry) {
  if (surface === 'project') appendProjectMainFeed(project, agent?.name || 'orchestrator', entry);
  else if (surface === 'code') appendCodeEntry(entry);
  else if (surface === 'hub') appendEntry(agent || activeAgent(), entry);
  else homeFeedAppend(agent?.name || 'orchestrator', entry);
  if (agent && !['code', 'hub'].includes(surface)) appendEntry(agent, entry);
}

async function runAutoOrchestration({ surface, instruction, project = null, modelKey, pool }) {
  if (await stopSurfaceRun(surface)) return;
  const { payload: effortPayload } = effortForPrompt(surface, instruction);
  const cwd = project?.path || currentTreeRoot || getDirectory() || '.';
  const context = project ? await projectDocsContext(project) : '';
  const { run, controller } = await beginRun({
    surface, instruction, project_id: project?.id || null, cwd, model_key: modelKey,
    agent_ids: pool.map(agent => agent.id), ...effortPayload, ...consumeRunLineage(),
  });
  markSurfaceRun(surface, run);
  const coordinator = pool[0] || activeAgent();
  try {
    await updateRun(run.id, { status: 'planning' });
    setAgentState(coordinator, 'thinking');
    const tasks = await createTaskPlan({ instruction, agents: pool, modelKey, context, signal: controller.signal, runId: run.id, ...effortRunOptions(surface, run) });
    await updateRun(run.id, { status: 'running', tasks });
    appendSurfaceEntry(surface, project, coordinator, { type: 'system', text: `Plan created: ${tasks.length} tasks across ${pool.length} agents.` });
    const completed = await executeTaskGraph({
      tasks, agents: pool, signal: controller.signal,
      onChange: async (rows, task) => {
        const safeRows = rows.map(row => ({ ...row, output: row.output?.slice(0, 5000) || null }));
        await updateRun(run.id, { tasks: safeRows });
        await emitRunEvent(run.id, `task.${task.status}`, { task_id: task.id, title: task.title, agent_id: task.agent_id, error: task.error });
      },
      worker: async (task, agent, dependencyContext) => {
        setAgentState(agent, 'thinking');
        appendSurfaceEntry(surface, project, agent, { type: 'system', text: `${task.title} · assigned to ${agent.name}` });
        const streamKey = `${run.id}:${task.id}`;
        const reply = await runAgentTurn({
          modelKey: agent.modelKey || modelKey, signal: controller.signal, runId: run.id, cwd, toolProfile: 'project',
          ...effortRunOptions(surface, run),
          userText: `${task.instruction}\n\nDependency handoff:\n${dependencyContext || '(none)'}`,
          systemPrompt: `You are the ${task.role} agent in a coordinated task graph. Complete only your assigned task. Use project tools when needed. Return a concise handoff containing work performed, files changed, evidence, and remaining risks.\n${context}`,
          onToken: content => updateStreamingMessage(feedForSurface(surface), agent, streamKey, content, agent.name),
          onEvent: event => {
            if (event.type === 'tool_call') { setAgentState(agent, 'working'); emitRunEvent(run.id, 'agent.tool.started', { task_id: task.id, agent_id: agent.id, tool: event.name, args: event.args }); }
            if (event.type === 'tool_result') emitRunEvent(run.id, 'agent.tool.completed', { task_id: task.id, agent_id: agent.id });
          },
        });
        clearStreamingMessage(feedForSurface(surface), streamKey);
        appendSurfaceEntry(surface, project, agent, { type: 'assistant', text: reply });
        setAgentState(agent, 'idle');
        return reply;
      },
    });

    await updateRun(run.id, { status: 'verifying', tasks: completed });
    setAgentState(coordinator, 'thinking');
    const handoffs = completed.map(task => `${task.status.toUpperCase()} ${task.title} (${task.agent_id}):\n${task.output || task.error || ''}`).join('\n\n');
    const finalKey = `${run.id}:synthesis`;
    const synthesis = await runAgentTurn({
      modelKey, signal: controller.signal, runId: run.id, cwd, toolsEnabled: false,
      ...effortRunOptions(surface, run),
      systemPrompt: 'You are the lead verifier. Synthesize the task handoffs into one direct final response. State what was completed, verification evidence, failures, and remaining risks. Do not claim work that the handoffs do not prove.',
      userText: `Original instruction:\n${instruction}\n\nTask handoffs:\n${handoffs.slice(0, 26000)}`,
      onToken: content => updateStreamingMessage(feedForSurface(surface), coordinator, finalKey, content, coordinator.name),
    });
    clearStreamingMessage(feedForSurface(surface), finalKey);
    appendSurfaceEntry(surface, project, coordinator, { type: 'assistant', text: synthesis });
    await finishRun(run.id, { synthesis, tasks: completed });
  } catch (error) {
    if (error.name !== 'AbortError') {
      appendSurfaceEntry(surface, project, coordinator, { type: 'system', text: `Orchestration failed: ${error.message}` });
      await failRun(run.id, error);
    }
  } finally {
    pool.forEach(agent => setAgentState(agent, 'idle'));
    clearSurfaceRun(surface, run.id);
  }
}

async function sendHomeMessage() {
  if (await stopSurfaceRun('home')) return;
  const input = $('#home-composer-input');
  const text = input.value.trim();
  if (!text && getAttachments().length === 0) return;
  const fullText = text + attachmentsAsPromptSuffix();
  const targetId = $('#home-agent-target').value;
  const modelKey = $('#home-model-select').value;
  const targets = targetId === '__all__' ? agents : agents.filter(a => a.id === targetId);
  const project = activeProject();
  const docsContext = project ? await projectDocsContext(project) : '';
  const projectContext = project ? ` Active project: ${project.name} at ${project.path}.` : '';
  if (targetId === '__auto__') {
    input.value = ''; clearAttachments(); renderAttachmentChips();
    return runAutoOrchestration({ surface: 'home', instruction: fullText, project, modelKey, pool: agents });
  }
  const cwd = project?.path || getDirectory() || '.';
  const { payload: effortPayload } = effortForPrompt('home', fullText);
  const { run, controller } = await beginRun({
    surface: 'home', instruction: fullText, project_id: project?.id || null,
    cwd, model_key: modelKey, agent_ids: targets.map(agent => agent.id), ...effortPayload, ...consumeRunLineage(),
  });
  markSurfaceRun('home', run);
  input.value = '';
  clearAttachments();
  renderAttachmentChips();

  for (const agent of targets) {
    const entry = stampEntry({ type: 'user', text: fullText });
    homeFeedAppend(agent.name, entry);
    appendEntry(agent, entry);
    setAgentState(agent, 'listening');
  }
  await sleep(200);
  for (const agent of targets) {
    setAgentState(agent, 'thinking');
    emitRunEvent(run.id, 'agent.thinking', { agent_id: agent.id, agent_name: agent.name });
  }

  const outcomes = await Promise.all(targets.map(async (agent) => {
    const chatId = await ensureChat(agent);
    const priorMessages = await buildPriorMessages(chatId);
    try {
      const reply = await runAgentTurn({
        modelKey, userText: fullText, priorMessages,
        cwd, signal: controller.signal, runId: run.id, toolProfile: 'home',
        ...effortRunOptions('home', run),
        systemPrompt: `You are a helpful assistant in the qwen-harness Control Room.${projectContext} Use tools when they would genuinely help.\n${docsContext}`,
        onToken: (content) => updateStreamingMessage($('#home-feed'), agent, `${run.id}:${agent.id}`, content, agent.name),
        onApprovalRequest: handleApprovalRequest,
      onEvent: (ev) => {
          if (ev.type === 'usage') {
            trackUsageAndMaybeCompact(chatId, modelKey, ev.totalTokens, (prompt) =>
              runAgentTurn({ modelKey, userText: prompt, systemPrompt: 'You summarize conversations concisely.' })
            );
            return;
          }
          if (ev.type === 'tool_call') {
            setAgentState(agent, 'working');
            emitRunEvent(run.id, 'agent.tool.started', { agent_id: agent.id, tool: ev.name, args: ev.args });
            if (['write_file', 'edit_file', 'append_file'].includes(ev.name) && ev.args?.path) logWorkDone(agent.id, 'edit', ev.args.path, chatId);
            else if (ev.name === 'read_file' && ev.args?.path) logWorkDone(agent.id, 'read', ev.args.path, chatId);
          }
          if (ev.type === 'tool_result') emitRunEvent(run.id, 'agent.tool.completed', { agent_id: agent.id });
          if (ev.type === 'approval_required') emitRunEvent(run.id, 'agent.waiting_permission', { agent_id: agent.id, tool: ev.tool, scope: ev.scope });
          homeFeedAppend(agent.name, ev);
          appendEntry(agent, ev);
        },
      });
      // Chained, not fired in parallel: sb_get_messages orders by created_at,
      // so two simultaneous inserts can replay out of order.
      persistTurn(chatId, fullText, reply, fullText.slice(0, 80));
      const entry = { type: 'assistant', text: reply || '(empty response)' };
      clearStreamingMessage($('#home-feed'), `${run.id}:${agent.id}`);
      homeFeedAppend(agent.name, entry);
      appendEntry(agent, entry);
      setAgentState(agent, 'happy');
      emitRunEvent(run.id, 'agent.completed', { agent_id: agent.id });
      return { agent: agent.name, reply };
    } catch (err) {
      clearStreamingMessage($('#home-feed'), `${run.id}:${agent.id}`);
      if (err.name === 'AbortError') {
        setAgentState(agent, 'idle');
        return { agent: agent.name, cancelled: true };
      }
      const entry = { type: 'system', text: `⚠ ${err.message}. Is "ollama serve" running with ${modelKey} pulled?` };
      homeFeedAppend(agent.name, entry);
      appendEntry(agent, entry);
      setAgentState(agent, 'confused');
      emitRunEvent(run.id, 'agent.failed', { agent_id: agent.id, error: err.message });
      return { agent: agent.name, error: err.message };
    }
    await sleep(350);
    setAgentState(agent, 'idle');
  }));
  for (const agent of targets) setAgentState(agent, 'idle');
  if (controller.signal.aborted) { clearSurfaceRun('home', run.id); return; }
  const failures = outcomes.filter(result => result?.error);
  if (failures.length === outcomes.length) await failRun(run.id, failures.map(item => item.error).join('; '));
  else await finishRun(run.id, outcomes);
  clearSurfaceRun('home', run.id);
}

$('#home-send').addEventListener('click', sendHomeMessage);
$('#home-composer-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendHomeMessage(); }
});
trackComposerFocus($('#home-composer-input'));

// ═══════════════════════════════════════════════════════════════════════
// Shared capability bar + drawer
// ═══════════════════════════════════════════════════════════════════════
const drawer = $('#cap-drawer');
const drawerBody = $('#cap-drawer-body');
const drawerTitle = $('#cap-drawer-title');

function closeDrawer() { drawer.hidden = true; }
$('#cap-drawer-close').addEventListener('click', closeDrawer);

function openDrawer(capId, cwd) {
  drawer.hidden = false;
  if (capId === 'settings') return renderSettingsDrawer();
  if (capId === 'hotphrases') return renderHotphrasesDrawer();
  if (capId === 'upload') return renderUploadDrawer();
  if (capId === 'terminal') return renderTerminalDrawer(cwd);
  if (capId === 'browser') return renderBrowserDrawer();
  if (capId === 'github') return renderGithubDrawer();
  if (capId === 'websearch') return renderWebsearchDrawer();
  if (capId === 'chats') return renderChatsDrawer();
  if (capId === 'supabase') return renderSupabaseDrawer();
  if (capId === 'ai') return renderAiActionsDrawer();
}

// ── AI actions: one-click AI utilities over whatever you're working on ────
function aiContext() {
  return {
    draft: getLastComposerValue() || ($('#home-composer-input')?.value || ''),
    file: selectedFilePath ? { path: selectedFilePath, content: $('#code-editor').value } : null,
    feedText: $('#home-feed')?.innerText?.trim() || '',
    project: activeProject(),
  };
}
const AI_ACTIONS = [
  { id: 'summarize-chat', label: 'Summarize chat', hint: 'TL;DR of the Home feed', run: async (ctx) => {
      if (!ctx.feedText) throw new Error('No chat content on Home to summarize yet.');
      return aiRun(`Summarize this conversation in 5 bullet points or fewer:\n\n${ctx.feedText.slice(0, 6000)}`);
    } },
  { id: 'next-steps', label: 'Suggest next steps', hint: 'From the current chat', run: async (ctx) => {
      if (!ctx.feedText) throw new Error('No chat content on Home yet.');
      return aiRun(`Based on this conversation, list the 3-5 most useful concrete next steps:\n\n${ctx.feedText.slice(0, 6000)}`);
    } },
  { id: 'title-chat', label: 'Title this chat', hint: 'AI-names the active chat', run: async (ctx) => {
      if (!ctx.feedText) throw new Error('No chat content to title yet.');
      const title = await aiRun(`Give this conversation a short descriptive title (max 8 words). Reply with the title only:\n\n${ctx.feedText.slice(0, 3000)}`);
      const agent = agents.find(a => a.id === activeAgentId);
      const chatId = agent ? getActiveChatId(agent.id) : null;
      if (chatId) { await touchChat(chatId, title.replace(/^["']|["']$/g, '')); return `Chat renamed to: ${title}`; }
      return `Suggested title: ${title}\n(No persisted chat active — connect Supabase to save titles.)`;
    } },
  { id: 'explain-file', label: 'Explain open file', hint: 'Code page file walkthrough', run: async (ctx) => {
      if (!ctx.file) throw new Error('Open a file on the Code page first.');
      return aiRun(`Explain what this file does, section by section, concisely:\n\n--- ${ctx.file.path} ---\n${ctx.file.content.slice(0, 8000)}`);
    } },
  { id: 'review-file', label: 'Review file for bugs', hint: 'Find issues in open file', run: async (ctx) => {
      if (!ctx.file) throw new Error('Open a file on the Code page first.');
      return aiRun(`Review this code for bugs, edge cases, and risky patterns. List concrete findings only:\n\n--- ${ctx.file.path} ---\n${ctx.file.content.slice(0, 8000)}`);
    } },
  { id: 'gen-tests', label: 'Generate tests', hint: 'Test cases for open file', run: async (ctx) => {
      if (!ctx.file) throw new Error('Open a file on the Code page first.');
      return aiRun(`Write test cases for this code. Match its language and style:\n\n--- ${ctx.file.path} ---\n${ctx.file.content.slice(0, 8000)}`);
    } },
  { id: 'doc-file', label: 'Add docstrings', hint: 'Document the open file', run: async (ctx) => {
      if (!ctx.file) throw new Error('Open a file on the Code page first.');
      return aiRun(`Add concise docstrings/comments to this code. Return the full updated file:\n\n--- ${ctx.file.path} ---\n${ctx.file.content.slice(0, 8000)}`);
    } },
  { id: 'commit-msg', label: 'Commit message', hint: 'From the active project diff', run: async (ctx) => {
      if (!ctx.project) throw new Error('Select a project first (Projects page).');
      const diff = await apiExec('git diff HEAD --stat && git diff HEAD | head -300', ctx.project.path);
      const text = `${diff.stdout || ''}${diff.stderr || ''}`.trim();
      if (!text) throw new Error('No uncommitted changes in the active project.');
      return aiRun(`Write a one-line conventional commit message for this diff, then a short body if warranted:\n\n${text.slice(0, 6000)}`);
    } },
  { id: 'gen-readme', label: 'Draft README', hint: 'For the active project', run: async (ctx) => {
      if (!ctx.project) throw new Error('Select a project first (Projects page).');
      const listing = await apiExec('find . -maxdepth 2 -not -path "./.git*" | head -50', ctx.project.path);
      return aiRun(`Draft a README.md for a project named "${ctx.project.name}" with this file structure:\n\n${listing.stdout || '(empty project)'}`);
    } },
  { id: 'improve-draft', label: 'Improve draft', hint: 'Rewrite your composer text', run: async (ctx) => {
      if (!ctx.draft.trim()) throw new Error('Type something in a composer first.');
      return aiRun(`Rewrite this instruction to be clearer and more specific, keeping the intent. Reply with the rewrite only:\n\n${ctx.draft}`);
    } },
  { id: 'fix-grammar', label: 'Fix grammar', hint: 'Clean up your composer text', run: async (ctx) => {
      if (!ctx.draft.trim()) throw new Error('Type something in a composer first.');
      return aiRun(`Fix the grammar and spelling. Change nothing else. Reply with the corrected text only:\n\n${ctx.draft}`);
    } },
  { id: 'translate', label: 'Translate to English', hint: 'Composer text → English', run: async (ctx) => {
      if (!ctx.draft.trim()) throw new Error('Type something in a composer first.');
      return aiRun(`Translate to English. Reply with the translation only:\n\n${ctx.draft}`);
    } },
  { id: 'explain-error', label: 'Explain last error', hint: 'Diagnose the most recent toast', run: async () => {
      if (!lastErrorMessage) throw new Error('No error captured yet this session.');
      return aiRun(`This error appeared in a local dev tool (Ollama + Python backend + web GUI). Explain the likely cause and the fix, briefly:\n\n${lastErrorMessage}`);
    } },
];
function aiRun(prompt) {
  return runAgentTurn({
    modelKey: modelsConfig.active,
    userText: prompt,
    systemPrompt: 'You are a fast utility assistant inside the qwen-harness Control Room. Answer directly, no preamble.',
  });
}
function renderAiActionsDrawer() {
  drawerTitle.textContent = 'AI actions';
  drawerBody.replaceChildren();
  const grid = document.createElement('div');
  grid.className = 'ai-actions-grid';
  const resultWrap = document.createElement('div');
  for (const action of AI_ACTIONS) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ai-action-btn';
    btn.innerHTML = `${action.label}<small>${action.hint}</small>`;
    btn.addEventListener('click', async () => {
      resultWrap.replaceChildren();
      const pending = document.createElement('div');
      pending.className = 'ai-result';
      pending.textContent = `Running "${action.label}" on ${modelsConfig.active}…`;
      resultWrap.append(pending);
      try {
        const result = await action.run(aiContext());
        pending.textContent = result || '(empty response)';
        const actions = document.createElement('div');
        actions.className = 'ai-result-actions';
        const copyBtn = document.createElement('button');
        copyBtn.type = 'button'; copyBtn.className = 'mode-button'; copyBtn.textContent = 'Copy';
        copyBtn.addEventListener('click', () => navigator.clipboard.writeText(result).then(() => { copyBtn.textContent = 'Copied ✓'; }));
        const insertBtn = document.createElement('button');
        insertBtn.type = 'button'; insertBtn.className = 'mode-button'; insertBtn.textContent = 'Insert into composer';
        insertBtn.addEventListener('click', () => { insertIntoLastComposer(result); closeDrawer(); });
        actions.append(copyBtn, insertBtn);
        resultWrap.append(actions);
      } catch (err) {
        pending.textContent = `⚠ ${err.message}`;
      }
    });
    grid.append(btn);
  }
  drawerBody.append(grid, resultWrap);
}

async function renderSupabaseDrawer() {
  drawerTitle.textContent = 'Supabase';
  drawerBody.replaceChildren();
  const p = document.createElement('p');
  p.className = 'panel-copy';
  p.textContent = 'Loading…';
  drawerBody.append(p);

  let status;
  try { status = await apiSupabaseStatus(); }
  catch (err) { p.textContent = `⚠ ${err.message} — is gui/server.py running? (use wake-up.sh)`; return; }

  drawerBody.replaceChildren();

  if (status.connected) {
    const card = document.createElement('div');
    card.className = 'connector-row';
    const label = document.createElement('div');
    label.className = 'connector-label';
    const icon = document.createElement('span'); icon.textContent = '🗄';
    const url = document.createElement('span'); url.textContent = status.url;
    label.append(icon, url);
    card.append(label);
    const disconnectBtn = document.createElement('button');
    disconnectBtn.type = 'button'; disconnectBtn.className = 'mode-button mode-button-danger';
    disconnectBtn.textContent = 'Disconnect';
    disconnectBtn.addEventListener('click', async () => {
      await apiSupabaseDisconnect();
      invalidateSupabaseStatus();
      renderSupabaseDrawer();
    });
    card.append(disconnectBtn);
    drawerBody.append(card);
    const note = document.createElement('p');
    note.className = 'panel-copy';
    note.style.marginTop = '10px';
    note.textContent = 'Persisted chats, memory search, project runs, and work logging are active. Production schema migrations are versioned in supabase/migrations.';
    drawerBody.append(note);
  } else {
    const p2 = document.createElement('p');
    p2.className = 'panel-copy';
    p2.innerHTML = 'From your Supabase project → Settings → API: paste the Project URL and the <strong>service_role</strong> secret key (not the anon key, not the database password). Stored locally on this Mac only (<code>~/.qwen-harness/secrets.json</code>), never in the browser or in this repo.';
    const form = document.createElement('form');
    form.className = 'composer';
    form.style.flexDirection = 'column'; form.style.alignItems = 'stretch';
    form.innerHTML = `
      <input type="text" placeholder="https://xxxx.supabase.co" autocomplete="off" style="margin-bottom:8px;" />
      <input type="password" placeholder="service_role key" autocomplete="off" style="margin-bottom:8px;" />
      <button type="submit">Connect</button>`;
    const errorEl = document.createElement('p');
    errorEl.className = 'panel-copy';
    errorEl.style.color = 'var(--primary)';
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const [urlInput, keyInput] = form.querySelectorAll('input');
      const url = urlInput.value.trim(), key = keyInput.value.trim();
      if (!url || !key) return;
      form.querySelector('button').disabled = true;
      errorEl.textContent = 'Connecting…';
      try {
        await apiSupabaseConnect(url, key);
        invalidateSupabaseStatus();
        renderSupabaseDrawer();
      } catch (err) {
        errorEl.textContent = `⚠ ${err.message}`;
        form.querySelector('button').disabled = false;
      }
    });
    drawerBody.append(p2, form, errorEl);
  }
}

async function renderChatsDrawer() {
  drawerTitle.textContent = 'Chats';
  drawerBody.replaceChildren();
  if (!(await isSupabaseConnected())) {
    const p = document.createElement('p');
    p.className = 'panel-copy';
    p.textContent = 'Chats aren’t persisted yet — connect Supabase in Connectors → Supabase to save conversations across sessions.';
    drawerBody.append(p);
    return;
  }
  const p = document.createElement('p');
  p.className = 'panel-copy';
  p.textContent = 'Loading…';
  drawerBody.append(p);
  const chats = await listAllChats();
  drawerBody.replaceChildren();
  if (chats.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'panel-copy';
    empty.textContent = 'No chats yet — send a message on Home to start one.';
    drawerBody.append(empty);
    return;
  }
  const list = document.createElement('div');
  list.className = 'drawer-list';
  for (const chat of chats) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'drawer-list-row';
    const when = new Date(chat.updated_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    const title = document.createElement('strong');
    title.textContent = chat.title || '(untitled)';
    row.append(title, document.createTextNode(` — ${chat.agent_name || chat.agent_id} · ${chat.model_key} · ${when}`));
    row.addEventListener('click', () => loadChatIntoHome(chat));
    list.append(row);
  }
  drawerBody.append(list);
}

async function loadChatIntoHome(chat) {
  setActiveChatId(chat.agent_id, chat.id);
  const targetSel = $('#home-agent-target');
  if ([...targetSel.options].some(o => o.value === chat.agent_id)) targetSel.value = chat.agent_id;
  const feed = $('#home-feed');
  feed.replaceChildren();
  const p = document.createElement('p');
  p.className = 'transcript-empty';
  p.textContent = 'Loading…';
  feed.append(p);
  const messages = await loadChatMessages(chat.id);
  feed.replaceChildren();
  if (messages.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'transcript-empty';
    empty.textContent = 'This chat has no messages yet.';
    feed.append(empty);
  } else {
    for (const m of messages) {
      if (!m.content) continue;
      homeFeedAppend(chat.agent_name || chat.agent_id, { type: m.role === 'user' ? 'user' : 'assistant', text: m.content });
    }
  }
  closeDrawer();
}

$('#home-chats-btn').addEventListener('click', () => openDrawer('chats'));
$('#home-new-chat-btn').addEventListener('click', () => {
  const targetId = $('#home-agent-target').value;
  const affected = targetId === '__all__' ? agents : agents.filter(a => a.id === targetId);
  for (const agent of affected) setActiveChatId(agent.id, null);
  $('#home-feed').replaceChildren();
  const p = document.createElement('p');
  p.className = 'transcript-empty';
  p.textContent = 'Responses from the agent(s) you message will appear here.';
  $('#home-feed').append(p);
});

function renderSettingsDrawer() {
  drawerTitle.textContent = 'Connectors & plugins';
  drawerBody.replaceChildren();
  const state = getConnectors();
  for (const def of CONNECTOR_DEFS) {
    const row = document.createElement('div');
    row.className = 'connector-row';
    const label = document.createElement('div');
    label.className = 'connector-label';
    label.innerHTML = `<span>${def.icon}</span><span>${def.label}</span>`;
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'toggle-switch' + (state[def.id] ? ' is-on' : '');
    toggle.disabled = def.always || def.comingSoon;
    toggle.title = def.always ? 'Always on' : (def.comingSoon ? 'Coming soon' : 'Toggle');
    toggle.addEventListener('click', () => {
      const next = !state[def.id];
      setConnectorEnabled(def.id, next);
      renderAllCapabilityBars();
      renderSettingsDrawer();
    });
    row.append(label, toggle);
    drawerBody.append(row);
  }
}

function renderHotphrasesDrawer() {
  drawerTitle.textContent = 'Hotphrases';
  drawerBody.replaceChildren();
  const list = getHotphrases();
  const listEl = document.createElement('div');
  list.forEach((hp, i) => {
    const row = document.createElement('div');
    row.className = 'hotphrase-row';
    const chip = document.createElement('button');
    chip.type = 'button'; chip.className = 'hotphrase-chip';
    const label = document.createElement('strong');
    label.textContent = hp.label;
    chip.append(label, document.createTextNode(` — ${hp.phrase}`));
    chip.addEventListener('click', () => { insertIntoLastComposer(hp.phrase); closeDrawer(); });
    const rm = document.createElement('button');
    rm.type = 'button'; rm.className = 'hotphrase-remove'; rm.textContent = '×';
    rm.addEventListener('click', () => { list.splice(i, 1); setHotphrases(list); renderHotphrasesDrawer(); });
    row.append(chip, rm);
    listEl.append(row);
  });
  drawerBody.append(listEl);

  const addRow = document.createElement('div');
  addRow.className = 'hotphrase-add';
  const labelInput = document.createElement('input');
  labelInput.placeholder = 'Label';
  const phraseInput = document.createElement('input');
  phraseInput.placeholder = 'Phrase to insert';
  const addBtn = document.createElement('button');
  addBtn.type = 'button'; addBtn.className = 'mode-button'; addBtn.textContent = '+ Add';
  addBtn.addEventListener('click', () => {
    if (!phraseInput.value.trim()) return;
    list.push({ label: labelInput.value.trim() || 'phrase', phrase: phraseInput.value.trim() });
    setHotphrases(list);
    renderHotphrasesDrawer();
  });
  addRow.append(labelInput, phraseInput, addBtn);
  drawerBody.append(addRow);
}

function renderUploadDrawer() {
  drawerTitle.textContent = 'File upload';
  drawerBody.replaceChildren();
  const p = document.createElement('p');
  p.className = 'panel-copy';
  p.textContent = 'Files are read as text and attached to your next Home message.';
  const input = document.createElement('input');
  input.type = 'file'; input.multiple = true;
  input.addEventListener('change', () => {
    addAttachmentFiles(input.files, () => { renderAttachmentChips(); closeDrawer(); });
  });
  drawerBody.append(p, input);
}

function renderTerminalDrawer(cwd) {
  let liveCwd = cwd || getDirectory() || '.';
  drawerTitle.textContent = `Terminal — ${liveCwd}`;
  drawerBody.replaceChildren();
  const copyBtn = document.createElement('button');
  copyBtn.type = 'button'; copyBtn.className = 'mode-button terminal-copy-btn';
  copyBtn.textContent = 'Copy output';
  const output = document.createElement('div');
  output.className = 'transcript terminal-output';
  output.style.height = '160px';
  copyBtn.addEventListener('click', () => copyTerminalOutput(output, copyBtn));
  const form = document.createElement('form');
  form.className = 'composer';
  form.innerHTML = `<input type="text" placeholder="$ command" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" /><button type="submit">Run</button>`;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = form.querySelector('input');
    const command = input.value.trim();
    if (!command) return;
    input.value = '';
    const cmdLine = document.createElement('div');
    cmdLine.className = 'terminal-line-cmd';
    cmdLine.textContent = `$ ${command}`;
    output.append(cmdLine);
    try {
      const result = await apiExec(command, liveCwd);
      if (result.cwd) { liveCwd = result.cwd; drawerTitle.textContent = `Terminal — ${liveCwd}`; }
      if (result.stdout) { const pre = document.createElement('div'); pre.textContent = result.stdout; output.append(pre); }
      if (result.stderr) { const pre = document.createElement('div'); pre.className = 'terminal-line-err'; pre.textContent = result.stderr; output.append(pre); }
    } catch (err) {
      const pre = document.createElement('div'); pre.className = 'terminal-line-err';
      pre.textContent = `⚠ ${err.message} — is gui/server.py running? (use wake-up.sh)`;
      output.append(pre);
    }
    output.scrollTop = output.scrollHeight;
  });
  drawerBody.append(copyBtn, output, form);
}

function copyTerminalOutput(outputEl, btn) {
  const text = outputEl.innerText;
  navigator.clipboard.writeText(text).then(() => {
    const original = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => { btn.textContent = original; }, 1200);
  }).catch(() => { btn.textContent = 'Copy failed'; });
}

function renderBrowserDrawer() {
  drawerTitle.textContent = 'Browser preview';
  drawerBody.replaceChildren();
  const form = document.createElement('form');
  form.className = 'composer';
  form.innerHTML = `<input type="text" placeholder="http://localhost:3000" autocomplete="off" /><button type="submit">Go</button>`;
  const frame = document.createElement('iframe');
  frame.className = 'browser-preview-frame';
  frame.style.height = '260px';
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const url = form.querySelector('input').value.trim();
    if (url) frame.src = url;
  });
  drawerBody.append(form, frame);
}

async function renderGithubDrawer() {
  drawerTitle.textContent = 'GitHub';
  drawerBody.replaceChildren();
  const p = document.createElement('p');
  p.className = 'panel-copy';
  p.textContent = 'Loading…';
  drawerBody.append(p);

  let status;
  try { status = await apiGithubStatus(); }
  catch (err) { p.textContent = `⚠ ${err.message} — is gui/server.py running? (use wake-up.sh)`; return; }

  drawerBody.replaceChildren();

  if (status.connected) {
    const card = document.createElement('div');
    card.className = 'connector-row';
    const identity = document.createElement('div');
    identity.className = 'connector-label';
    const avatar = document.createElement('img');
    avatar.src = status.avatar_url || '';
    avatar.alt = ''; avatar.width = 28; avatar.height = 28;
    avatar.style.border = '2px solid var(--border)';
    const account = document.createElement('span');
    account.textContent = status.name ? `${status.name} (@${status.login})` : `@${status.login}`;
    identity.append(avatar, account);
    card.append(identity);
    const disconnectBtn = document.createElement('button');
    disconnectBtn.type = 'button'; disconnectBtn.className = 'mode-button mode-button-danger';
    disconnectBtn.textContent = 'Disconnect';
    disconnectBtn.addEventListener('click', async () => { await apiGithubDisconnect(); renderGithubDrawer(); });
    card.append(disconnectBtn);
    drawerBody.append(card);

    const reposHeading = document.createElement('p');
    reposHeading.className = 'panel-copy';
    reposHeading.textContent = 'Repositories:';
    reposHeading.style.marginTop = '10px';
    drawerBody.append(reposHeading);

    const list = document.createElement('div');
    list.className = 'drawer-list';
    list.textContent = 'Loading repos…';
    drawerBody.append(list);
    try {
      const { repos } = await apiGithubRepos();
      list.replaceChildren();
      if (repos.length === 0) {
        list.textContent = 'No repositories found.';
      }
      for (const r of repos) {
        const row = document.createElement('a');
        row.className = 'drawer-list-row';
        row.href = r.html_url; row.target = '_blank'; row.rel = 'noopener';
        const visibility = document.createElement('span'); visibility.textContent = r.private ? '🔒' : '📂';
        const name = document.createElement('span'); name.textContent = r.full_name;
        row.append(visibility, name);
        list.append(row);
      }
    } catch (err) {
      list.textContent = `⚠ ${err.message}`;
    }
  } else {
    const p2 = document.createElement('p');
    p2.className = 'panel-copy';
    p2.innerHTML = 'Paste a <a href="https://github.com/settings/tokens/new" target="_blank" rel="noopener">Personal Access Token</a> (repo scope is enough for read + issues). It\'s stored locally on this Mac only (<code>~/.qwen-harness/secrets.json</code>), never in the browser or in this repo.';
    const form = document.createElement('form');
    form.className = 'composer';
    form.innerHTML = `<input type="password" placeholder="ghp_…" autocomplete="off" /><button type="submit">Connect</button>`;
    const errorEl = document.createElement('p');
    errorEl.className = 'panel-copy';
    errorEl.style.color = 'var(--primary)';
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = form.querySelector('input');
      const token = input.value.trim();
      if (!token) return;
      form.querySelector('button').disabled = true;
      errorEl.textContent = 'Connecting…';
      try {
        await apiGithubConnect(token);
        renderGithubDrawer();
      } catch (err) {
        errorEl.textContent = `⚠ ${err.message}`;
        form.querySelector('button').disabled = false;
      }
    });
    drawerBody.append(p2, form, errorEl);
  }
}

async function renderWebsearchDrawer() {
  drawerTitle.textContent = 'Web search';
  drawerBody.replaceChildren();
  const form = document.createElement('form');
  form.className = 'composer';
  form.innerHTML = `<input type="text" placeholder="Search the web…" autocomplete="off" /><button type="submit">Search</button>`;
  const results = document.createElement('div');
  results.className = 'drawer-list';
  results.style.marginTop = '10px';
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = form.querySelector('input');
    const query = input.value.trim();
    if (!query) return;
    results.replaceChildren();
    results.textContent = 'Searching…';
    try {
      const { results: hits } = await apiWebSearch(query, 6);
      results.replaceChildren();
      if (hits.length === 0) { results.textContent = 'No results.'; return; }
      for (const hit of hits) {
        const row = document.createElement('div');
        row.style.padding = '8px 10px';
        row.style.borderBottom = '2px solid var(--border)';
        const link = document.createElement('a');
        link.href = hit.url; link.target = '_blank'; link.rel = 'noopener'; link.style.fontWeight = '700'; link.textContent = hit.title;
        const url = document.createElement('div'); url.className = 'connector-note'; url.textContent = hit.url;
        const snippet = document.createElement('div'); snippet.className = 'panel-copy'; snippet.textContent = hit.snippet;
        row.append(link, url, snippet);
        const scrapeBtn = document.createElement('button');
        scrapeBtn.type = 'button'; scrapeBtn.className = 'hotphrase-remove'; scrapeBtn.style.width = 'auto'; scrapeBtn.style.padding = '4px 8px';
        scrapeBtn.textContent = 'Insert summary →';
        scrapeBtn.addEventListener('click', async () => {
          scrapeBtn.textContent = 'Fetching…'; scrapeBtn.disabled = true;
          try {
            const page = await apiWebScrape(hit.url);
            insertIntoLastComposer(`\n\nSource: ${hit.url}\n${page.text.slice(0, 1500)}`);
          } catch (err) {
            scrapeBtn.textContent = `⚠ ${err.message}`;
          }
        });
        row.append(scrapeBtn);
        results.append(row);
      }
    } catch (err) {
      results.replaceChildren();
      results.textContent = `⚠ ${err.message}`;
    }
  });
  drawerBody.append(form, results);
}

const capBarInstances = [];
function mountCapabilityBar(container, cwdGetter) {
  function render() {
    renderCapabilityBar(container, { onOpen: (capId) => openDrawer(capId, cwdGetter?.()) });
    const gear = document.createElement('button');
    gear.type = 'button'; gear.className = 'cap-btn'; gear.title = 'Connectors & plugins';
    gear.innerHTML = `<span class="cap-icon">⚙</span><span>Connectors</span>`;
    gear.addEventListener('click', () => openDrawer('settings'));
    container.append(gear);
  }
  capBarInstances.push(render);
  render();
}
function renderAllCapabilityBars() { capBarInstances.forEach(fn => fn()); }

mountCapabilityBar($('#home-capbar'));
mountCapabilityBar($('#hub-capbar'));
mountCapabilityBar($('#project-capbar'), () => activeProject()?.path || getDirectory() || currentTreeRoot || '.');
// Code page's capability bar handles terminal/browser as inline tabs instead of the drawer.
mountCapabilityBar($('#code-capbar'), () => getDirectory() || currentTreeRoot || '.');

// ═══════════════════════════════════════════════════════════════════════
// CODE — real filesystem browsing, editable files, terminal, browser preview
// ═══════════════════════════════════════════════════════════════════════
const filetreeEl = $('#filetree');
const codeFileName = $('#code-file-name');
const codeFileStatus = $('#code-file-status');
const codeFileStatusLabel = $('#code-file-status-label');
const codeEditor = $('#code-editor');
const codeSaveBtn = $('#code-save');
let selectedFilePath = null;
let selectedNode = null;
let currentTreeRoot = '.';
let grantedRoots = [];
let showSecretFiles = false;

function pathJoin(base, name) {
  const trimmed = String(name || '').trim().replace(/^\/+/, '');
  if (!trimmed) return base;
  return `${String(base || '.').replace(/\/+$/, '')}/${trimmed}`;
}

function dirname(path) {
  const value = String(path || '');
  const idx = value.lastIndexOf('/');
  return idx > 0 ? value.slice(0, idx) : '.';
}

function basename(path) {
  return String(path || '').split('/').filter(Boolean).pop() || '';
}

function currentActionDir() {
  if (selectedNode?.type === 'dir') return selectedNode.path;
  if (selectedNode?.type === 'file') return dirname(selectedNode.path);
  return currentTreeRoot || getDirectory() || '.';
}

function setSelectedNode(node) {
  selectedNode = node;
  const hasSelection = Boolean(node);
  $('#filetree-rename').disabled = !hasSelection;
  $('#filetree-delete').disabled = !hasSelection;
}

async function refreshGrants() {
  try {
    const data = await apiFsGrants();
    grantedRoots = data.roots || [];
    const strip = $('#filetree-grants');
    strip.replaceChildren();
    const title = document.createElement('div');
    title.className = 'permission-strip-row';
    title.innerHTML = '<strong>Granted roots</strong>';
    strip.append(title);
    for (const root of grantedRoots.slice(0, 4)) {
      const row = document.createElement('div');
      row.className = 'permission-strip-row';
      const bullet = document.createElement('span'); bullet.textContent = '•';
      const path = document.createElement('span'); path.className = 'permission-strip-path'; path.title = root; path.textContent = root;
      row.append(bullet, path);
      strip.append(row);
    }
    if (grantedRoots.length > 4) {
      const row = document.createElement('div');
      row.className = 'permission-strip-row';
      row.textContent = `+ ${grantedRoots.length - 4} more`;
      strip.append(row);
    }
  } catch {
    $('#filetree-grants').textContent = 'Permission state unavailable.';
  }
}

async function grantAndOpenFolder() {
  try {
    let path;
    if (isDesktopClient()) {
      const result = await desktopChooseDirectory();
      path = result.path;
    } else {
      path = prompt('Folder path to grant:');
    }
    if (!path) return;
    await apiFsGrant(path);
    await refreshGrants();
    await loadCodeTree(path);
  } catch (err) {
    if (err.cancelled) return;
    const error = document.createElement('p'); error.className = 'transcript-empty'; error.textContent = `⚠ ${err.message}`;
    filetreeEl.replaceChildren(error);
  }
}

async function loadCodeTree(path) {
  currentTreeRoot = path;
  try {
    const { entries } = await apiListDir(path, showSecretFiles);
    const tree = entries.map(e => ({ name: e.name, path: e.path, type: e.type }));
    renderFileTree(filetreeEl, tree, {
      onSelect: selectTreeNode,
      selectedPath: selectedNode?.path || selectedFilePath,
      loadChildren: async (node) => {
        const { entries } = await apiListDir(node.path, showSecretFiles);
        return entries.map(e => ({ name: e.name, path: e.path, type: e.type }));
      },
    });
    $('#filetree-path').value = path;
    setDirectory(path);
  } catch (err) {
    const error = document.createElement('p'); error.className = 'transcript-empty'; error.textContent = `⚠ ${err.message}`;
    filetreeEl.replaceChildren(error);
    if (err.permission_required) {
      const grant = document.createElement('button'); grant.className = 'mode-button'; grant.type = 'button'; grant.textContent = 'Grant folder access';
      grant.addEventListener('click', grantAndOpenFolder); error.append(document.createElement('br'), grant);
    } else {
      error.append(document.createElement('br'), document.createTextNode('Is gui/server.py running? Use ./wake-up.sh or python3 gui/server.py.'));
    }
  }
}

async function selectTreeNode(node) {
  setSelectedNode(node);
  if (node.type === 'dir') {
    selectedFilePath = null;
    codeFileName.textContent = node.path;
    codeFileStatus.dataset.state = 'off';
    codeFileStatusLabel.textContent = 'folder selected';
    codeSaveBtn.disabled = true;
    codeEditor.disabled = true;
    codeEditor.value = `// Folder selected\n// ${node.path}`;
    return;
  }
  await openFile(node);
}

async function openFile(node) {
  setSelectedNode(node);
  selectedFilePath = node.path;
  codeFileName.textContent = node.path;
  codeFileStatus.dataset.state = 'checking';
  codeFileStatusLabel.textContent = 'loading…';
  codeSaveBtn.disabled = true;
  codeEditor.disabled = true;
  try {
    const text = await apiReadFile(node.path);
    codeEditor.value = text;
    codeEditor.disabled = false;
    codeSaveBtn.disabled = false;
    codeFileStatus.dataset.state = 'ok';
    codeFileStatusLabel.textContent = `${text.split('\n').length} lines`;
  } catch (err) {
    codeEditor.value = `// Could not load ${node.path}\n// ${err.message}`;
    codeFileStatus.dataset.state = 'off';
    codeFileStatusLabel.textContent = 'load failed';
  }
}

async function refreshCodeTree() {
  await refreshGrants();
  await loadCodeTree(currentTreeRoot || getDirectory() || '.');
}

codeSaveBtn.addEventListener('click', async () => {
  if (!selectedFilePath) return;
  codeFileStatusLabel.textContent = 'saving…';
  try {
    await apiWriteFile(selectedFilePath, codeEditor.value);
    codeFileStatus.dataset.state = 'ok';
    codeFileStatusLabel.textContent = `saved · ${codeEditor.value.split('\n').length} lines`;
    logWorkDone('code-editor', 'edit', selectedFilePath, null, 'Saved via Code page editor');
  } catch (err) {
    codeFileStatus.dataset.state = 'off';
    codeFileStatusLabel.textContent = `save failed: ${err.message}`;
  }
});

$('#filetree-open-folder').addEventListener('click', grantAndOpenFolder);
$('#filetree-refresh').addEventListener('click', refreshCodeTree);
$('#filetree-secrets').addEventListener('click', async () => {
  showSecretFiles = !showSecretFiles;
  const button = $('#filetree-secrets');
  button.textContent = showSecretFiles ? 'Hide Secrets' : 'Show Secrets';
  button.setAttribute('aria-pressed', String(showSecretFiles));
  await refreshCodeTree();
});

$('#filetree-new-file').addEventListener('click', async () => {
  const base = currentActionDir();
  const name = prompt(`New file inside:\n${base}`);
  if (!name) return;
  try {
    const result = await apiCreateFile(pathJoin(base, name));
    await refreshCodeTree();
    await openFile({ name: basename(result.path), path: result.path, type: 'file' });
  } catch (err) {
    alert(`Could not create file:\n${err.message}`);
  }
});

$('#filetree-new-folder').addEventListener('click', async () => {
  const base = currentActionDir();
  const name = prompt(`New folder inside:\n${base}`);
  if (!name) return;
  try {
    const result = await apiCreateDir(pathJoin(base, name));
    await refreshCodeTree();
    setSelectedNode({ name: basename(result.path), path: result.path, type: 'dir' });
  } catch (err) {
    alert(`Could not create folder:\n${err.message}`);
  }
});

$('#filetree-rename').addEventListener('click', async () => {
  if (!selectedNode) return;
  const nextName = prompt('Rename to:', selectedNode.name || basename(selectedNode.path));
  if (!nextName || nextName === selectedNode.name) return;
  const dest = pathJoin(dirname(selectedNode.path), nextName);
  const wasFile = selectedNode.type === 'file';
  try {
    const result = await apiMovePath(selectedNode.path, dest);
    selectedFilePath = wasFile ? result.path : null;
    await refreshCodeTree();
    if (wasFile) {
      await openFile({ name: basename(result.path), path: result.path, type: 'file' });
    } else {
      setSelectedNode({ name: basename(result.path), path: result.path, type: 'dir' });
    }
  } catch (err) {
    alert(`Could not rename:\n${err.message}`);
  }
});

$('#filetree-delete').addEventListener('click', async () => {
  if (!selectedNode) return;
  const recursive = selectedNode.type === 'dir';
  const ok = confirm(`Delete ${recursive ? 'folder and its contents' : 'file'}?\n\n${selectedNode.path}`);
  if (!ok) return;
  try {
    await apiDeletePath(selectedNode.path, recursive);
    if (selectedFilePath === selectedNode.path) {
      selectedFilePath = null;
      codeEditor.value = '';
      codeSaveBtn.disabled = true;
      codeFileName.textContent = 'Select a file';
      codeFileStatus.dataset.state = 'off';
      codeFileStatusLabel.textContent = 'deleted';
    }
    setSelectedNode(null);
    await refreshCodeTree();
  } catch (err) {
    alert(`Could not delete:\n${err.message}`);
  }
});

$('#filetree-go').addEventListener('click', () => loadCodeTree(cleanPathInput($('#filetree-path').value) || '.'));
$('#filetree-path').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadCodeTree(cleanPathInput($('#filetree-path').value) || '.'); });
$('#filetree-minimize').addEventListener('click', () => {
  document.querySelector('.workspace-code').classList.toggle('tree-collapsed');
});

// code tabs
document.querySelectorAll('.code-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.code-tab').forEach(t => t.classList.toggle('is-active', t === tab));
    document.querySelectorAll('.code-tab-panel').forEach(p => { p.hidden = p.id !== `tab-${tab.dataset.tab}`; });
  });
});

// code chat (persisted)
function loadCodeChat() {
  try { const raw = localStorage.getItem(CODE_CHAT_KEY); return raw ? JSON.parse(raw) : []; } catch { return []; }
}
function saveCodeChat() { localStorage.setItem(CODE_CHAT_KEY, JSON.stringify(codeChatHistory)); }
let codeChatHistory = loadCodeChat();
function renderCodeTranscript() {
  const el = $('#code-transcript');
  el.replaceChildren();
  if (codeChatHistory.length === 0) {
    const p = document.createElement('p'); p.className = 'transcript-empty';
    p.textContent = 'Ask about a file, or paste a snippet. This chat is saved in your browser.';
    el.append(p);
    return;
  }
  for (const entry of codeChatHistory) el.append(renderChatEntry(activeAgent(), entry, 'code-agent', 'code'));
  el.scrollTop = el.scrollHeight;
}
function appendCodeEntry(entry) {
  stampEntry(entry);
  codeChatHistory.push(entry);
  saveCodeChat();
  const el = $('#code-transcript');
  el.querySelector('.transcript-empty')?.remove();
  el.append(renderChatEntry(activeAgent(), entry, 'code-agent', 'code'));
  el.scrollTop = el.scrollHeight;
}
renderCodeTranscript();
$('#code-composer').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (await stopSurfaceRun('code')) return;
  const input = $('#code-composer-input');
  const text = input.value.trim();
  if (!text) return;
  const project = activeProject();
  const cwd = currentTreeRoot || project?.path || getDirectory() || '.';
  const codeAgent = activeAgent();
  const { payload: effortPayload } = effortForPrompt('code', text);
  const { run, controller } = await beginRun({
    surface: 'code', instruction: text, project_id: project?.id || null,
    cwd, model_key: modelsConfig.active, agent_ids: [codeAgent?.id || 'code-chat'], ...effortPayload, ...consumeRunLineage(),
  });
  markSurfaceRun('code', run);
  input.value = '';
  appendCodeEntry({ type: 'user', text });
  if (codeAgent) setAgentState(codeAgent, 'thinking');
  const persistedCodeAgent = { id: 'code-chat', name: 'code-chat', modelKey: modelsConfig.active };
  const chatId = await ensureChat(persistedCodeAgent);
  const priorMessages = await buildPriorMessages(chatId);
  try {
    const docContext = project ? await projectDocsContext(project) : '';
    const fileContext = selectedFilePath ? ` The user currently has ${selectedFilePath} open.` : '';
    const projectContext = project ? ` Active project: ${project.name} at ${project.path}.` : '';
    const content = await runAgentTurn({
      modelKey: modelsConfig.active,
      userText: text, priorMessages,
      cwd, signal: controller.signal, runId: run.id, toolProfile: 'code',
      ...effortRunOptions('code', run),
      systemPrompt: `You are a code assistant embedded in the qwen-harness GUI.${projectContext}${fileContext}\n${docContext}\nUse tools (read_file, edit_file, search_files, run_shell, github_*, etc.) when they'd genuinely help. Be concise.`,
      onToken: (content) => updateStreamingMessage($('#code-transcript'), codeAgent, run.id, content, 'code-agent'),
      onApprovalRequest: handleApprovalRequest,
      onEvent: (ev) => {
        if (ev.type === 'usage') {
          trackUsageAndMaybeCompact(chatId, modelsConfig.active, ev.totalTokens, (prompt) =>
            runAgentTurn({ modelKey: modelsConfig.active, userText: prompt, systemPrompt: 'You summarize conversations concisely.' })
          );
          return;
        }
        if (ev.type === 'tool_call') {
          if (codeAgent) setAgentState(codeAgent, 'working');
          emitRunEvent(run.id, 'agent.tool.started', { agent_id: codeAgent?.id || 'code-chat', tool: ev.name, args: ev.args });
          if (['write_file', 'edit_file', 'append_file'].includes(ev.name) && ev.args?.path) logWorkDone('code-chat', 'edit', ev.args.path, chatId);
          else if (ev.name === 'read_file' && ev.args?.path) logWorkDone('code-chat', 'read', ev.args.path, chatId);
        }
        if (ev.type === 'tool_result') emitRunEvent(run.id, 'agent.tool.completed', { agent_id: codeAgent?.id || 'code-chat' });
        appendCodeEntry(ev);
      },
    });
    persistTurn(chatId, text, content, text.slice(0, 80));
    clearStreamingMessage($('#code-transcript'), run.id);
    appendCodeEntry({ type: 'assistant', text: content || '(empty response)' });
    await finishRun(run.id, content);
  } catch (err) {
    clearStreamingMessage($('#code-transcript'), run.id);
    if (err.name !== 'AbortError') {
      appendCodeEntry({ type: 'system', text: `⚠ Live call failed: ${err.message}` });
      await failRun(run.id, err);
    }
  } finally {
    if (codeAgent) setAgentState(codeAgent, 'idle');
    clearSurfaceRun('code', run.id);
  }
});
trackComposerFocus($('#code-composer-input'));

// terminal tab (Code page)
let codeTerminalCwd = null;
$('#terminal-composer').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#terminal-input');
  const command = input.value.trim();
  if (!command) return;
  input.value = '';
  const output = $('#terminal-output');
  const cmdLine = document.createElement('div');
  cmdLine.className = 'terminal-line-cmd';
  cmdLine.textContent = `$ ${command}`;
  output.append(cmdLine);
  try {
    const result = await apiExec(command, codeTerminalCwd || currentTreeRoot);
    if (result.cwd) codeTerminalCwd = result.cwd;
    if (result.stdout) { const d = document.createElement('div'); d.textContent = result.stdout; output.append(d); }
    if (result.stderr) { const d = document.createElement('div'); d.className = 'terminal-line-err'; d.textContent = result.stderr; output.append(d); }
  } catch (err) {
    const d = document.createElement('div'); d.className = 'terminal-line-err';
    d.textContent = `⚠ ${err.message}`;
    output.append(d);
  }
  output.scrollTop = output.scrollHeight;
});
$('#terminal-copy')?.addEventListener('click', () => copyTerminalOutput($('#terminal-output'), $('#terminal-copy')));

// browser preview tab (Code page)
$('#browser-preview-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const url = $('#browser-preview-url').value.trim();
  if (url) $('#browser-preview-frame').src = url;
});

// ═══════════════════════════════════════════════════════════════════════
// PROJECTS — persisted workspaces, markdown dock, git + optional GitHub
// ═══════════════════════════════════════════════════════════════════════
let projects = [];
let activeProjectId = localStorage.getItem('qh-active-project-v1') || null;
let selectedProjectDocName = null;
let projectRepoFormOpen = false;
let projectWorkspaceOpen = false;
const PROJECT_AGENT_FEED_KEY = 'qh-project-agent-feed-v1';
const PROJECT_MAIN_FEED_KEY = 'qh-project-main-feed-v1';
let projectAgentFeed = (() => {
  try { return JSON.parse(localStorage.getItem(PROJECT_AGENT_FEED_KEY) || '{}'); } catch { return {}; }
})();
let projectMainFeed = (() => {
  try { return JSON.parse(localStorage.getItem(PROJECT_MAIN_FEED_KEY) || '{}'); } catch { return {}; }
})();

function activeProject() {
  return projects.find(p => p.id === activeProjectId) || null;
}

function persistProjectAgentFeed() {
  localStorage.setItem(PROJECT_AGENT_FEED_KEY, JSON.stringify(projectAgentFeed));
}

function persistProjectMainFeed() {
  localStorage.setItem(PROJECT_MAIN_FEED_KEY, JSON.stringify(projectMainFeed));
}

async function projectDocsContext(project) {
  const docs = (project.documents || []).slice(0, 8);
  const chunks = [];
  if (project.index) {
    chunks.push(`Repository index: ${project.index.file_count} source files and ${project.index.symbol_count} symbols. Use filesystem search/read tools for exact source context.`);
  }
  for (const doc of docs) {
    try {
      const text = await apiProjectReadDoc(project.id, doc.name);
      chunks.push(`--- Project doc: ${doc.name} ---\n${text.slice(0, 4000)}`);
    } catch {
      // Ignore a stale/missing context doc; the dock UI still shows the source of truth.
    }
  }
  return chunks.length ? `Project document context:\n${chunks.join('\n\n')}` : '';
}

function setProjectStatus(message, isError = false) {
  const el = $('#project-create-status');
  el.textContent = message || '';
  el.style.color = isError ? 'var(--primary)' : 'var(--muted-foreground)';
}

async function refreshProjects() {
  try {
    const data = await apiProjects();
    projects = data.projects || [];
    if (!projects.some(p => p.id === activeProjectId)) activeProjectId = projects[0]?.id || null;
    if (activeProjectId) localStorage.setItem('qh-active-project-v1', activeProjectId);
    renderProjects();
  } catch (err) {
    const error = document.createElement('p');
    error.className = 'transcript-empty';
    error.textContent = `⚠ ${err.message}`;
    $('#project-list').replaceChildren(error);
  }
}

function renderProjects() {
  const list = $('#project-list');
  list.replaceChildren();
  if (projects.length === 0) {
    const p = document.createElement('p');
    p.className = 'transcript-empty';
    p.textContent = 'No projects yet.';
    list.append(p);
  }
  for (const project of projects) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'project-card' + (project.id === activeProjectId ? ' is-active' : '');
    const title = document.createElement('strong');
    const path = document.createElement('span');
    const summary = document.createElement('span');
    title.textContent = project.name;
    path.textContent = project.path;
    summary.textContent = `${project.documents?.length || 0} docs · ${project.git_exists ? 'git ready' : 'git missing'}`;
    btn.append(title, path, summary);
    btn.addEventListener('click', () => {
      activeProjectId = project.id;
      selectedProjectDocName = null;
      projectRepoFormOpen = false;
      projectWorkspaceOpen = false;
      localStorage.setItem('qh-active-project-v1', activeProjectId);
      renderProjects();
    });
    list.append(btn);
  }
  renderProjectDetail();
}

function renderProjectDetail() {
  const project = activeProject();
  $('#project-open-working-dir').disabled = !project;
  $('#project-workspace-toggle').disabled = !project;
  $('#project-remove').disabled = !project;
  $('#project-delete-git').disabled = !project || !project.git_exists;
  $('#project-duplicate').disabled = !project;
  $('#project-agent-save').disabled = !project;
  $('#project-agent-send').disabled = !project;
  $('#project-send').disabled = !project;
  $('#project-actions-toggle').disabled = !project;
  $('#project-repo-create-confirm').disabled = !project;
  $('#project-repo-publish-confirm').disabled = !project;
  $('#project-doc-upload').disabled = !project;
  $('#project-index').disabled = !project;
  $('#project-doc-rename').disabled = !project || !selectedProjectDocName;
  $('#project-doc-delete').disabled = !project || !selectedProjectDocName;
  $('#project-context-query').disabled = !project;
  for (const btn of document.querySelectorAll('[data-project-git-action], #project-github-create, #project-git-commit, #project-git-checkout, #project-git-merge, #project-worktree-create, #project-worktree-remove, #project-pr-create, #project-issue-create, #project-workflows')) {
    btn.disabled = !project;
  }
  if (!project) {
    $('#project-detail-title').textContent = 'Select a project';
    $('#project-detail-path').textContent = '—';
    $('#project-doc-list').innerHTML = '<p class="transcript-empty">No project selected.</p>';
    $('#project-doc-title').textContent = 'Context';
    $('#project-context-status').textContent = 'Repository not indexed.';
    $('#project-context-results').replaceChildren();
    $('#project-doc-preview').value = '';
    $('#project-github-status').textContent = 'Select a project to publish.';
    $('#project-git-output').textContent = 'Git output will appear here.';
    $('#project-agent-list').innerHTML = '<p class="transcript-empty">No project selected.</p>';
    $('#project-agent-feed').innerHTML = '<p class="transcript-empty">Project agent messages appear here.</p>';
    $('#project-github-form').hidden = true;
    $('#project-workspace-body').hidden = true;
    $('#project-workspace-toggle').textContent = 'Workspace +';
    $('#project-workspace-toggle').setAttribute('aria-expanded', 'false');
    return;
  }

  $('#project-detail-title').textContent = project.name;
  $('#project-detail-path').textContent = project.path;
  $('#project-workspace-body').hidden = !projectWorkspaceOpen;
  $('#project-workspace-toggle').textContent = projectWorkspaceOpen ? 'Workspace -' : 'Workspace +';
  $('#project-workspace-toggle').setAttribute('aria-expanded', String(projectWorkspaceOpen));
  $('#project-github-status').textContent = project.repo?.github?.html_url
    ? `Published: ${project.repo.github.html_url}`
    : 'Local Git repo ready. Publish is optional.';
  $('#project-github-form').hidden = !projectRepoFormOpen;
  $('#project-repo-name').value = $('#project-repo-name').value || (project.slug || project.name);
  $('#project-repo-description').value = $('#project-repo-description').value || `${project.name} project`;
  $('#project-duplicate-name').value = `${project.name} copy`;
  const index = project.index;
  $('#project-context-status').textContent = index
    ? `${index.file_count} files · ${index.symbol_count} symbols · indexed ${new Date(index.created_at * 1000).toLocaleString()}`
    : 'Repository not indexed.';

  const docs = $('#project-doc-list');
  docs.replaceChildren();
  if (!project.documents || project.documents.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'transcript-empty';
    empty.textContent = 'Upload markdown files to make them project context.';
    docs.append(empty);
  } else {
    for (const doc of project.documents) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'drawer-list-row' + (doc.name === selectedProjectDocName ? ' is-selected' : '');
      const mark = document.createElement('span');
      const name = document.createElement('span');
      mark.textContent = 'M';
      name.textContent = doc.name;
      row.append(mark, name);
      row.addEventListener('click', async () => {
        selectedProjectDocName = doc.name;
        $('#project-doc-rename').disabled = false;
        $('#project-doc-delete').disabled = false;
        docs.querySelectorAll('.drawer-list-row').forEach(item => item.classList.toggle('is-selected', item === row));
        $('#project-doc-title').textContent = doc.name;
        $('#project-doc-preview').value = 'Loading…';
        try {
          $('#project-doc-preview').value = await apiProjectReadDoc(project.id, doc.name);
        } catch (err) {
          $('#project-doc-preview').value = `Could not load ${doc.name}\n${err.message}`;
        }
      });
      docs.append(row);
    }
  }
  renderProjectAgentList(project);
  renderProjectAgentFeed(project);
  renderProjectComposer(project);
  renderProjectMainFeed(project);
}

function renderProjectSearchResults(project, results) {
  const container = $('#project-context-results');
  container.replaceChildren();
  if (!results.length) {
    const empty = document.createElement('p');
    empty.className = 'transcript-empty';
    empty.textContent = 'No indexed files or symbols matched.';
    container.append(empty);
    return;
  }
  for (const result of results) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'context-result';
    button.textContent = result.symbols?.length ? `${result.path} · ${result.symbols.join(', ')}` : result.path;
    button.addEventListener('click', async () => {
      await apiFsGrant(project.path);
      await loadCodeTree(project.path);
      await openFile({ type: 'file', path: pathJoin(project.path, result.path) });
      switchRoute('code');
    });
    container.append(button);
  }
}

function renderProjectAgentList(project) {
  const list = $('#project-agent-list');
  const selected = new Set(project.settings?.agent_ids || []);
  list.replaceChildren();
  for (const agent of agents) {
    const label = document.createElement('label');
    label.className = 'project-agent-choice';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = agent.id; checkbox.checked = selected.has(agent.id);
    const name = document.createElement('span'); name.textContent = agent.name;
    const display = document.createElement('small'); display.textContent = displayName(agent);
    label.append(checkbox, name, display);
    list.append(label);
  }
}

function projectSelectedAgents(project) {
  const ids = project.settings?.agent_ids || [];
  const selected = ids.length ? agents.filter(agent => ids.includes(agent.id)) : [agents.find(agent => agent.id === activeAgentId) || agents[0]];
  return selected.filter(Boolean);
}

function renderProjectComposer(project) {
  const targetSel = $('#project-agent-target');
  const modelSel = $('#project-model-select');
  targetSel.replaceChildren();
  const selected = projectSelectedAgents(project);
  const auto = document.createElement('option');
  auto.value = '__auto__'; auto.textContent = 'Auto plan & assign';
  targetSel.append(auto);
  const all = document.createElement('option');
  all.value = '__project__';
  all.textContent = selected.length > 1 ? 'Project agents' : (selected[0]?.name || 'Active agent');
  targetSel.append(all);
  for (const agent of selected.length > 1 ? selected : []) {
    const opt = document.createElement('option');
    opt.value = agent.id;
    opt.textContent = agent.name;
    targetSel.append(opt);
  }
  modelSel.replaceChildren();
  for (const m of modelsConfig.models) {
    const opt = document.createElement('option');
    opt.value = m.key;
    opt.textContent = m.key;
    if (m.key === modelsConfig.active) opt.selected = true;
    modelSel.append(opt);
  }
}

function renderProjectMainFeed(project) {
  const feed = $('#project-main-feed');
  const rows = projectMainFeed[project.id] || [];
  feed.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement('p');
    empty.className = 'transcript-empty';
    empty.textContent = 'Project-scoped agent responses appear here.';
    feed.append(empty);
    return;
  }
  for (const row of rows.slice(-60)) {
    const agent = agents.find(a => a.name === row.agent || a.id === row.agent);
    feed.append(renderChatEntry(agent, row.entry, row.agent || 'project', 'project'));
  }
  feed.scrollTop = feed.scrollHeight;
}

function appendProjectMainFeed(project, agentName, entry) {
  stampEntry(entry);
  if (!projectMainFeed[project.id]) projectMainFeed[project.id] = [];
  projectMainFeed[project.id].push({ agent: agentName, entry });
  projectMainFeed[project.id] = projectMainFeed[project.id].slice(-120);
  persistProjectMainFeed();
  renderProjectMainFeed(project);
}

function renderProjectAgentFeed(project) {
  const feed = $('#project-agent-feed');
  const rows = projectAgentFeed[project.id] || [];
  feed.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement('p');
    empty.className = 'transcript-empty';
    empty.textContent = 'Project agent messages appear here.';
    feed.append(empty);
    return;
  }
  for (const row of rows.slice(-40)) {
    const agent = agents.find(a => a.name === row.agent || a.id === row.agent);
    feed.append(renderChatEntry(agent, row.entry, row.agent || 'project', 'project'));
  }
  feed.scrollTop = feed.scrollHeight;
}

function appendProjectAgentFeed(project, agentName, entry) {
  stampEntry(entry);
  if (!projectAgentFeed[project.id]) projectAgentFeed[project.id] = [];
  projectAgentFeed[project.id].push({ agent: agentName, entry });
  projectAgentFeed[project.id] = projectAgentFeed[project.id].slice(-80);
  persistProjectAgentFeed();
  renderProjectAgentFeed(project);
}

function setGitOutput(label, payload) {
  const output = $('#project-git-output');
  if (typeof payload === 'string') {
    output.textContent = `${label}\n${payload}`;
    return;
  }
  const git = payload?.git || payload;
  const text = [
    `${label}`,
    git?.stdout || '',
    git?.stderr || '',
    payload?.github?.html_url ? `GitHub: ${payload.github.html_url}` : '',
    payload?.github?.workflows ? payload.github.workflows.map(item => `${item.id}  ${item.state}  ${item.name} (${item.path})`).join('\n') : '',
  ].filter(Boolean).join('\n');
  output.textContent = text || `${label}\nDone.`;
}

async function runProjectGitAction(action, extra = {}, label = action) {
  const project = activeProject();
  if (!project) return;
  setGitOutput(label, 'Running…');
  try {
    const result = await apiProjectGitAction({ project_id: project.id, action, ...extra });
    if (result.project) {
      projects = projects.map(p => p.id === result.project.id ? result.project : p);
      renderProjects();
    }
    setGitOutput(label, result);
    await refreshProjects();
  } catch (err) {
    setGitOutput(label, err.message);
    setProjectStatus(err.message, true);
  }
}

async function chooseProjectParent() {
  try {
    let path;
    if (isDesktopClient()) {
      const result = await desktopChooseDirectory();
      path = result.path;
    } else {
      path = prompt('Choose parent folder path:');
    }
    if (!path) return;
    await apiFsGrant(path);
    $('#project-parent-dir').value = path;
    await refreshGrants();
  } catch (err) {
    if (!err.cancelled) setProjectStatus(err.message, true);
  }
}

async function maybePublishProject(project) {
  projectRepoFormOpen = true;
  $('#project-git-controls').hidden = true;
  $('#project-github-form').hidden = false;
  $('#project-repo-name').value = project.slug || project.name;
  $('#project-repo-description').value = `${project.name} project`;
  setProjectStatus('Fill in the GitHub repository details, then approve creation.');
}

$('#project-choose-parent').addEventListener('click', chooseProjectParent);

$('#project-create-toggle').addEventListener('click', () => {
  const body = $('#project-create-body');
  const open = body.hidden;
  body.hidden = !open;
  $('#project-create-toggle').setAttribute('aria-expanded', String(open));
  $('#project-create-toggle span').textContent = open ? 'New Project -' : 'New Project +';
});

$('#project-workspace-toggle').addEventListener('click', () => {
  projectWorkspaceOpen = !projectWorkspaceOpen;
  renderProjectDetail();
});

$('#project-actions-toggle').addEventListener('click', () => {
  const controls = $('#project-git-controls');
  controls.hidden = !controls.hidden;
});

$('#project-repo-create-cancel').addEventListener('click', () => {
  projectRepoFormOpen = false;
  $('#project-github-form').hidden = true;
});

$('#project-create-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = $('#project-name').value.trim();
  const parentDir = cleanPathInput($('#project-parent-dir').value);
  const initApproved = $('#project-init-git').checked;
  const publishAfter = $('#project-publish-github').checked;
  if (!name || !parentDir) {
    setProjectStatus('Name and parent folder are required.', true);
    return;
  }
  if (!initApproved) {
    setProjectStatus('Approve `.git` creation before creating the project.', true);
    return;
  }

  setProjectStatus('Creating project…');
  try {
    const result = await apiProjectCreate({
      name,
      parent_dir: parentDir,
      init_git_approved: true,
    });
    activeProjectId = result.project.id;
    localStorage.setItem('qh-active-project-v1', activeProjectId);
    $('#project-name').value = '';
    $('#project-init-git').checked = false;
    $('#project-publish-github').checked = false;
    await refreshProjects();
    await refreshGrants();
    setProjectStatus(`Created ${result.project.path}`);
    if (publishAfter) await maybePublishProject(result.project);
  } catch (err) {
    setProjectStatus(err.message, true);
  }
});

$('#project-doc-upload').addEventListener('change', async () => {
  const project = activeProject();
  const input = $('#project-doc-upload');
  if (!project || !input.files?.length) return;
  const files = [...input.files].filter(file => file.name.toLowerCase().endsWith('.md'));
  if (files.length !== input.files.length) {
    setProjectStatus('Only .md files are accepted in the document dock.', true);
    input.value = '';
    return;
  }
  const documents = await Promise.all(files.map(file => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, content: String(reader.result || '') });
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
    reader.readAsText(file);
  })));
  try {
    const result = await apiProjectAddDocs(project.id, documents);
    projects = projects.map(p => p.id === result.project.id ? result.project : p);
    renderProjects();
    setProjectStatus(`Added ${result.saved.length} markdown document${result.saved.length === 1 ? '' : 's'}.`);
  } catch (err) {
    setProjectStatus(err.message, true);
  } finally {
    input.value = '';
  }
});

$('#project-index').addEventListener('click', async () => {
  const project = activeProject();
  if (!project) return;
  const button = $('#project-index');
  button.disabled = true;
  button.textContent = 'Indexing...';
  $('#project-context-status').textContent = 'Reading repository structure and symbols...';
  try {
    const result = await apiProjectIndex(project.id);
    projects = projects.map(item => item.id === result.project.id ? result.project : item);
    renderProjects();
    setProjectStatus(`Indexed ${result.index.file_count} files and ${result.index.symbol_count} symbols.`);
  } catch (err) {
    setProjectStatus(err.message, true);
    $('#project-context-status').textContent = err.message;
  } finally {
    button.textContent = 'Index Repo';
    button.disabled = !activeProject();
  }
});

$('#project-doc-rename').addEventListener('click', async () => {
  const project = activeProject();
  if (!project || !selectedProjectDocName) return;
  const nextName = prompt('Rename project document:', selectedProjectDocName)?.trim();
  if (!nextName || nextName === selectedProjectDocName) return;
  try {
    const result = await apiProjectRenameDoc(project.id, selectedProjectDocName, nextName);
    selectedProjectDocName = result.name;
    projects = projects.map(item => item.id === result.project.id ? result.project : item);
    renderProjects();
    $('#project-doc-title').textContent = result.name;
    setProjectStatus(`Renamed document to ${result.name}.`);
  } catch (err) {
    setProjectStatus(err.message, true);
  }
});

$('#project-doc-delete').addEventListener('click', async () => {
  const project = activeProject();
  if (!project || !selectedProjectDocName) return;
  const name = selectedProjectDocName;
  if (!confirm(`Delete ${name} from this project's context dock?`)) return;
  try {
    const result = await apiProjectDeleteDoc(project.id, name);
    selectedProjectDocName = null;
    projects = projects.map(item => item.id === result.project.id ? result.project : item);
    $('#project-doc-title').textContent = 'Context';
    $('#project-doc-preview').value = '';
    renderProjects();
    setProjectStatus(`Deleted ${name}.`);
  } catch (err) {
    setProjectStatus(err.message, true);
  }
});

$('#project-context-search').addEventListener('submit', async (event) => {
  event.preventDefault();
  const project = activeProject();
  const query = $('#project-context-query').value.trim();
  if (!project || !query) return;
  const container = $('#project-context-results');
  container.textContent = 'Searching index...';
  try {
    const result = await apiProjectSearch(project.id, query);
    renderProjectSearchResults(project, result.results || []);
  } catch (err) {
    container.textContent = err.message;
  }
});

$('#project-open-working-dir').addEventListener('click', async () => {
  const project = activeProject();
  if (!project) return;
  await apiFsGrant(project.path);
  await refreshGrants();
  await loadCodeTree(project.path);
  switchRoute('code');
});

async function sendProjectMessage() {
  if (await stopSurfaceRun('project')) return;
  const project = activeProject();
  if (!project) return;
  const input = $('#project-composer-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  const selectedAgents = projectSelectedAgents(project);
  const targetId = $('#project-agent-target').value;
  const targets = targetId === '__project__' ? selectedAgents : selectedAgents.filter(agent => agent.id === targetId);
  const modelKey = $('#project-model-select').value;
  const context = await projectDocsContext(project);
  if (targetId === '__auto__') {
    return runAutoOrchestration({ surface: 'project', instruction: text, project, modelKey, pool: selectedAgents.length > 1 ? selectedAgents : agents });
  }
  const { payload: effortPayload } = effortForPrompt('project', text);
  const { run, controller } = await beginRun({
    surface: 'project', instruction: text, project_id: project.id, cwd: project.path,
    model_key: modelKey, agent_ids: targets.map(agent => agent.id), ...effortPayload, ...consumeRunLineage(),
  });
  markSurfaceRun('project', run);
  for (const agent of targets) {
    const entry = { type: 'user', text };
    appendProjectMainFeed(project, agent.name, entry);
    appendEntry(agent, entry);
    setAgentState(agent, 'listening');
  }
  await sleep(200);
  for (const agent of targets) {
    setAgentState(agent, 'thinking');
    emitRunEvent(run.id, 'agent.thinking', { agent_id: agent.id, agent_name: agent.name });
  }
  const outcomes = await Promise.all(targets.map(async (agent) => {
    const chatId = await ensureChat(agent);
    const priorMessages = await buildPriorMessages(chatId);
    try {
      const reply = await runAgentTurn({
        modelKey,
        userText: text, priorMessages,
        cwd: project.path, signal: controller.signal, runId: run.id, toolProfile: 'project',
        ...effortRunOptions('project', run),
        systemPrompt: `You are working inside project "${project.name}" at ${project.path}. This is the primary project control room: use terminal, browser preview, filesystem, GitHub, and other tools when they genuinely help.\n${context}`,
        onToken: (content) => updateStreamingMessage($('#project-main-feed'), agent, `${run.id}:${agent.id}`, content, agent.name),
        onApprovalRequest: handleApprovalRequest,
      onEvent: (ev) => {
          if (ev.type === 'usage') {
            trackUsageAndMaybeCompact(chatId, modelKey, ev.totalTokens, (prompt) =>
              runAgentTurn({ modelKey, userText: prompt, systemPrompt: 'You summarize conversations concisely.' })
            );
            return;
          }
          if (ev.type === 'tool_call') {
            setAgentState(agent, 'working');
            emitRunEvent(run.id, 'agent.tool.started', { agent_id: agent.id, tool: ev.name, args: ev.args });
            if (['write_file', 'edit_file', 'append_file'].includes(ev.name) && ev.args?.path) logWorkDone(agent.id, 'edit', ev.args.path, chatId, `project: ${project.name}`);
            else if (ev.name === 'read_file' && ev.args?.path) logWorkDone(agent.id, 'read', ev.args.path, chatId, `project: ${project.name}`);
          }
          if (ev.type === 'tool_result') emitRunEvent(run.id, 'agent.tool.completed', { agent_id: agent.id });
          appendProjectMainFeed(project, agent.name, ev);
          appendEntry(agent, ev);
        },
      });
      persistTurn(chatId, text, reply, `[${project.name}] ${text.slice(0, 70)}`);
      const entry = { type: 'assistant', text: reply || '(empty response)' };
      clearStreamingMessage($('#project-main-feed'), `${run.id}:${agent.id}`);
      appendProjectMainFeed(project, agent.name, entry);
      appendEntry(agent, entry);
      setAgentState(agent, 'happy');
      emitRunEvent(run.id, 'agent.completed', { agent_id: agent.id });
      return { agent: agent.name, reply };
    } catch (err) {
      clearStreamingMessage($('#project-main-feed'), `${run.id}:${agent.id}`);
      if (err.name === 'AbortError') return { agent: agent.name, cancelled: true };
      const entry = { type: 'system', text: err.message };
      appendProjectMainFeed(project, agent.name, entry);
      appendEntry(agent, entry);
      setAgentState(agent, 'confused');
      emitRunEvent(run.id, 'agent.failed', { agent_id: agent.id, error: err.message });
      return { agent: agent.name, error: err.message };
    }
  }));
  for (const agent of targets) setAgentState(agent, 'idle');
  if (controller.signal.aborted) { clearSurfaceRun('project', run.id); return; }
  const failures = outcomes.filter(result => result?.error);
  if (failures.length === outcomes.length) await failRun(run.id, failures.map(item => item.error).join('; '));
  else await finishRun(run.id, outcomes);
  clearSurfaceRun('project', run.id);
}

$('#project-send').addEventListener('click', sendProjectMessage);
$('#project-composer-input').addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') sendProjectMessage();
});
trackComposerFocus($('#project-composer-input'));

for (const [id, action, label] of [
  ['project-git-status', 'status', 'Git status'],
  ['project-git-log', 'log', 'Git log'],
  ['project-git-push', 'push', 'Git push'],
  ['project-git-pull', 'pull', 'Git pull'],
  ['project-git-fetch', 'fetch', 'Git fetch'],
  ['project-git-branches', 'branches', 'Git branches'],
]) {
  const btn = $(`#${id}`);
  btn.dataset.projectGitAction = action;
  btn.addEventListener('click', async () => {
    const approved = ['push', 'pull'].includes(action) ? confirm(`${label} for "${activeProject()?.name}"?`) : true;
    if (!approved) return;
    await runProjectGitAction(action, { approved }, label);
  });
}

for (const [action, label, needsApproval] of [
  ['diff', 'Working tree diff', false],
  ['stage', 'Stage all changes', true],
  ['unstage', 'Unstage all changes', true],
  ['worktrees', 'Git worktrees', false],
]) {
  const btn = document.querySelector(`.git-inspector [data-project-git-action="${action}"]`);
  btn.addEventListener('click', async () => {
    if (needsApproval && !confirm(`${label} for "${activeProject()?.name}"?`)) return;
    await runProjectGitAction(action, { approved: needsApproval }, label);
  });
}

$('#project-worktree-create').addEventListener('click', async () => {
  const project = activeProject();
  if (!project) return;
  const branch = prompt('New worktree branch name:');
  if (!branch) return;
  const suggested = `${dirname(project.path)}/${basename(project.path)}-${branch.replace(/[^A-Za-z0-9._-]+/g, '-')}`;
  const path = prompt('Worktree folder:', suggested);
  if (!path || !confirm(`Create branch "${branch}" in linked worktree ${path}?`)) return;
  await runProjectGitAction('worktree_create', { branch, path, create: true, approved: true }, 'Create worktree');
});

$('#project-worktree-remove').addEventListener('click', async () => {
  const project = activeProject();
  if (!project) return;
  const path = prompt('Exact linked worktree folder to remove:');
  if (!path || !confirm(`Remove linked worktree ${path}? Uncommitted changes will prevent removal.`)) return;
  await runProjectGitAction('worktree_remove', { path, approved: true }, 'Remove worktree');
});

$('#project-pr-create').addEventListener('click', async () => {
  const title = prompt('Pull request title:');
  if (!title) return;
  const head = prompt('Head branch:', 'feature');
  if (!head) return;
  const base = prompt('Base branch:', 'main') || 'main';
  const body = prompt('Pull request description:', '') || '';
  if (!confirm(`Create pull request ${head} → ${base}?`)) return;
  await runProjectGitAction('pr_create', { title, head, base, body, approved: true }, 'Create pull request');
});

$('#project-issue-create').addEventListener('click', async () => {
  const title = prompt('Issue title:');
  if (!title) return;
  const body = prompt('Issue description:', '') || '';
  if (!confirm(`Create GitHub issue "${title}"?`)) return;
  await runProjectGitAction('issue_create', { title, body, approved: true }, 'Create issue');
});

$('#project-workflows').addEventListener('click', async () => {
  await runProjectGitAction('workflows', {}, 'GitHub Actions workflows');
  const workflow = prompt('Enter a workflow id or filename to run, or leave blank to only view the list:');
  if (!workflow) return;
  const ref = prompt('Git ref:', 'main') || 'main';
  if (!confirm(`Dispatch workflow ${workflow} on ${ref}?`)) return;
  await runProjectGitAction('workflow_dispatch', { workflow, ref, approved: true }, 'Dispatch workflow');
});

$('#project-github-create').addEventListener('click', async () => {
  const project = activeProject();
  if (!project) return;
  projectRepoFormOpen = true;
  $('#project-git-controls').hidden = true;
  $('#project-github-form').hidden = false;
  $('#project-repo-name').value = project.slug || project.name;
  $('#project-repo-description').value = project.repo?.github?.description || `${project.name} project`;
  $('#project-repo-visibility').value = 'private';
  $('#project-repo-readme').checked = false;
  $('#project-repo-name').focus();
});

async function submitProjectRepoForm({ initialPush = false } = {}) {
  const project = activeProject();
  if (!project) return;
  const repoName = $('#project-repo-name').value.trim();
  const description = $('#project-repo-description').value.trim();
  const privateRepo = $('#project-repo-visibility').value !== 'public';
  const addReadme = $('#project-repo-readme').checked;
  if (!repoName) {
    setProjectStatus('Repository name is required.', true);
    return;
  }
  const actionLabel = initialPush ? 'create the repo and push an initial commit' : 'create the repo';
  const approved = confirm(`Create ${privateRepo ? 'private' : 'public'} GitHub repo "${repoName}" for "${project.name}" and ${actionLabel}?`);
  if (!approved) return;
  projectRepoFormOpen = false;
  $('#project-github-form').hidden = true;
  if (initialPush) {
    if (addReadme) setProjectStatus('Initial push creates the local README; skipping GitHub auto-README to avoid history conflicts.');
    setGitOutput('Create + initial push', 'Running...');
    try {
      const result = await apiProjectPublishGithub({
        project_id: project.id,
        repo_name: repoName,
        description,
        private: privateRepo,
        add_readme: false,
        publish_approved: true,
      });
      projects = projects.map(p => p.id === result.project.id ? result.project : p);
      activeProjectId = result.project.id;
      renderProjects();
      setGitOutput('Create + initial push', result);
      setProjectStatus(`Published: ${result.github.html_url}`);
    } catch (err) {
      setGitOutput('Create + initial push', err.message);
      setProjectStatus(err.message, true);
    }
    return;
  }
  await runProjectGitAction('create_remote', { repo_name: repoName, description, private: privateRepo, add_readme: addReadme, approved: true }, 'Create GitHub repo');
}

$('#project-repo-create-confirm').addEventListener('click', () => submitProjectRepoForm({ initialPush: false }));
$('#project-repo-publish-confirm').addEventListener('click', () => submitProjectRepoForm({ initialPush: true }));

$('#project-git-commit').addEventListener('click', async () => {
  const project = activeProject();
  if (!project) return;
  const message = prompt('Commit message:', 'Project update');
  if (!message) return;
  const approved = confirm(`Commit all current project changes?\n\n${message}`);
  if (!approved) return;
  await runProjectGitAction('commit', { message, approved: true }, 'Git commit');
});

$('#project-git-checkout').addEventListener('click', async () => {
  const branch = prompt('Branch to checkout or create:');
  if (!branch) return;
  const create = confirm(`Create/reset local branch "${branch}" from current HEAD?\n\nCancel to checkout an existing branch.`);
  await runProjectGitAction('checkout', { branch, create, approved: true }, 'Git checkout');
});

$('#project-git-merge').addEventListener('click', async () => {
  const branch = prompt('Branch to merge into the current branch:');
  if (!branch) return;
  const approved = confirm(`Merge "${branch}" into the current project branch?`);
  if (!approved) return;
  await runProjectGitAction('merge', { branch, approved: true }, 'Git merge');
});

$('#project-agent-save').addEventListener('click', async () => {
  const project = activeProject();
  if (!project) return;
  const agentIds = [...document.querySelectorAll('#project-agent-list input[type="checkbox"]:checked')].map(input => input.value);
  setProjectStatus('Saving project agents...');
  try {
    await apiProjectSettings({ project_id: project.id, action: 'update', agent_ids: agentIds });
    await refreshProjects();
    renderProjectDetail();
    setProjectStatus('Project agents saved.');
  } catch (err) {
    setProjectStatus(err.message, true);
  }
});

$('#project-agent-composer').addEventListener('submit', async (e) => {
  e.preventDefault();
  const project = activeProject();
  if (!project) return;
  const input = $('#project-agent-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  const targets = projectSelectedAgents(project);
  const context = await projectDocsContext(project);
  const projectAgentEffort = getEffort('project-agent', text);
  lockEffort('project-agent', true);
  for (const agent of targets) {
    const entry = { type: 'user', text };
    appendProjectAgentFeed(project, agent.name, entry);
    appendEntry(agent, entry);
    setAgentState(agent, 'listening');
  }
  await sleep(200);
  for (const agent of targets) setAgentState(agent, 'thinking');
  await Promise.all(targets.map(async (agent) => {
    try {
      const reply = await runAgentTurn({
        modelKey: agent.modelKey,
        userText: text,
        cwd: project.path,
        toolProfile: 'project',
        effort: projectAgentEffort.level,
        onEffortEscalation: ({ level = 4 }) => escalateEffort('project-agent', level),
        systemPrompt: `You are working inside project "${project.name}" at ${project.path}. Use the document context and tools when useful.\n${context}`,
        onApprovalRequest: handleApprovalRequest,
      onEvent: (ev) => {
          if (ev.type === 'tool_call') setAgentState(agent, 'working');
          appendProjectAgentFeed(project, agent.name, ev);
          appendEntry(agent, ev);
        },
      });
      const entry = { type: 'assistant', text: reply || '(empty response)' };
      appendProjectAgentFeed(project, agent.name, entry);
      appendEntry(agent, entry);
      setAgentState(agent, 'happy');
    } catch (err) {
      const entry = { type: 'system', text: err.message };
      appendProjectAgentFeed(project, agent.name, entry);
      appendEntry(agent, entry);
      setAgentState(agent, 'confused');
    }
    await sleep(900);
    setAgentState(agent, 'idle');
  }));
  lockEffort('project-agent', false);
});

$('#project-duplicate').addEventListener('click', async () => {
  const project = activeProject();
  if (!project) return;
  const name = $('#project-duplicate-name').value.trim() || `${project.name} copy`;
  const approved = confirm(`Duplicate "${project.name}" as "${name}"?\n\nThe new copy gets fresh .git metadata.`);
  if (!approved) return;
  setProjectStatus('Duplicating project...');
  try {
    const result = await apiProjectSettings({ project_id: project.id, action: 'duplicate', name, approved: true });
    activeProjectId = result.project.id;
    localStorage.setItem('qh-active-project-v1', activeProjectId);
    await refreshProjects();
    await refreshGrants();
    setProjectStatus(`Duplicated to ${result.project.path}`);
  } catch (err) {
    setProjectStatus(err.message, true);
  }
});

$('#project-delete-git').addEventListener('click', async () => {
  const project = activeProject();
  if (!project) return;
  const approved = confirm(`Delete only the .git directory for "${project.name}"?\n\nProject files stay on disk.`);
  if (!approved) return;
  try {
    await apiProjectSettings({ project_id: project.id, action: 'delete_git', approved: true });
    await refreshProjects();
    setProjectStatus('.git metadata deleted. Project files are still present.');
  } catch (err) {
    setProjectStatus(err.message, true);
  }
});

$('#project-remove').addEventListener('click', async () => {
  const project = activeProject();
  if (!project) return;
  const removeFiles = $('#project-remove-files').checked;
  const removeRecord = confirm(removeFiles
    ? `Remove "${project.name}" and delete its working directory?\n\n${project.path}`
    : `Remove "${project.name}" from Projects?\n\nThe working directory stays on disk.`);
  if (!removeRecord) return;
  try {
    await apiProjectDelete({
      project_id: project.id,
      remove_files: removeFiles,
      delete_approved: true,
    });
    activeProjectId = null;
    localStorage.removeItem('qh-active-project-v1');
    await refreshProjects();
    setProjectStatus(removeFiles ? 'Project and files removed.' : 'Project removed from the list.');
    setGitOutput('Remove project', 'Done.');
  } catch (err) {
    setProjectStatus(err.message, true);
    setGitOutput('Remove project', err.message);
  }
});

// ═══════════════════════════════════════════════════════════════════════
// AGENT HUB — roster grid + collapsible console
// ═══════════════════════════════════════════════════════════════════════
function inlineEditable({ value, className, ariaLabel, placeholder, onCommit }) {
  const input = document.createElement('input');
  input.type = 'text'; input.className = `editable ${className || ''}`.trim(); input.value = value || '';
  if (placeholder) input.placeholder = placeholder;
  if (ariaLabel) input.setAttribute('aria-label', ariaLabel);
  input.addEventListener('click', e => e.stopPropagation());
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') input.blur();
    if (e.key === 'Escape') { input.value = value || ''; input.blur(); }
  });
  input.addEventListener('blur', () => {
    const next = input.value.trim();
    if (next !== (value || '')) onCommit(next);
  });
  return input;
}

const hubGrid = $('#hub-grid');
async function renderHub() {
  if ($('#view-hub').hidden) return;
  hubGrid.replaceChildren();
  const mountPromises = [];
  for (const agent of agents) {
    const card = document.createElement('div');
    card.className = 'hub-card'; card.dataset.agentId = agent.id;

    const top = document.createElement('div'); top.className = 'hub-card-top';
    const avatarEl = document.createElement('div'); avatarEl.className = 'hub-avatar';
    const idBox = document.createElement('div'); idBox.className = 'hub-card-id';

    const nameLabel = document.createElement('div'); nameLabel.className = 'hub-field-label'; nameLabel.textContent = 'Agent name';
    const nameInput = inlineEditable({
      value: agent.name, className: 'hub-name-input', ariaLabel: 'Agent name',
      onCommit: (next) => { agent.name = next || agent.name; persistAgents(); if (agent.id === activeAgentId) $('#stage-name').textContent = agent.name; renderHubFocusSelect(); populateHomeSelectors(); renderHomeGreetingAvatars(); },
    });

    const nickLabel = document.createElement('div'); nickLabel.className = 'hub-field-label'; nickLabel.textContent = 'Avatar nickname';
    const nickInput = inlineEditable({
      value: agent.nickname || '', className: 'hub-nick-input', ariaLabel: 'Avatar nickname',
      placeholder: avatarBySlug.get(agent.slug)?.name || '',
      onCommit: (next) => {
        agent.nickname = next || null; persistAgents();
        if (agent.id === activeAgentId) $('#stage-model').textContent = `${displayName(agent)} · ${agent.modelKey}`;
        renderHub();
      },
    });

    idBox.append(nameLabel, nameInput, nickLabel, nickInput);
    top.append(avatarEl, idBox);

    const row = document.createElement('div'); row.className = 'hub-row';
    const charField = document.createElement('label'); charField.className = 'field'; charField.innerHTML = '<span>Character</span>';
    const charSelect = document.createElement('select');
    for (const a of manifest) {
      const opt = document.createElement('option'); opt.value = a.slug; opt.textContent = a.name;
      if (a.slug === agent.slug) opt.selected = true;
      charSelect.append(opt);
    }
    charSelect.addEventListener('change', async () => {
      agent.slug = charSelect.value; persistAgents();
      if (agent.id === activeAgentId) await renderStage();
      renderHub();
      renderHomeGreetingAvatars();
    });
    charField.append(charSelect);

    const modelField = document.createElement('label'); modelField.className = 'field'; modelField.innerHTML = '<span>Model</span>';
    const modelSelect = document.createElement('select');
    for (const m of modelsConfig.models) {
      const opt = document.createElement('option'); opt.value = m.key; opt.textContent = m.key;
      if (m.key === agent.modelKey) opt.selected = true;
      modelSelect.append(opt);
    }
    modelSelect.addEventListener('change', () => {
      agent.modelKey = modelSelect.value; persistAgents();
      if (agent.id === activeAgentId) $('#stage-model').textContent = `${displayName(agent)} · ${agent.modelKey}`;
    });
    modelField.append(modelSelect);
    row.append(charField, modelField);

    const actions = document.createElement('div'); actions.className = 'hub-card-actions';
    const stateBadge = document.createElement('span'); stateBadge.className = 'roster-state'; stateBadge.append(createActivityIndicator(agent.state, { size: 15, label: agent.state }));
    const btnGroup = document.createElement('div'); btnGroup.style.display = 'flex'; btnGroup.style.gap = '8px';
    const openBtn = document.createElement('button'); openBtn.type = 'button'; openBtn.className = 'hub-open'; openBtn.textContent = 'Open in console →';
    openBtn.addEventListener('click', () => { $('#hub-console-body').hidden = false; $('#hub-console-caret').textContent = '▾'; selectAgent(agent.id); });
    const removeBtn = document.createElement('button'); removeBtn.type = 'button'; removeBtn.className = 'hub-remove'; removeBtn.textContent = 'Remove';
    removeBtn.addEventListener('click', () => removeAgent(agent.id));
    btnGroup.append(openBtn, removeBtn);
    actions.append(stateBadge, btnGroup);

    card.append(top, row, actions);
    hubGrid.append(card);
    mountPromises.push(
      mountInto(avatarEl, agent.slug, STATE_SEQUENCE[agent.state]).then(handle => { agent.hubHandle = handle; })
    );
  }
  await Promise.all(mountPromises);
  renderHubFocusSelect();
}

function renderHubFocusSelect() {
  const sel = $('#hub-focus-agent');
  const prev = sel.value;
  sel.replaceChildren();
  for (const a of agents) {
    const opt = document.createElement('option'); opt.value = a.id; opt.textContent = a.name;
    if (a.id === activeAgentId) opt.selected = true;
    sel.append(opt);
  }
  if (prev && agents.some(a => a.id === prev)) sel.value = prev;
}
$('#hub-focus-agent').addEventListener('change', () => selectAgent($('#hub-focus-agent').value));

$('#hub-console-toggle').addEventListener('click', () => {
  const body = $('#hub-console-body');
  body.hidden = !body.hidden;
  $('#hub-console-caret').textContent = body.hidden ? '▸' : '▾';
});

async function selectAgent(id) {
  activeAgentId = id;
  persistAgents();
  await renderStage();
  renderTranscript();
  renderHubFocusSelect();
  populateHomeSelectors();
}

function removeAgent(id) {
  const idx = agents.findIndex(a => a.id === id);
  if (idx === -1 || agents.length <= 1) return;
  agents.splice(idx, 1);
  if (activeAgentId === id) activeAgentId = agents[0].id;
  persistAgents();
  renderStage(); renderTranscript(); renderHub(); populateHomeSelectors(); renderHomeGreetingAvatars();
}
function addAgent() {
  const pool = manifest.map(a => a.slug);
  const agent = makeAgent(pool[agents.length % pool.length], `agent-${agents.length + 1}`, modelsConfig.active);
  agents.push(agent);
  activeAgentId = agent.id;
  persistAgents();
  renderStage(); renderTranscript(); renderHub(); populateHomeSelectors(); renderHomeGreetingAvatars();
}
$('#hub-add-agent').addEventListener('click', addAgent);

async function renderStage() {
  const agent = agents.find(a => a.id === activeAgentId);
  if (!agent) return;
  $('#stage-name').textContent = agent.name;
  $('#stage-model').textContent = `${displayName(agent)} · ${agent.modelKey}`;
  $('#avatar-picker').value = agent.slug;
  $('#model-picker').value = agent.modelKey;
  $('#state-picker').value = agent.state;
  agent.stageHandle = await mountInto($('#stage-avatar'), agent.slug, STATE_SEQUENCE[agent.state]);
  setAgentState(agent, agent.state);
}
function renderTranscript() {
  const agent = agents.find(a => a.id === activeAgentId);
  const el = $('#transcript');
  el.replaceChildren();
  if (!agent || agent.transcript.length === 0) {
    const p = document.createElement('p'); p.className = 'transcript-empty';
    p.textContent = 'Pick a script above to preview a run, or switch to Live mode.';
    el.append(p);
    return;
  }
  for (const entry of agent.transcript) el.append(renderChatEntry(agent, entry, agent.name, 'hub'));
  el.scrollTop = el.scrollHeight;
}

for (const a of manifest) { const opt = document.createElement('option'); opt.value = a.slug; opt.textContent = a.name; $('#avatar-picker').append(opt); }
for (const s of ALL_STATES) { const opt = document.createElement('option'); opt.value = s; opt.textContent = s; $('#state-picker').append(opt); }
for (const m of modelsConfig.models) { const opt = document.createElement('option'); opt.value = m.key; opt.textContent = m.key + (m.local ? ' (local)' : ' (cloud)'); $('#model-picker').append(opt); }
for (const script of demoScripts) {
  const btn = document.createElement('button'); btn.className = 'run-button'; btn.type = 'button';
  btn.innerHTML = `${script.label}<small>${script.description}</small>`;
  btn.addEventListener('click', () => runDemoScript(script));
  $('#run-buttons').append(btn);
}

$('#avatar-picker').addEventListener('change', async () => {
  const agent = agents.find(a => a.id === activeAgentId);
  agent.slug = $('#avatar-picker').value; persistAgents();
  await renderStage(); renderHub(); renderHomeGreetingAvatars();
});
$('#state-picker').addEventListener('change', () => setAgentState(agents.find(a => a.id === activeAgentId), $('#state-picker').value));
$('#model-picker').addEventListener('change', () => {
  const agent = agents.find(a => a.id === activeAgentId);
  agent.modelKey = $('#model-picker').value;
  $('#stage-model').textContent = `${displayName(agent)} · ${agent.modelKey}`;
  persistAgents(); renderHub();
});

async function runDemoScript(script) {
  const agent = agents.find(a => a.id === activeAgentId);
  if (!agent) return;
  agent.transcript = []; agent.name = script.agentName; agent.modelKey = script.model;
  renderTranscript(); persistAgents();
  for (const ev of script.events) {
    await sleep(ev.holdMs ?? 350);
    if (ev.state) setAgentState(agent, ev.state);
    if (ev.type === 'user') appendEntry(agent, { type: 'user', text: ev.text });
    else if (ev.type === 'assistant') appendEntry(agent, { type: 'assistant', text: ev.text, role: ev.role });
    else if (ev.type === 'system') {
      appendEntry(agent, { type: 'system', text: ev.text });
      if (ev.model) { agent.modelKey = ev.model; $('#stage-model').textContent = `${displayName(agent)} · ${agent.modelKey}`; }
    } else if (ev.type === 'tool_call') appendEntry(agent, { type: 'tool_call', name: ev.name, args: ev.args });
    else if (ev.type === 'tool_result') appendEntry(agent, { type: 'tool_result', text: ev.text });
    else if (ev.type === 'handoff') appendEntry(agent, { type: 'system', text: `→ handing off to "${ev.role}"` });
  }
  setAgentState(agent, 'idle');
}

let hubMode = 'demo';
$('#mode-demo').addEventListener('click', () => setHubMode('demo'));
$('#mode-live').addEventListener('click', () => setHubMode('live'));
function setHubMode(next) {
  hubMode = next;
  $('#mode-demo').classList.toggle('is-active', hubMode === 'demo');
  $('#mode-live').classList.toggle('is-active', hubMode === 'live');
  $('#mode-copy').textContent = hubMode === 'demo'
    ? 'Demo mode — scripted, no model calls.'
    : 'Live mode — calls your local Ollama server directly.';
}
setHubMode('demo');

$('#composer').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (await stopSurfaceRun('hub')) return;
  const input = $('#composer-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  const agent = agents.find(a => a.id === activeAgentId);
  if (hubMode === 'live') {
    const cwd = getDirectory() || '.';
    const { payload: effortPayload } = effortForPrompt('hub', text);
    const { run, controller } = await beginRun({
      surface: 'hub', instruction: text, cwd, model_key: agent.modelKey, agent_ids: [agent.id], ...effortPayload, ...consumeRunLineage(),
    });
    markSurfaceRun('hub', run);
    appendEntry(agent, { type: 'user', text });
    setAgentState(agent, 'listening'); await sleep(250); setAgentState(agent, 'thinking');
    emitRunEvent(run.id, 'agent.thinking', { agent_id: agent.id, agent_name: agent.name });
    try {
      const content = await runAgentTurn({
        modelKey: agent.modelKey, userText: text,
        cwd, signal: controller.signal, runId: run.id, toolProfile: 'hub',
        ...effortRunOptions('hub', run),
        systemPrompt: 'You are a concise, helpful assistant. Use tools when they would genuinely help.',
        onToken: (content) => updateStreamingMessage($('#transcript'), agent, run.id, content, agent.name),
        onApprovalRequest: handleApprovalRequest,
      onEvent: (ev) => {
          if (ev.type === 'tool_call') {
            setAgentState(agent, 'working');
            emitRunEvent(run.id, 'agent.tool.started', { agent_id: agent.id, tool: ev.name, args: ev.args });
          }
          if (ev.type === 'tool_result') emitRunEvent(run.id, 'agent.tool.completed', { agent_id: agent.id });
          appendEntry(agent, ev);
        },
      });
      appendEntry(agent, { type: 'assistant', text: content || '(empty response)' });
      clearStreamingMessage($('#transcript'), run.id);
      setAgentState(agent, 'happy');
      await finishRun(run.id, content);
    } catch (err) {
      clearStreamingMessage($('#transcript'), run.id);
      if (err.name !== 'AbortError') {
        appendEntry(agent, { type: 'system', text: `⚠ Live call failed: ${err.message}.` });
        setAgentState(agent, 'confused');
        await failRun(run.id, err);
      }
    }
    await sleep(350); setAgentState(agent, 'idle'); clearSurfaceRun('hub', run.id);
  } else {
    appendEntry(agent, { type: 'user', text });
    setAgentState(agent, 'listening'); await sleep(400); setAgentState(agent, 'thinking'); await sleep(900);
    appendEntry(agent, { type: 'system', text: 'Demo mode has no model behind it — switch to Live mode, or pick a script above.' });
    setAgentState(agent, 'idle');
  }
});
trackComposerFocus($('#composer-input'));

window.addEventListener('qh-retry-run', async ({ detail: run }) => {
  retryParentRun = run;
  setActivityDrawer(false);
  if (run.surface === 'project' && run.project_id) {
    activeProjectId = run.project_id;
    projectWorkspaceOpen = true;
    switchRoute('projects');
    await refreshProjects();
    $('#project-composer-input').value = run.instruction;
    return sendProjectMessage();
  }
  if (run.surface === 'code') {
    switchRoute('code');
    $('#code-composer-input').value = run.instruction;
    return $('#code-composer').requestSubmit();
  }
  if (run.surface === 'hub') {
    switchRoute('hub'); setHubMode('live');
    $('#composer-input').value = run.instruction;
    return $('#composer').requestSubmit();
  }
  switchRoute('home');
  $('#home-composer-input').value = run.instruction;
  return sendHomeMessage();
});

// ═══════════════════════════════════════════════════════════════════════
// theme, router, status polling, boot
// ═══════════════════════════════════════════════════════════════════════
function applyTheme(theme) {
  document.documentElement.classList.toggle('dark', theme === 'dark');
  $('#theme-icon').textContent = theme === 'dark' ? '●' : '◐';
  localStorage.setItem(THEME_KEY, theme);
}
const savedTheme = localStorage.getItem(THEME_KEY) || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
applyTheme(savedTheme);
$('#theme-toggle').addEventListener('click', () => applyTheme(document.documentElement.classList.contains('dark') ? 'light' : 'dark'));

const ROUTE_TITLES = { home: 'Home', code: 'Code', projects: 'Projects', hub: 'Agent hub' };
function switchRoute(route) {
  document.querySelectorAll('.rail-link').forEach(link => link.classList.toggle('is-active', link.dataset.route === route));
  $('#view-home').hidden = route !== 'home';
  $('#view-code').hidden = route !== 'code';
  $('#view-projects').hidden = route !== 'projects';
  $('#view-hub').hidden = route !== 'hub';
  $('#route-title').textContent = ROUTE_TITLES[route];
  localStorage.setItem(ROUTE_KEY, route);
  if (route === 'projects') refreshProjects();
  if (route === 'hub') renderHub();
}
document.querySelectorAll('.rail-link').forEach(link => link.addEventListener('click', () => switchRoute(link.dataset.route)));

async function pollBackend() {
  const pill = $('#backend-status'); const label = $('#backend-status-label');
  const ok = await apiHealth();
  pill.dataset.state = ok ? 'ok' : 'off';
  label.textContent = ok ? 'Server online' : 'Server offline — run wake-up.sh';
}
async function pollOllama() {
  const pill = $('#ollama-status'); const label = $('#ollama-status-label');
  const modelPill = $('#model-status'); const modelLabel = $('#model-status-label');
  try {
    const res = await fetch('http://localhost:11434/api/tags', { cache: 'no-store' });
    if (!res.ok) throw new Error();
    const data = await res.json();
    const names = new Set((data.models || []).map(m => m.name));
    pill.dataset.state = 'ok';
    label.textContent = `Ollama online · ${names.size} model${names.size === 1 ? '' : 's'} pulled`;
    const active = modelsConfig.active;
    if (names.has(active)) { modelPill.dataset.state = 'ok'; modelLabel.textContent = `${active} ready`; }
    else { modelPill.dataset.state = 'checking'; modelLabel.textContent = `${active} still downloading…`; }
  } catch {
    pill.dataset.state = 'off'; label.textContent = 'Ollama offline — run setup.sh';
    modelPill.dataset.state = 'off'; modelLabel.textContent = `config.py: ${modelsConfig.active} (not started)`;
  }
}

// ── resource monitor (Home) ─────────────────────────────────────────────
function pctFillClass(pct) {
  return pct >= 80 ? 'is-hot' : '';
}
async function pollResourceMonitor() {
  try {
    const { system, tasks } = await apiSystemStats();
    for (const prefix of ['', 'project-']) {
      const cpuFill = $(`#${prefix}rm-cpu-fill`), cpuValue = $(`#${prefix}rm-cpu-value`);
      const memFill = $(`#${prefix}rm-mem-fill`), memValue = $(`#${prefix}rm-mem-value`);
      const tasksEl = $(`#${prefix}resource-tasks`);
      if (!cpuFill || !memFill || !tasksEl) continue;
      // Both the Home and Project monitors exist in the DOM at all times;
      // only paint the one actually on screen.
      if (cpuFill.closest('.view')?.hidden) continue;
      const cpuPct = system.cpu_pct ?? 0;
      cpuFill.style.width = `${Math.min(100, cpuPct)}%`;
      cpuFill.className = `resource-bar-fill ${pctFillClass(cpuPct)}`;
      cpuValue.textContent = system.cpu_pct != null ? `${system.cpu_pct}%` : '—';

      const memPct = system.mem_total_mb ? (system.mem_used_mb / system.mem_total_mb) * 100 : 0;
      memFill.style.width = `${Math.min(100, memPct)}%`;
      memFill.className = `resource-bar-fill ${pctFillClass(memPct)}`;
      memValue.textContent = system.mem_used_mb != null
        ? `${(system.mem_used_mb / 1024).toFixed(1)}/${(system.mem_total_mb / 1024).toFixed(1)}GB`
        : '—';

      tasksEl.replaceChildren();
      if (!tasks || tasks.length === 0) {
        const span = document.createElement('span');
        span.className = 'resource-idle';
        span.textContent = 'No active inference';
        tasksEl.append(span);
      } else {
        for (const t of tasks) {
          const chip = document.createElement('span');
          chip.className = 'resource-task-chip';
          const cpu = t.cpu_pct != null ? `${t.cpu_pct.toFixed(0)}%` : '?';
          const mem = t.mem_mb != null ? `${(t.mem_mb / 1024).toFixed(1)}GB` : '?';
          const dot = document.createElement('span'); dot.className = 'rt-dot';
          chip.append(dot, document.createTextNode(`${t.model} · ${cpu} CPU · ${mem}`));
          tasksEl.append(chip);
        }
      }
    }
  } catch { /* backend offline — leave last-known values showing */ }
}

// ── boot ──────────────────────────────────────────────────────────────────
renderTOD();
populateHomeSelectors();
renderHomeGreetingAvatars();
renderAttachmentChips();
await renderStage();
renderTranscript();
renderHubFocusSelect();
await refreshGrants();
await refreshProjects();
await loadCodeTree(getDirectory() || '.');
pollBackend(); pollOllama(); pollResourceMonitor();
refreshActivity();

// Polling discipline. Previously five timers fired unconditionally, with no
// in-flight guard — a slow endpoint stacked duplicate requests until the
// backend was saturated. Each poller now (a) skips while the tab is hidden,
// (b) skips while its own previous call is still running, and (c) skips when
// the DOM it updates isn't visible.
const inFlightPolls = new Set();

// Fail-safe visibility gate. Some embedded hosts (WKWebView in the Mac app,
// automation browsers) can report `hidden` permanently — suppressing polls
// there would freeze the whole UI. So only trust `hidden` once we've actually
// observed the page report `visible` at least once; until then, always poll.
let hasEverBeenVisible = document.visibilityState === 'visible';
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') hasEverBeenVisible = true;
});
function pollingSuppressed() {
  return hasEverBeenVisible && document.visibilityState === 'hidden';
}

function schedulePoll(key, fn, intervalMs, shouldRun = () => true) {
  setInterval(async () => {
    if (pollingSuppressed()) return;
    if (inFlightPolls.has(key)) return;
    if (!shouldRun()) return;
    inFlightPolls.add(key);
    try { await fn(); } catch { /* pollers report their own errors */ }
    finally { inFlightPolls.delete(key); }
  }, intervalMs);
}

schedulePoll('backend', pollBackend, 8000);
schedulePoll('ollama', pollOllama, 5000);
// 3s was chosen to look live during inference; when nothing is running the
// numbers barely move, so back off and leave the CPU to the models.
schedulePoll('resources', pollResourceMonitor, 5000);
// The activity drawer rebuilds up to 40 cards from a ~22KB payload — pointless
// while the drawer is closed.
schedulePoll('activity', refreshActivity, 5000);
// Projects are shared, on-disk state (~/.qwen-harness/projects.json) that
// any client (this browser tab, another tab, the Mac app) can mutate — so
// while the Projects view is open, poll for changes made elsewhere instead
// of only refreshing on this client's own actions.
schedulePoll('projects', refreshProjects, 4000, () => !$('#view-projects').hidden);

// Catch up immediately when the tab comes back, instead of waiting out a
// full interval on stale data.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  pollBackend(); pollOllama(); pollResourceMonitor();
  refreshActivity();
  if (!$('#view-projects').hidden) refreshProjects();
});
switchRoute(localStorage.getItem(ROUTE_KEY) || 'home');
