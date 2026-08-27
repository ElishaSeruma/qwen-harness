// gui/metrics.js — the bottom-right metrics panel.
//
// Collapsed to a single live line by default; click to expand into per-session
// and per-chat breakdowns. Updates while any agent is working, not just when a
// turn finishes, because the interesting question during a four-minute local
// inference is "is this going anywhere?".
//
// The numbers are real, not estimated. Since the loop moved to Ollama's native
// /api/chat, every turn returns prompt_eval_count, eval_count and the actual
// eval duration, so tokens/sec here is measured rather than assumed.

const BLANK = () => ({
  turns: 0,
  promptTokens: 0,
  completionTokens: 0,
  evalMs: 0,
  loadMs: 0,
  toolCalls: 0,
  toolErrors: 0,
  handoffs: 0,
  escalations: 0,
  incomplete: 0,
  approvals: 0,
  denials: 0,
  models: {},          // model -> turns
  startedAt: Date.now(),
});

const session = BLANK();
const chats = new Map();            // chatKey -> stats
let activeChatKey = 'default';
let inFlight = 0;                   // agents currently working
const listeners = new Set();

function bucket(key = activeChatKey) {
  if (!chats.has(key)) chats.set(key, BLANK());
  return chats.get(key);
}

function notify() {
  for (const fn of listeners) {
    try { fn(); } catch { /* a broken listener must not stop the others */ }
  }
}

export function onMetricsChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function setActiveChat(key) {
  activeChatKey = key || 'default';
  notify();
}

export function resetChatMetrics(key = activeChatKey) {
  chats.set(key, BLANK());
  notify();
}

export function turnStarted(model) {
  inFlight += 1;
  for (const stats of [session, bucket()]) {
    stats.turns += 1;
    stats.models[model] = (stats.models[model] || 0) + 1;
  }
  notify();
}

export function turnFinished() {
  inFlight = Math.max(0, inFlight - 1);
  notify();
}

/** Force the in-flight counter back to zero. A stuck "3 working" is worse than
 *  a slightly late one: it permanently claims work that is not happening. */
export function clearInFlight() {
  if (inFlight === 0) return;
  inFlight = 0;
  notify();
}

/** Usage from one completed model call (native /api/chat reports all of this). */
export function recordUsage(usage) {
  if (!usage) return;
  for (const stats of [session, bucket()]) {
    stats.promptTokens += usage.prompt_tokens || 0;
    stats.completionTokens += usage.completion_tokens || 0;
    stats.evalMs += usage.eval_duration_ms || 0;
    stats.loadMs += usage.load_duration_ms || 0;
  }
  notify();
}

/**
 * A handoff means the model that started the turn is not the one doing the
 * work. Move the turn's attribution across, or the panel keeps reporting the
 * model you asked for rather than the one that actually ran.
 */
export function recordModelSwitch(from, to) {
  if (!to || from === to) return;
  for (const stats of [session, bucket()]) {
    if (from && stats.models[from]) {
      stats.models[from] -= 1;
      if (stats.models[from] <= 0) delete stats.models[from];
    }
    stats.models[to] = (stats.models[to] || 0) + 1;
  }
  notify();
}

export function recordEvent(type, detail = {}) {
  const keys = {
    tool_call: 'toolCalls', handoff: 'handoffs', escalation: 'escalations',
    incomplete: 'incomplete', approval: 'approvals', denial: 'denials',
    tool_error: 'toolErrors',
  };
  const key = keys[type];
  if (!key) return;
  for (const stats of [session, bucket()]) stats[key] += 1;
  if (type === 'handoff' && detail.to) recordModelSwitch(detail.from, detail.to);
  notify();
}

export function isWorking() { return inFlight > 0; }

function derived(stats) {
  const tokensPerSecond = stats.evalMs > 0
    ? stats.completionTokens / (stats.evalMs / 1000)
    : null;
  return {
    ...stats,
    totalTokens: stats.promptTokens + stats.completionTokens,
    tokensPerSecond,
    elapsedMs: Date.now() - stats.startedAt,
    topModel: Object.entries(stats.models).sort((a, b) => b[1] - a[1])[0]?.[0] || null,
  };
}

