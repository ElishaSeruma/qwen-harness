// gui/capabilities.js — shared, page-agnostic capability system.
// Backs the toggleable capability bar that sits above every composer
// (Home chat, Code chat, Agent Hub console): directory browsing, file
// upload attachments, hotphrases, terminal exec, and connector toggles.
// All state persists to localStorage so it survives reload.

const API = ''; // same-origin, server.py serves both the API and the static files

export async function apiListDir(path) {
  const res = await fetch(`${API}/api/fs/list?path=${encodeURIComponent(path || '')}`);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `list failed (${res.status})`);
  return data;
}
export async function apiReadFile(path) {
  const res = await fetch(`${API}/api/fs/read?path=${encodeURIComponent(path)}`);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `read failed (${res.status})`);
  }
  return res.text();
}
export async function apiWriteFile(path, content) {
  const res = await fetch(`${API}/api/fs/write`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `write failed (${res.status})`);
  return data;
}
export async function apiExec(command, cwd) {
  const res = await fetch(`${API}/api/exec`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command, cwd }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `exec failed (${res.status})`);
  return data;
}
export async function apiHealth() {
  try {
    const res = await fetch(`${API}/api/health`, { cache: 'no-store' });
    return res.ok;
  } catch { return false; }
}
export async function apiWebSearch(query, maxResults = 5) {
  const res = await fetch(`${API}/api/web/search?q=${encodeURIComponent(query)}&max_results=${maxResults}`);
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || `search failed (${res.status})`);
  return data;
}
export async function apiWebScrape(url) {
  const res = await fetch(`${API}/api/web/scrape?url=${encodeURIComponent(url)}`);
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || `scrape failed (${res.status})`);
  return data;
}
export async function apiGithubStatus() {
  const res = await fetch(`${API}/api/github/status`);
  return res.json();
}
export async function apiGithubConnect(token) {
  const res = await fetch(`${API}/api/github/connect`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || `connect failed (${res.status})`);
  return data;
}
export async function apiGithubDisconnect() {
  const res = await fetch(`${API}/api/github/disconnect`, { method: 'POST' });
  return res.json();
}
export async function apiGithubRepos() {
  const res = await fetch(`${API}/api/github/repos`);
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || `list repos failed (${res.status})`);
  return data;
}

// ── persisted state ─────────────────────────────────────────────────────
const DIR_KEY = 'qh-directory-v1';
const HOTPHRASES_KEY = 'qh-hotphrases-v1';
const CONNECTORS_KEY = 'qh-connectors-v2'; // v2: GitHub + web search are no longer "coming soon"

export const CONNECTOR_DEFS = [
  { id: 'filesystem', label: 'Filesystem', icon: '📁', always: true },
  { id: 'terminal', label: 'Terminal', icon: '⌨' },
  { id: 'browser', label: 'Browser preview', icon: '◫' },
  { id: 'hotphrases', label: 'Hotphrases', icon: '⚡' },
  { id: 'upload', label: 'File upload', icon: '📎' },
  { id: 'websearch', label: 'Web search', icon: '🔎' },
  { id: 'github', label: 'GitHub', icon: '⎇' },
];

export function getDirectory() {
  return localStorage.getItem(DIR_KEY) || '';
}
export function setDirectory(path) {
  localStorage.setItem(DIR_KEY, path);
}

export function getHotphrases() {
  try {
    const raw = localStorage.getItem(HOTPHRASES_KEY);
    if (raw) return JSON.parse(raw);
  } catch { /* fall through to defaults */ }
  return [
    { label: 'Explain', phrase: 'Explain what this code does, step by step.' },
    { label: 'Fix bug', phrase: 'There is a bug here. Find the root cause and fix it.' },
    { label: 'Add tests', phrase: 'Add tests covering the edge cases for this.' },
    { label: 'Summarize', phrase: 'Summarize this in three bullet points.' },
  ];
}
export function setHotphrases(list) {
  localStorage.setItem(HOTPHRASES_KEY, JSON.stringify(list));
}

export function getConnectors() {
  try {
    const raw = localStorage.getItem(CONNECTORS_KEY);
    if (raw) return { ...defaultConnectors(), ...JSON.parse(raw) };
  } catch { /* fall through */ }
  return defaultConnectors();
}
function defaultConnectors() {
  const state = {};
  for (const c of CONNECTOR_DEFS) state[c.id] = !c.comingSoon;
  return state;
}
export function setConnectorEnabled(id, enabled) {
  const state = getConnectors();
  state[id] = enabled;
  localStorage.setItem(CONNECTORS_KEY, JSON.stringify(state));
}

// ── focus tracking (so hotphrases know where to insert) ──────────────────
let lastFocusedComposer = null;
export function trackComposerFocus(el) {
  el.addEventListener('focus', () => { lastFocusedComposer = el; });
}
export function insertIntoLastComposer(text) {
  const el = lastFocusedComposer;
  if (!el) return false;
  const start = el.selectionStart ?? el.value.length;
  const end = el.selectionEnd ?? el.value.length;
  el.value = el.value.slice(0, start) + text + el.value.slice(end);
  el.focus();
  el.selectionStart = el.selectionEnd = start + text.length;
  return true;
}

// ── attachments (client-side only; text content inlined into prompts) ────
let attachments = [];
export function getAttachments() { return attachments; }
export function clearAttachments() { attachments = []; }
export function addAttachmentFiles(fileList, onChange) {
  const files = Array.from(fileList);
  let remaining = files.length;
  if (remaining === 0) return;
  files.forEach(file => {
    const reader = new FileReader();
    reader.onload = () => {
      attachments.push({ name: file.name, size: file.size, content: String(reader.result) });
      remaining -= 1;
      if (remaining === 0) onChange?.(attachments);
    };
    reader.onerror = () => {
      remaining -= 1;
      if (remaining === 0) onChange?.(attachments);
    };
    reader.readAsText(file);
  });
}
export function attachmentsAsPromptSuffix() {
  if (attachments.length === 0) return '';
  return '\n\n' + attachments.map(a => `--- ${a.name} ---\n${a.content}`).join('\n\n');
}

// ── capability bar ────────────────────────────────────────────────────────
/**
 * Render a row of capability toggle buttons into `container`.
 * @param {HTMLElement} container
 * @param {{scope:string, onOpen:(capabilityId:string)=>void}} opts
 */
export function renderCapabilityBar(container, opts) {
  container.replaceChildren();
  const connectors = getConnectors();
  for (const def of CONNECTOR_DEFS) {
    if (def.id === 'filesystem') continue; // implicit, no button needed
    const enabled = connectors[def.id];
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cap-btn';
    btn.disabled = !enabled;
    btn.title = def.comingSoon ? `${def.label} (coming soon)` : def.label;
    btn.innerHTML = `<span class="cap-icon">${def.icon}</span><span>${def.label}</span>`;
    if (enabled) btn.addEventListener('click', () => opts.onOpen(def.id));
    container.append(btn);
  }
}
