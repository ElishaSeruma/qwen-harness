// gui/app.js — Control Room for qwen-harness.
// Home: chat-first instruction dispatch to one agent or all of them.
// Code: real filesystem browsing/editing + terminal + browser preview.
// Agent hub: rename agents/avatars, manage the roster, and a tucked-away
// console for scripted demo runs / direct live chat with one agent.

import { renderFileTree, withPaths } from './filetree.js';
import {
  apiListDir, apiReadFile, apiWriteFile, apiExec, apiHealth,
  apiFsGrants, apiFsGrant, apiCreateFile, apiCreateDir, apiMovePath, apiDeletePath,
  apiWebSearch, apiWebScrape, apiGithubStatus, apiGithubConnect, apiGithubDisconnect, apiGithubRepos,
  isDesktopClient, desktopChooseDirectory, desktopChooseFile,
  getDirectory, setDirectory, getHotphrases, setHotphrases,
  getConnectors, setConnectorEnabled, CONNECTOR_DEFS,
  trackComposerFocus, insertIntoLastComposer,
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
    handle: null, stageHandle: null, hubHandle: null, transcript: [],
  };
}
function persistAgents() {
  localStorage.setItem(AGENTS_KEY, JSON.stringify({
    agentSeq, activeAgentId,
    agents: agents.map(a => ({ id: a.id, name: a.name, slug: a.slug, nickname: a.nickname, modelKey: a.modelKey, state: a.state })),
  }));
}
function loadAgents() {
  try {
    const raw = localStorage.getItem(AGENTS_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!Array.isArray(data.agents) || data.agents.length === 0) return null;
    agentSeq = data.agentSeq || data.agents.length;
    const restored = data.agents.map(a => ({ ...makeAgent(a.slug, a.name, a.modelKey, a.nickname ?? null), id: a.id, state: a.state || 'sleeping' }));
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

function setAgentState(agent, state) {
  agent.state = state;
  const seq = STATE_SEQUENCE[state] || 'idle';
  if (agent.handle) agent.handle.play(seq);
  if (agent.hubHandle) agent.hubHandle.play(seq);
  if (agent.id === activeAgentId && agent.stageHandle) agent.stageHandle.play(seq);
  if (agent.id === activeAgentId) {
    $('#stage-state').dataset.state = ['idle', 'sleeping', 'waking'].includes(state) ? 'off' : 'ok';
    $('#stage-state-label').textContent = state;
    $('#state-picker').value = state;
  }
  const hubBadge = document.querySelector(`.hub-card[data-agent-id="${agent.id}"] .roster-state`);
  if (hubBadge) hubBadge.textContent = agent.state;
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
    div.innerHTML = `<span class="tool-head">🔧 ${entry.name}</span>(${JSON.stringify(entry.args)})`;
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
  if (entry.role) div.innerHTML = `<span class="msg-role">${entry.role}</span>${entry.text}`;
  else div.textContent = entry.text;
  return div;
}
function appendEntry(agent, entry) {
  agent.transcript.push(entry);
  if (agent.id === activeAgentId) {
    $('#transcript').querySelector('.transcript-empty')?.remove();
    $('#transcript').append(renderEntry(entry));
    $('#transcript').scrollTop = $('#transcript').scrollHeight;
  }
}

async function callOllama(modelKey, userText, systemPrompt) {
  const res = await fetch('http://localhost:11434/v1/chat/completions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: modelKey,
      messages: [{ role: 'system', content: `/no_think\n\n${systemPrompt}` }, { role: 'user', content: userText }],
      max_tokens: 512, temperature: 0.6,
    }),
  });
  if (!res.ok) throw new Error(`Ollama responded ${res.status}`);
  const data = await res.json();
  return (data.choices?.[0]?.message?.content || '').replace(/<think>[\s\S]*?<\/think>/g, '').trim();
}

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
    chip.innerHTML = `📎 ${att.name}`;
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

function homeFeedAppend(agentLabel, entry) {
  const feed = $('#home-feed');
  feed.querySelector('.transcript-empty')?.remove();
  const wrap = document.createElement('div');
  const el = renderEntry(entry);
  if (entry.type === 'user' || entry.type === 'assistant') {
    const tag = document.createElement('div');
    tag.className = 'msg-role';
    tag.style.textAlign = entry.type === 'user' ? 'right' : 'left';
    tag.style.margin = '0 2px';
    tag.textContent = agentLabel;
    wrap.append(tag);
  }
  wrap.append(el);
  feed.append(wrap);
  feed.scrollTop = feed.scrollHeight;
}

