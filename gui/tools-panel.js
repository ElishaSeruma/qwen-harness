// gui/tools-panel.js — the topbar "Tools" drawer.
//
// Lists every tool in tools/__init__.py's TOOL_DEFINITIONS as
// "Friendly name → tool_id", so you can see at a glance what the models can
// actually call and what to name in a prompt ("use the web search tool").
//
// It also shows which surfaces offer each tool. That is not decoration: the
// per-surface TOOL_PROFILES in agent-loop.js trim the tool list to keep the
// prompt small, and a tool being absent from the current surface's profile is
// otherwise invisible — it just looks like the model ignored you.

import { apiToolsList, getToolsEnabled } from './capabilities.js';
import { TOOL_PROFILES, modelSupportsTools } from './agent-loop.js';

const $ = (sel, root = document) => root.querySelector(sel);

// Groups follow the section structure of tools/__init__.py. `match` is checked
// in order, so prefixes (browser_*) win before the broader buckets.
const GROUPS = [
  { id: 'web', label: 'Web', hint: 'Reach the live internet', names: ['web_search', 'scrape_url', 'fetch_data'] },
  { id: 'files', label: 'Files', hint: 'Read and change files on disk', names: ['read_file', 'write_file', 'edit_file', 'append_file', 'list_dir', 'search_files', 'find_files'] },
  { id: 'shell', label: 'Shell', hint: 'Run commands', names: ['run_shell'] },
  { id: 'github', label: 'GitHub', hint: 'Read-only, needs a connected token', prefix: 'github_' },
  { id: 'memory', label: 'Memory', hint: 'Persists across sessions', prefix: 'memory_' },
  { id: 'browser', label: 'Browser', hint: 'Drive a real browser', prefix: 'browser_' },
  { id: 'computer', label: 'Computer control', hint: 'Mouse, keyboard, screen', names: ['screenshot', 'click', 'double_click', 'type_text', 'press_key', 'scroll'] },
  { id: 'meta', label: 'Other', hint: '', names: ['ask_user', 'create_display', 'tool_search'] },
];

// snake_case → "Sentence case", which is exactly how a user would name the
// tool in a prompt. Derived rather than hand-mapped so tools added to
// TOOL_DEFINITIONS show up here correctly without touching this file.
const WORD_FIXES = { url: 'URL', github: 'GitHub', api: 'API', html: 'HTML' };
export function friendlyToolName(name) {
  const words = String(name).split('_').map(w => WORD_FIXES[w.toLowerCase()] || w);
  const [first, ...rest] = words;
  const head = WORD_FIXES[String(first).toLowerCase()] || first.charAt(0).toUpperCase() + first.slice(1);
  return [head, ...rest].join(' ');
}

function groupFor(name) {
  for (const group of GROUPS) {
    if (group.prefix && name.startsWith(group.prefix)) return group;
    if (group.names?.includes(name)) return group;
  }
  return GROUPS.at(-1);
}

/** Surfaces whose profile includes this tool, e.g. ['Home', 'Project']. */
function surfacesFor(name) {
  return Object.entries(TOOL_PROFILES)
    .filter(([, names]) => names.includes(name))
    .map(([profile]) => profile.charAt(0).toUpperCase() + profile.slice(1));
}

let toolsCache = null;
let lastFilter = '';

