export const EFFORT_LEVELS = {
  1: { name: 'Quick', detail: 'Direct response', maxTokens: 1024, toolIterations: 6, think: false, temperature: 0.45 },
  2: { name: 'Focus', detail: 'Careful execution', maxTokens: 2048, toolIterations: 10, think: false, temperature: 0.55 },
  3: { name: 'Deep', detail: 'Plan and verify', maxTokens: 4096, toolIterations: 14, think: true, temperature: 0.5 },
  4: { name: 'Max', detail: 'Maximum rigor', maxTokens: 6144, toolIterations: 18, think: true, temperature: 0.4 },
};

// Prompts that explicitly ask for tool work — "search the web for X", "use the
// web search tool", "read that file", "run the tests". These used to score as
// Quick (short prompt, no keyword signals), which then appended "Avoid
// unnecessary planning or tool calls" to the system prompt: the harness was
// literally telling the model not to do the thing the user just asked for.
// Detected here so effort never bottoms out on an explicit tool request.
const EXPLICIT_TOOL_INTENT = [
  // naming a tool outright: "use the web search tool", "call web_search"
  /\b(use|call|invoke|run)\b[^.?!]{0,40}\b(tool|web_search|scrape_url|fetch_data|read_file|write_file|edit_file|search_files|find_files|run_shell|memory_\w+)\b/i,
  /\b(web_search|scrape_url|fetch_data|read_file|write_file|edit_file|search_files|find_files|run_shell)\b/i,
  // going to the internet, by any phrasing
  /\b(search|google|look\s?up|browse|surf)\b[^.?!]{0,30}\b(web|internet|online|net)\b/i,
  /\b(go|going|head)\b[^.?!]{0,20}\b(to the )?(web|internet|online)\b/i,
  /\b(on|from|across)\s+the\s+(web|internet)\b/i,
  /\b(web ?search|google it|look (this|that|it) up|find out online)\b/i,
  // information the model cannot have: current events, live values
  /\b(latest|current|today'?s|this (week|month|year)|right now|up[- ]to[- ]date|recent)\b[^.?!]{0,30}\b(news|price|score|weather|release|version|status|results?)\b/i,
];

/**
 * True when the prompt is asking for tool work rather than for the model's own
 * knowledge. Used both to floor the effort level and, in agent-loop.js, to
 * instruct the model to actually make the call.
 */
export function wantsToolUse(text) {
  const source = String(text || '');
  return EXPLICIT_TOOL_INTENT.some(pattern => pattern.test(source));
}

const gauges = new Map();

export function inferEffort(text) {
  const source = String(text || '').trim();
  const words = source ? source.split(/\s+/).length : 0;
  let score = source.length > 1400 || words > 220 ? 4
    : source.length > 520 || words > 85 ? 3
      : source.length > 120 || words > 24 ? 2 : 1;
  const signals = [
    /```|\b(error|traceback|failing|debug|regression)\b/i,
    /\b(implement|architect|migrate|refactor|research|compare|audit|production)\b/i,
    /\b(files?|directories|repository|database|api|tests?|deploy|security)\b/i,
    /\b(first|then|after that|finally|step \d|phase \d)\b/i,
  ].filter(pattern => pattern.test(source)).length;
  if (signals >= 3) score += 2;
  else if (signals >= 1) score += 1;
  // An explicit tool request is never a Quick turn: Quick suppresses tool use
  // and caps the budget at 1024 tokens, which isn't enough to call a tool and
  // then summarize what came back.
  if (wantsToolUse(source)) score = Math.max(score, 2);
  return Math.max(1, Math.min(4, score));
}

export function parseEffortEscalation(text) {
  const match = /^\s*\[\[REQUEST_MAX_EFFORT:\s*([^\]]+)\]\]\s*$/i.exec(String(text || ''));
  return match ? { level: 4, reason: match[1].trim() } : null;
}

function meterSvg() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 40 40'); svg.setAttribute('aria-hidden', 'true');
  for (const [tag, className, attrs] of [
    ['circle', 'effort-track', { cx: 20, cy: 20, r: 15 }],
    ['path', 'effort-wave effort-wave-a', { d: 'M7 21 C12 10, 17 30, 22 17 S32 13, 34 21' }],
    ['path', 'effort-wave effort-wave-b', { d: 'M8 25 C14 18, 19 31, 25 20 S31 18, 33 24' }],
    ['circle', 'effort-core', { cx: 20, cy: 20, r: 4 }],
  ]) {
    const node = document.createElementNS(ns, tag); node.setAttribute('class', className);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    svg.append(node);
  }
  return svg;
}

function renderGauge(state, { announced = false } = {}) {
  const config = EFFORT_LEVELS[state.level];
  state.root.dataset.level = String(state.level);
  state.root.dataset.source = state.source;
  state.range.value = String(state.level);
  state.name.textContent = config.name;
  state.value.textContent = `${state.level}/4`;
  state.auto.textContent = state.source === 'auto' ? 'AUTO' : state.source === 'model' ? 'MODEL' : 'SET';
  state.root.title = `${config.name}: ${config.detail}. ${state.source === 'auto' ? 'Automatically selected from this prompt.' : state.source === 'model' ? 'Raised by the model.' : 'Manually selected.'}`;
  if (announced) {
    state.root.classList.remove('is-changing');
    requestAnimationFrame(() => state.root.classList.add('is-changing'));
  }
}

export function mountEffortGauge(surface, input, sendButton) {
  const root = document.createElement('div');
  root.className = 'effort-gauge'; root.setAttribute('role', 'group'); root.setAttribute('aria-label', 'Thinking effort');
  const visual = document.createElement('span'); visual.className = 'effort-visual'; visual.append(meterSvg());
  const copy = document.createElement('label'); copy.className = 'effort-copy';
  const head = document.createElement('span'); head.className = 'effort-head';
  const name = document.createElement('strong');
  const value = document.createElement('span'); value.className = 'effort-value';
  const auto = document.createElement('span'); auto.className = 'effort-source';
  head.append(name, value, auto);
  const range = document.createElement('input');
  range.type = 'range'; range.min = '1'; range.max = '4'; range.step = '1'; range.setAttribute('aria-label', 'Thinking effort level');
  copy.append(head, range); root.append(visual, copy);
  sendButton.parentNode.insertBefore(root, sendButton);

  const state = { root, input, range, name, value, auto, level: inferEffort(input.value), source: 'auto', locked: false };
  gauges.set(surface, state); renderGauge(state);
  input.addEventListener('input', () => {
    if (state.locked || state.source !== 'auto') return;
    const next = inferEffort(input.value);
    if (next !== state.level) { state.level = next; renderGauge(state, { announced: true }); }
  });
  range.addEventListener('input', () => {
    if (state.locked) return;
    state.level = Number(range.value); state.source = 'manual'; renderGauge(state, { announced: true });
  });
  auto.addEventListener('click', () => {
    if (state.locked) return;
    state.source = 'auto'; state.level = inferEffort(input.value); renderGauge(state, { announced: true });
  });
  return root;
}

export function getEffort(surface, text) {
  const state = gauges.get(surface);
  if (!state) return { level: inferEffort(text), source: 'auto', ...EFFORT_LEVELS[inferEffort(text)] };
  if (state.source === 'auto' && !state.locked) state.level = inferEffort(text);
  renderGauge(state);
  return { level: state.level, source: state.source, ...EFFORT_LEVELS[state.level] };
}

export function lockEffort(surface, locked) {
  const state = gauges.get(surface);
  if (!state) return;
  state.locked = locked; state.range.disabled = locked;
  state.root.classList.toggle('is-running', locked);
  if (!locked) {
    state.source = 'auto'; state.level = inferEffort(state.input.value); renderGauge(state);
  }
}

export function escalateEffort(surface, level = 4) {
  const state = gauges.get(surface);
  if (!state) return;
  state.level = Math.max(state.level, Math.min(4, level)); state.source = 'model';
  renderGauge(state, { announced: true });
}