async function sendHomeMessage() {
  const input = $('#home-composer-input');
  const text = input.value.trim();
  if (!text && getAttachments().length === 0) return;
  const fullText = text + attachmentsAsPromptSuffix();
  const targetId = $('#home-agent-target').value;
  const modelKey = $('#home-model-select').value;
  const targets = targetId === '__all__' ? agents : agents.filter(a => a.id === targetId);
  input.value = '';
  clearAttachments();
  renderAttachmentChips();

  for (const agent of targets) {
    homeFeedAppend(agent.name, { type: 'user', text: fullText });
    appendEntry(agent, { type: 'user', text: fullText });
    setAgentState(agent, 'listening');
  }
  await sleep(200);
  for (const agent of targets) setAgentState(agent, 'thinking');

  await Promise.all(targets.map(async (agent) => {
    try {
      const reply = await callOllama(modelKey, fullText, 'You are a helpful assistant in the qwen-harness Control Room.');
      const entry = { type: 'assistant', text: reply || '(empty response)' };
      homeFeedAppend(agent.name, entry);
      appendEntry(agent, entry);
      setAgentState(agent, 'happy');
    } catch (err) {
      const entry = { type: 'system', text: `⚠ ${err.message}. Is "ollama serve" running with ${modelKey} pulled?` };
      homeFeedAppend(agent.name, entry);
      appendEntry(agent, entry);
      setAgentState(agent, 'confused');
    }
    await sleep(1200);
    setAgentState(agent, 'idle');
  }));
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
}

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
    chip.innerHTML = `<strong>${hp.label}</strong> — ${hp.phrase}`;
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
  drawerTitle.textContent = `Terminal — ${cwd || getDirectory() || '.'}`;
  drawerBody.replaceChildren();
  const output = document.createElement('div');
  output.className = 'transcript terminal-output';
  output.style.height = '160px';
  const form = document.createElement('form');
  form.className = 'composer';
  form.innerHTML = `<input type="text" placeholder="$ command" autocomplete="off" /><button type="submit">Run</button>`;
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
      const result = await apiExec(command, cwd || getDirectory() || '.');
      if (result.stdout) { const pre = document.createElement('div'); pre.textContent = result.stdout; output.append(pre); }
      if (result.stderr) { const pre = document.createElement('div'); pre.className = 'terminal-line-err'; pre.textContent = result.stderr; output.append(pre); }
    } catch (err) {
      const pre = document.createElement('div'); pre.className = 'terminal-line-err';
      pre.textContent = `⚠ ${err.message} — is gui/server.py running? (use wake-up.sh)`;
      output.append(pre);
    }
    output.scrollTop = output.scrollHeight;
  });
  drawerBody.append(output, form);
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
    card.innerHTML = `
      <div class="connector-label">
        <img src="${status.avatar_url || ''}" alt="" width="28" height="28" style="border:2px solid var(--border);" />
        <span>${status.name ? `${status.name} (@${status.login})` : `@${status.login}`}</span>
      </div>`;
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
        row.innerHTML = `<span>${r.private ? '🔒' : '📂'}</span><span>${r.full_name}</span>`;
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
        row.innerHTML = `<a href="${hit.url}" target="_blank" rel="noopener" style="font-weight:700;">${hit.title}</a>
          <div class="connector-note">${hit.url}</div>
          <div class="panel-copy">${hit.snippet}</div>`;
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
      row.innerHTML = `<span>•</span><span class="permission-strip-path" title="${root}">${root}</span>`;
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
    filetreeEl.innerHTML = `<p class="transcript-empty">⚠ ${err.message}</p>`;
  }
}

async function loadCodeTree(path) {
  currentTreeRoot = path;
  try {
    const { entries } = await apiListDir(path);
    const tree = entries.map(e => ({ name: e.name, path: e.path, type: e.type }));
    renderFileTree(filetreeEl, tree, {
      onSelect: selectTreeNode,
      selectedPath: selectedNode?.path || selectedFilePath,
      loadChildren: async (node) => {
        const { entries } = await apiListDir(node.path);
        return entries.map(e => ({ name: e.name, path: e.path, type: e.type }));
      },
    });
    $('#filetree-path').value = path;
    setDirectory(path);
  } catch (err) {
    const action = err.permission_required
      ? '<br><button class="mode-button" id="grant-failed-path" type="button">Grant folder access</button>'
      : '<br>Is gui/server.py running? Use <code>./wake-up.sh</code> or <code>python3 gui/server.py</code>.';
    filetreeEl.innerHTML = `<p class="transcript-empty">⚠ ${err.message}${action}</p>`;
    $('#grant-failed-path')?.addEventListener('click', grantAndOpenFolder);
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
  } catch (err) {
    codeFileStatus.dataset.state = 'off';
    codeFileStatusLabel.textContent = `save failed: ${err.message}`;
  }
});

$('#filetree-open-folder').addEventListener('click', grantAndOpenFolder);
$('#filetree-refresh').addEventListener('click', refreshCodeTree);

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

