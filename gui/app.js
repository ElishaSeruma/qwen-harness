// gui/app.js — Control Room for qwen-harness.
// Renders real avatar-lab avatars (gui/engine/avatars/*.avatar.js) as agent
// faces, drives their expression state from either scripted demo runs (works
// with no model downloaded) or live calls to a local Ollama server.

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

// Map agent lifecycle states -> avatar sequence names (all exist in every
// avatar's baked behavior block; see gui/build.cjs).
const STATE_SEQUENCE = {
  sleeping: 'sleeping', waking: 'waking', idle: 'idle', listening: 'listening',
  thinking: 'thinking', searching: 'searching', working: 'working',
  happy: 'happy', excited: 'excited', curious: 'curious', suspicious: 'suspicious',
  proud: 'proud', confused: 'confused', surprised: 'surprised', angry: 'angry',
  bored: 'bored', drowsy: 'drowsy', scared: 'scared', playful: 'playful',
};
const ALL_STATES = Object.keys(STATE_SEQUENCE);

let agentSeq = 0;
function makeAgent(slug, name, modelKey) {
  agentSeq += 1;
  return {
    id: `agent-${agentSeq}`,
    name,
    slug,
    modelKey,
    state: 'sleeping',
    handle: null,       // mounted avatar instance for the mini roster card
    stageHandle: null,  // mounted avatar instance for the big stage view
    transcript: [],
  };
}

const agents = [
  makeAgent('strobi', 'hello', 'qwen3:14b'),
  makeAgent('freddy', 'file-agent', 'qwen3:14b'),
  makeAgent('nova', 'researcher', 'qwen3:14b'),
  makeAgent('citrus', 'writer', 'qwen3:14b'),
  makeAgent('grok-bot', 'critic', 'qwen3:14b'),
];
let activeAgentId = agents[0].id;
let mode = 'demo';

// ── DOM refs ──────────────────────────────────────────────────────────────
const $ = sel => document.querySelector(sel);
const rosterList = $('#roster-list');
const stageAvatarEl = $('#stage-avatar');
const stageName = $('#stage-name');
const stageModel = $('#stage-model');
const stageState = $('#stage-state');
const stageStateLabel = $('#stage-state-label');
const avatarPicker = $('#avatar-picker');
const statePicker = $('#state-picker');
const modelPicker = $('#model-picker');
const runButtons = $('#run-buttons');
const transcriptEl = $('#transcript');
const modeCopy = $('#mode-copy');
const composer = $('#composer');
const composerInput = $('#composer-input');

// ── populate static pickers ──────────────────────────────────────────────
for (const a of manifest) {
  const opt = document.createElement('option');
  opt.value = a.slug;
  opt.textContent = a.name;
  avatarPicker.append(opt);
}
for (const s of ALL_STATES) {
  const opt = document.createElement('option');
  opt.value = s;
  opt.textContent = s;
  statePicker.append(opt);
}
for (const m of modelsConfig.models) {
  const opt = document.createElement('option');
  opt.value = m.key;
  opt.textContent = m.key + (m.local ? ' (local)' : ' (cloud)');
  modelPicker.append(opt);
}
for (const script of demoScripts) {
  const btn = document.createElement('button');
  btn.className = 'run-button';
  btn.type = 'button';
  btn.innerHTML = `${script.label}<small>${script.description}</small>`;
  btn.addEventListener('click', () => runDemoScript(script));
  runButtons.append(btn);
}

// ── avatar mounting ───────────────────────────────────────────────────────
async function mountInto(el, slug, animation) {
  el.replaceChildren();
  const mod = await loadAvatarModule(slug);
  const host = document.createElement('div');
  host.style.width = '100%';
  host.style.height = '100%';
  el.append(host);
  return mod.createAvatar(host, { size: '100%', animation, autoplay: true });
}

function setAgentState(agent, state) {
  agent.state = state;
  const seq = STATE_SEQUENCE[state] || 'idle';
  if (agent.handle) agent.handle.play(seq);
  if (agent.id === activeAgentId && agent.stageHandle) agent.stageHandle.play(seq);
  if (agent.id === activeAgentId) {
    stageState.dataset.state = ['idle', 'sleeping', 'waking'].includes(state) ? 'off' : 'ok';
    stageStateLabel.textContent = state;
    statePicker.value = state;
  }
  renderRosterCardState(agent);
}

function renderRosterCardState(agent) {
  const el = document.querySelector(`[data-agent-id="${agent.id}"] .roster-state`);
  if (el) el.textContent = agent.state;
}