export async function renderToolsDrawer({ modelKey } = {}) {
  const list = $('#tools-list');
  if (!list) return;
  if (!toolsCache) {
    try {
      toolsCache = (await apiToolsList()).tools || [];
    } catch (err) {
      list.replaceChildren(Object.assign(document.createElement('p'), {
        className: 'transcript-empty',
        textContent: `Couldn't load the tool list: ${err.message}. Is the server running?`,
      }));
      return;
    }
  }
  $('#tools-count').textContent = String(toolsCache.length);
  await renderStatus(modelKey);

  const filter = lastFilter.trim().toLowerCase();
  const matches = toolsCache.filter(t => {
    if (!filter) return true;
    const fn = t.function || {};
    return `${fn.name} ${friendlyToolName(fn.name)} ${fn.description || ''}`.toLowerCase().includes(filter);
  });

  list.replaceChildren();
  if (!matches.length) {
    list.append(Object.assign(document.createElement('p'), {
      className: 'transcript-empty', textContent: `No tool matches "${lastFilter}".`,
    }));
    return;
  }

  for (const group of GROUPS) {
    const inGroup = matches.filter(t => groupFor(t.function?.name) === group);
    if (!inGroup.length) continue;

    const section = document.createElement('section');
    section.className = 'tools-group';
    const head = document.createElement('div');
    head.className = 'tools-group-head';
    const label = document.createElement('h3');
    label.textContent = group.label;
    head.append(label);
    if (group.hint) {
      const hint = document.createElement('small');
      hint.textContent = group.hint;
      head.append(hint);
    }
    section.append(head);

    for (const tool of inGroup) {
      const fn = tool.function || {};
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'tool-row';
      row.title = 'Copy the tool name';

      const naming = document.createElement('span');
      naming.className = 'tool-naming';
      const friendly = document.createElement('strong');
      friendly.textContent = friendlyToolName(fn.name);
      const arrow = document.createElement('span');
      arrow.className = 'tool-arrow';
      arrow.textContent = '→';
      arrow.setAttribute('aria-hidden', 'true');
      const code = document.createElement('code');
      code.textContent = fn.name;
      naming.append(friendly, arrow, code);

      const desc = document.createElement('span');
      desc.className = 'tool-desc';
      desc.textContent = fn.description || '';

      const surfaces = surfacesFor(fn.name);
      const badges = document.createElement('span');
      badges.className = 'tool-surfaces';
      if (surfaces.length) {
        for (const surface of surfaces) {
          const badge = document.createElement('em');
          badge.textContent = surface;
          badges.append(badge);
        }
      } else {
        // Reachable only via tool_search — worth saying, since "it's in the
        // list but the model never calls it" is otherwise baffling.
        const badge = document.createElement('em');
        badge.className = 'is-absent';
        badge.textContent = 'via tool_search only';
        badges.append(badge);
      }

      row.append(naming, desc, badges);
      row.addEventListener('click', () => copyToolName(fn.name, row));
      section.append(row);
    }
    list.append(section);
  }
}

async function copyToolName(name, row) {
  try {
    await navigator.clipboard.writeText(name);
    row.classList.add('is-copied');
    setTimeout(() => row.classList.remove('is-copied'), 900);
  } catch {
    /* clipboard blocked (insecure context) — the name is on screen anyway */
  }
}

// Two things silently stop every tool from being callable, regardless of what
// this list shows: the Agent tools toggle being off, and the active model
// lacking Ollama's `tools` capability. Surface both at the top of the drawer.
async function renderStatus(modelKey) {
  const status = $('#tools-status');
  if (!status) return;
  const enabled = getToolsEnabled();
  let supports = null;
  if (modelKey) supports = await modelSupportsTools(modelKey).catch(() => null);

  const problems = [];
  if (!enabled) problems.push('The "Agent tools" toggle is off, so no tools are sent to the model. Turn it on in the capability bar.');
  if (supports === false) problems.push(`${modelKey} has no tool-calling support, so it can't call any of these. Switch to a tools-capable model such as qwen3:4b.`);

  status.replaceChildren();
  status.dataset.state = problems.length ? 'warn' : 'ok';
  const line = document.createElement('p');
  line.textContent = problems.length
    ? problems.join(' ')
    : `Ready${modelKey ? ` — ${modelKey} can call these.` : '.'} Name a tool in your prompt to ask for it directly.`;
  status.append(line);
}

export function mountToolsDrawer({ getModelKey } = {}) {
  const toggle = $('#tools-toggle');
  const drawer = $('#tools-drawer');
  if (!toggle || !drawer) return;

  const setOpen = (open) => {
    drawer.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open) renderToolsDrawer({ modelKey: getModelKey?.() });
  };

  toggle.addEventListener('click', () => setOpen(drawer.hidden));
  $('#tools-close')?.addEventListener('click', () => setOpen(false));
  $('#tools-filter')?.addEventListener('input', (e) => {
    lastFilter = e.target.value;
    renderToolsDrawer({ modelKey: getModelKey?.() });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !drawer.hidden) setOpen(false);
  });
  window.addEventListener('qh-tools-enabled-changed', () => {
    if (!drawer.hidden) renderStatus(getModelKey?.());
  });

  // Populate the topbar count without opening the drawer.
  apiToolsList()
    .then(r => { toolsCache = r.tools || []; $('#tools-count').textContent = String(toolsCache.length); })
    .catch(() => { /* server down; the count stays at its placeholder */ });
}