export function snapshot() {
  return {
    session: derived(session),
    chat: derived(bucket()),
    chatKey: activeChatKey,
    working: inFlight,
  };
}

// ── rendering ────────────────────────────────────────────────────────────
function fmtTokens(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n || 0);
}
function fmtDuration(ms) {
  const total = ms || 0;
  // Sub-second work is normal on a fast small model; rounding it to "0s" next
  // to a real tok/s figure reads as broken instrumentation.
  if (total > 0 && total < 1000) return `${total}ms`;
  const s = Math.round(total / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

const ROWS = [
  ['Turns', d => String(d.turns)],
  ['Prompt tokens', d => fmtTokens(d.promptTokens)],
  ['Output tokens', d => fmtTokens(d.completionTokens)],
  ['Total tokens', d => fmtTokens(d.totalTokens)],
  ['Speed', d => (d.tokensPerSecond ? `${d.tokensPerSecond.toFixed(1)} tok/s` : '—')],
  ['Inference time', d => fmtDuration(d.evalMs)],
  ['Model load', d => fmtDuration(d.loadMs)],
  ['Tool calls', d => String(d.toolCalls)],
  ['Tool errors', d => String(d.toolErrors)],
  ['Handoffs', d => String(d.handoffs)],
  ['Effort raises', d => String(d.escalations)],
  ['Approvals', d => `${d.approvals} allowed / ${d.denials} denied`],
  ['Incomplete turns', d => String(d.incomplete)],
  ['Model', d => d.topModel || '—'],
];

function renderColumn(title, data) {
  const column = document.createElement('div');
  column.className = 'metrics-column';
  const head = document.createElement('h3');
  head.textContent = title;
  column.append(head);
  const list = document.createElement('dl');
  for (const [label, get] of ROWS) {
    const dt = document.createElement('dt'); dt.textContent = label;
    const dd = document.createElement('dd'); dd.textContent = get(data);
    list.append(dt, dd);
  }
  column.append(list);
  return column;
}

export function mountMetricsPanel() {
  const panel = document.createElement('details');
  panel.className = 'metrics-panel';
  panel.id = 'metrics-panel';

  const summary = document.createElement('summary');
  const dot = document.createElement('span'); dot.className = 'metrics-dot';
  const label = document.createElement('strong'); label.textContent = 'Metrics';
  const live = document.createElement('span'); live.className = 'metrics-live';
  summary.append(dot, label, live);

  const body = document.createElement('div');
  body.className = 'metrics-body';

  const actions = document.createElement('div');
  actions.className = 'metrics-actions';
  const reset = document.createElement('button');
  reset.type = 'button'; reset.className = 'mode-button'; reset.textContent = 'Reset this chat';
  reset.addEventListener('click', (e) => { e.preventDefault(); resetChatMetrics(); });
  actions.append(reset);

  panel.append(summary, body, actions);
  document.body.append(panel);

  const paint = () => {
    const data = snapshot();
    panel.dataset.working = String(data.working > 0);
    // The collapsed line has to be worth reading on its own — during a long
    // local inference it is the only thing telling you work is happening.
    live.textContent = data.working > 0
      ? `${data.working} working · ${fmtTokens(data.session.totalTokens)} tok`
      : `${data.session.turns} turns · ${fmtTokens(data.session.totalTokens)} tok`
        + (data.session.tokensPerSecond ? ` · ${data.session.tokensPerSecond.toFixed(1)} tok/s` : '');
    if (!panel.open) return;   // don't rebuild a hidden body on every event
    body.replaceChildren(
      renderColumn('This session', data.session),
      renderColumn('This chat', data.chat),
    );
  };

  panel.addEventListener('toggle', paint);
  onMetricsChange(paint);
  // A ticking clock so elapsed/among-the-live values move even between events.
  setInterval(() => { if (panel.open || isWorking()) paint(); }, 1000);
  paint();
  return panel;
}