$('#filetree-go').addEventListener('click', () => loadCodeTree($('#filetree-path').value.trim() || '.'));
$('#filetree-path').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadCodeTree($('#filetree-path').value.trim() || '.'); });
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
  for (const entry of codeChatHistory) el.append(renderEntry(entry));
  el.scrollTop = el.scrollHeight;
}
function appendCodeEntry(entry) {
  codeChatHistory.push(entry);
  saveCodeChat();
  const el = $('#code-transcript');
  el.querySelector('.transcript-empty')?.remove();
  el.append(renderEntry(entry));
  el.scrollTop = el.scrollHeight;
}
renderCodeTranscript();
$('#code-composer').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#code-composer-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  appendCodeEntry({ type: 'user', text });
  try {
    const context = selectedFilePath ? ` The user currently has ${selectedFilePath} open.` : '';
    const content = await callOllama(modelsConfig.active, text, `You are a code assistant embedded in the qwen-harness GUI.${context} Be concise.`);
    appendCodeEntry({ type: 'assistant', text: content || '(empty response)' });
  } catch (err) {
    appendCodeEntry({ type: 'system', text: `⚠ Live call failed: ${err.message}` });
  }
});
trackComposerFocus($('#code-composer-input'));

// terminal tab (Code page)
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
    const result = await apiExec(command, currentTreeRoot);
    if (result.stdout) { const d = document.createElement('div'); d.textContent = result.stdout; output.append(d); }
    if (result.stderr) { const d = document.createElement('div'); d.className = 'terminal-line-err'; d.textContent = result.stderr; output.append(d); }
  } catch (err) {
    const d = document.createElement('div'); d.className = 'terminal-line-err';
    d.textContent = `⚠ ${err.message}`;
    output.append(d);
  }
  output.scrollTop = output.scrollHeight;
});

// browser preview tab (Code page)
$('#browser-preview-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const url = $('#browser-preview-url').value.trim();
  if (url) $('#browser-preview-frame').src = url;
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
      onCommit: (next) => { agent.name = next || agent.name; persistAgents(); if (agent.id === activeAgentId) $('#stage-name').textContent = agent.name; renderHubFocusSelect(); populateHomeSelectors(); },
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
    const stateBadge = document.createElement('span'); stateBadge.className = 'roster-state'; stateBadge.textContent = agent.state;
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
  renderStage(); renderTranscript(); renderHub(); populateHomeSelectors();
}
function addAgent() {
  const pool = manifest.map(a => a.slug);
  const agent = makeAgent(pool[agents.length % pool.length], `agent-${agents.length + 1}`, modelsConfig.active);
  agents.push(agent);
  activeAgentId = agent.id;
  persistAgents();
  renderStage(); renderTranscript(); renderHub(); populateHomeSelectors();
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
  for (const entry of agent.transcript) el.append(renderEntry(entry));
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
  await renderStage(); renderHub();
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
  const input = $('#composer-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  const agent = agents.find(a => a.id === activeAgentId);
  if (hubMode === 'live') {
    appendEntry(agent, { type: 'user', text });
    setAgentState(agent, 'listening'); await sleep(250); setAgentState(agent, 'thinking');
    try {
      const content = await callOllama(agent.modelKey, text, 'You are a concise, helpful assistant.');
      appendEntry(agent, { type: 'assistant', text: content || '(empty response)' });
      setAgentState(agent, 'happy');
    } catch (err) {
      appendEntry(agent, { type: 'system', text: `⚠ Live call failed: ${err.message}.` });
      setAgentState(agent, 'confused');
    }
    await sleep(1200); setAgentState(agent, 'idle');
  } else {
    appendEntry(agent, { type: 'user', text });
    setAgentState(agent, 'listening'); await sleep(400); setAgentState(agent, 'thinking'); await sleep(900);
    appendEntry(agent, { type: 'system', text: 'Demo mode has no model behind it — switch to Live mode, or pick a script above.' });
    setAgentState(agent, 'idle');
  }
});
trackComposerFocus($('#composer-input'));

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

const ROUTE_TITLES = { home: 'Home', code: 'Code', hub: 'Agent hub' };
function switchRoute(route) {
  document.querySelectorAll('.rail-link').forEach(link => link.classList.toggle('is-active', link.dataset.route === route));
  $('#view-home').hidden = route !== 'home';
  $('#view-code').hidden = route !== 'code';
  $('#view-hub').hidden = route !== 'hub';
  $('#route-title').textContent = ROUTE_TITLES[route];
  localStorage.setItem(ROUTE_KEY, route);
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

// ── boot ──────────────────────────────────────────────────────────────────
renderTOD();
populateHomeSelectors();
renderAttachmentChips();
await renderStage();
renderTranscript();
renderHubFocusSelect();
await refreshGrants();
await loadCodeTree(getDirectory() || '.');
pollBackend(); pollOllama();
setInterval(pollBackend, 8000);
setInterval(pollOllama, 5000);
switchRoute(localStorage.getItem(ROUTE_KEY) || 'home');