// ── roster rendering ──────────────────────────────────────────────────────
async function renderRoster() {
  rosterList.replaceChildren();
  for (const agent of agents) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'roster-card' + (agent.id === activeAgentId ? ' is-active' : '');
    card.dataset.agentId = agent.id;
    card.innerHTML = `
      <div class="roster-avatar"></div>
      <div class="roster-meta">
        <div class="roster-name">${agent.name}</div>
        <div class="roster-sub">${avatarBySlug.get(agent.slug).name} · ${agent.modelKey}</div>
      </div>
      <span class="roster-state">${agent.state}</span>
      <button class="roster-remove" type="button" title="Remove agent">×</button>
    `;
    card.addEventListener('click', (e) => {
      if (e.target.closest('.roster-remove')) return;
      selectAgent(agent.id);
    });
    card.querySelector('.roster-remove').addEventListener('click', (e) => {
      e.stopPropagation();
      removeAgent(agent.id);
    });
    rosterList.append(card);
    agent.handle = await mountInto(card.querySelector('.roster-avatar'), agent.slug, STATE_SEQUENCE[agent.state]);
  }
}

async function selectAgent(id) {
  activeAgentId = id;
  await renderRoster();
  await renderStage();
  renderTranscript();
}

function removeAgent(id) {
  const idx = agents.findIndex(a => a.id === id);
  if (idx === -1 || agents.length <= 1) return;
  agents.splice(idx, 1);
  if (activeAgentId === id) activeAgentId = agents[0].id;
  renderRoster();
  renderStage();
  renderTranscript();
}

$('#add-agent').addEventListener('click', () => {
  const pool = manifest.map(a => a.slug);
  const slug = pool[agents.length % pool.length];
  const agent = makeAgent(slug, `agent-${agents.length + 1}`, modelsConfig.active);
  agents.push(agent);
  activeAgentId = agent.id;
  renderRoster();
  renderStage();
  renderTranscript();
});

// ── stage rendering ───────────────────────────────────────────────────────
async function renderStage() {
  const agent = agents.find(a => a.id === activeAgentId);
  if (!agent) return;
  stageName.textContent = agent.name;
  stageModel.textContent = `${avatarBySlug.get(agent.slug).name} · ${agent.modelKey}`;
  avatarPicker.value = agent.slug;
  modelPicker.value = agent.modelKey;
  statePicker.value = agent.state;
  agent.stageHandle = await mountInto(stageAvatarEl, agent.slug, STATE_SEQUENCE[agent.state]);
  setAgentState(agent, agent.state);
}

avatarPicker.addEventListener('change', async () => {
  const agent = agents.find(a => a.id === activeAgentId);
  agent.slug = avatarPicker.value;
  await renderRoster();
  await renderStage();
});
statePicker.addEventListener('change', () => {
  const agent = agents.find(a => a.id === activeAgentId);
  setAgentState(agent, statePicker.value);
});
modelPicker.addEventListener('change', () => {
  const agent = agents.find(a => a.id === activeAgentId);
  agent.modelKey = modelPicker.value;
  stageModel.textContent = `${avatarBySlug.get(agent.slug).name} · ${agent.modelKey}`;
  renderRoster();
});

