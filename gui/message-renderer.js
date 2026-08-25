function appendInline(parent, text) {
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    if (token.startsWith('`')) {
      const code = document.createElement('code'); code.textContent = token.slice(1, -1); parent.append(code);
    } else if (token.startsWith('**')) {
      const strong = document.createElement('strong'); strong.textContent = token.slice(2, -2); parent.append(strong);
    } else {
      const parts = /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(token);
      const link = document.createElement('a'); link.textContent = parts[1]; link.href = parts[2]; link.target = '_blank'; link.rel = 'noopener noreferrer'; parent.append(link);
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
}

function appendTextBlock(root, text) {
  const lines = text.split('\n');
  let list = null;
  let paragraph = [];
  const flushParagraph = () => {
    if (!paragraph.length) return;
    const p = document.createElement('p'); appendInline(p, paragraph.join(' ')); root.append(p); paragraph = [];
  };
  const flushList = () => { if (list) { root.append(list); list = null; } };
  for (const line of lines) {
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph(); flushList();
      const h = document.createElement(`h${Math.min(4, heading[1].length + 2)}`); appendInline(h, heading[2]); root.append(h);
    } else if (bullet) {
      flushParagraph();
      if (!list) list = document.createElement('ul');
      const li = document.createElement('li'); appendInline(li, bullet[1]); list.append(li);
    } else if (!line.trim()) {
      flushParagraph(); flushList();
    } else {
      flushList(); paragraph.push(line.trim());
    }
  }
  flushParagraph(); flushList();
}

export function renderMarkdown(text) {
  const root = document.createElement('div'); root.className = 'message-markdown';
  const source = String(text || '');
  const fence = /```([\w+-]*)\n([\s\S]*?)```/g;
  let cursor = 0;
  for (const match of source.matchAll(fence)) {
    if (match.index > cursor) appendTextBlock(root, source.slice(cursor, match.index));
    const block = document.createElement('div'); block.className = 'code-block';
    const head = document.createElement('div'); head.className = 'code-block-head';
    const lang = document.createElement('span'); lang.textContent = match[1] || 'text';
    const copy = document.createElement('button'); copy.type = 'button'; copy.textContent = 'Copy';
    copy.addEventListener('click', async () => { await navigator.clipboard.writeText(match[2]); copy.textContent = 'Copied'; setTimeout(() => { copy.textContent = 'Copy'; }, 1000); });
    head.append(lang, copy);
    const pre = document.createElement('pre'); const code = document.createElement('code');
    code.className = match[1] ? `language-${match[1]}` : ''; code.textContent = match[2]; pre.append(code);
    block.append(head, pre); root.append(block);
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) appendTextBlock(root, source.slice(cursor));
  return root;
}

export function createMessageActions(entry) {
  const actions = document.createElement('div'); actions.className = 'message-actions';
  const add = (icon, action, label) => {
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = icon; button.title = label;
    button.setAttribute('aria-label', label);
    button.addEventListener('click', () => window.dispatchEvent(new CustomEvent('qh-message-action', { detail: { action, entry, source: button } })));
    actions.append(button);
  };
  if (entry.type === 'user') {
    const time = document.createElement('time');
    const parsed = new Date(entry.created_at || Date.now());
    const timestamp = Number.isNaN(parsed.getTime()) ? new Date() : parsed;
    time.dateTime = timestamp.toISOString();
    time.title = timestamp.toLocaleString();
    time.textContent = timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    actions.append(time);
    add('⧉', 'copy', 'Copy prompt');
    add('✎', 'edit', 'Edit prompt');
  } else {
    add('⧉', 'copy', 'Copy response');
  }
  return actions;
}