// ── transcript rendering ─────────────────────────────────────────────────
function renderTranscript() {
  const agent = agents.find(a => a.id === activeAgentId);
  transcriptEl.replaceChildren();
  if (!agent || agent.transcript.length === 0) {
    const p = document.createElement('p');
    p.className = 'transcript-empty';
    p.textContent = 'Pick a script above to preview a run, or switch to Live mode once Ollama has a model ready.';
    transcriptEl.append(p);
    return;
  }
  for (const entry of agent.transcript) transcriptEl.append(renderEntry(entry));
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

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
    transcriptEl.querySelector('.transcript-empty')?.remove();
    transcriptEl.append(renderEntry(entry));
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// ── demo mode ─────────────────────────────────────────────────────────────
async function runDemoScript(script) {
  const agent = agents.find(a => a.id === activeAgentId);
  if (!agent) return;
  agent.transcript = [];
  agent.name = script.agentName;
  agent.modelKey = script.model;
  renderTranscript();
  renderRoster();

  for (const ev of script.events) {
    await sleep(ev.holdMs ?? 350);
    if (ev.state) setAgentState(agent, ev.state);
    if (ev.type === 'user') appendEntry(agent, { type: 'user', text: ev.text });
    else if (ev.type === 'assistant') appendEntry(agent, { type: 'assistant', text: ev.text, role: ev.role });
    else if (ev.type === 'system') {
      appendEntry(agent, { type: 'system', text: ev.text });
      if (ev.model) { agent.modelKey = ev.model; stageModel.textContent = `${avatarBySlug.get(agent.slug).name} · ${agent.modelKey}`; }
    }
    else if (ev.type === 'tool_call') appendEntry(agent, { type: 'tool_call', name: ev.name, args: ev.args });
    else if (ev.type === 'tool_result') appendEntry(agent, { type: 'tool_result', text: ev.text });
    else if (ev.type === 'handoff') {
      appendEntry(agent, { type: 'system', text: `→ handing off to "${ev.role}"` });
    }
  }
  setAgentState(agent, 'idle');
}

// ── live mode (talks to a local Ollama server directly from the browser) ──
async function runLive(userText) {
  const agent = agents.find(a => a.id === activeAgentId);
  if (!agent) return;
  appendEntry(agent, { type: 'user', text: userText });
  setAgentState(agent, 'listening');
  await sleep(250);
  setAgentState(agent, 'thinking');
  try {
    const res = await fetch('http://localhost:11434/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: agent.modelKey,
        messages: [
          { role: 'system', content: '/no_think\n\nYou are a concise, helpful assistant.' },
          { role: 'user', content: userText },
        ],
        max_tokens: 512,
        temperature: 0.6,
      }),
    });
    if (!res.ok) throw new Error(`Ollama responded ${res.status}`);
    const data = await res.json();
    const content = (data.choices?.[0]?.message?.content || '').replace(/<think>[\s\S]*?<\/think>/g, '').trim();
    appendEntry(agent, { type: 'assistant', text: content || '(empty response)' });
    setAgentState(agent, 'happy');
  } catch (err) {
    appendEntry(agent, { type: 'system', text: `⚠ Live call failed: ${err.message}. Is "ollama serve" running with ${agent.modelKey} pulled?` });
    setAgentState(agent, 'confused');
  }
  await sleep(1200);
  setAgentState(agent, 'idle');
}

composer.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = composerInput.value.trim();
  if (!text) return;
  composerInput.value = '';
  if (mode === 'live') await runLive(text);
  else {
    const agent = agents.find(a => a.id === activeAgentId);
    appendEntry(agent, { type: 'user', text });
    setAgentState(agent, 'listening');
    await sleep(400);
    setAgentState(agent, 'thinking');
    await sleep(900);
    appendEntry(agent, { type: 'system', text: 'Demo mode has no model behind it — switch to Live mode once a model has finished downloading, or pick a script above.' });
    setAgentState(agent, 'idle');
  }
});

$('#mode-demo').addEventListener('click', () => setMode('demo'));
$('#mode-live').addEventListener('click', () => setMode('live'));
function setMode(next) {
  mode = next;
  $('#mode-demo').classList.toggle('is-active', mode === 'demo');
  $('#mode-live').classList.toggle('is-active', mode === 'live');
  modeCopy.textContent = mode === 'demo'
    ? 'Demo mode — scripted, no model calls. Swap to Live once a model has finished downloading.'
    : 'Live mode — calls your local Ollama server (http://localhost:11434) directly from the browser.';
}

// ── Ollama connection polling ──────────────────────────────────────────────
async function pollOllama() {
  const pill = $('#ollama-status');
  const label = $('#ollama-status-label');
  const modelPill = $('#model-status');
  const modelLabel = $('#model-status-label');
  try {
    const res = await fetch('http://localhost:11434/api/tags', { cache: 'no-store' });
    if (!res.ok) throw new Error();
    const data = await res.json();
    const names = new Set((data.models || []).map(m => m.name));
    pill.dataset.state = 'ok';
    label.textContent = `Ollama online · ${names.size} model${names.size === 1 ? '' : 's'} pulled`;
    const active = modelsConfig.active;
    if (names.has(active)) {
      modelPill.dataset.state = 'ok';
      modelLabel.textContent = `${active} ready`;
    } else {
      modelPill.dataset.state = 'checking';
      modelLabel.textContent = `${active} still downloading…`;
    }
  } catch {
    pill.dataset.state = 'off';
    label.textContent = 'Ollama offline — run setup.sh';
    modelPill.dataset.state = 'off';
    modelLabel.textContent = `config.py: ${modelsConfig.active} (not started)`;
  }
}

// ── boot ──────────────────────────────────────────────────────────────────
setMode('demo');
await renderRoster();
await renderStage();
renderTranscript();
pollOllama();
setInterval(pollOllama, 5000);
