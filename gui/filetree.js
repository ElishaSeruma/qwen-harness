// gui/filetree.js — vanilla collapsible file tree.
// Supports both a fully-static tree (children pre-populated) and lazy
// backend-driven loading (opts.loadChildren fetches a folder's contents on
// first expand). Expand/collapse state persists on the container element
// across re-renders, and each folder always shows a distinct folder icon
// (separate from the expand/collapse caret) — files get a per-extension icon.

const FILE_ICON_MAP = {
  py: { color: 'var(--chart-3)', label: 'Py' },
  js: { color: 'var(--chart-2)', label: 'JS' },
  cjs: { color: 'var(--chart-2)', label: 'JS' },
  mjs: { color: 'var(--chart-2)', label: 'JS' },
  ts: { color: 'var(--chart-3)', label: 'TS' },
  tsx: { color: 'var(--chart-3)', label: 'TX' },
  json: { color: 'var(--muted-foreground)', label: '{}' },
  md: { color: 'var(--chart-3)', label: 'M' },
  css: { color: 'var(--chart-5)', label: '#' },
  html: { color: 'var(--chart-1)', label: '<>' },
  sh: { color: 'var(--chart-4)', label: '$' },
  txt: { color: 'var(--muted-foreground)', label: 'T' },
  yml: { color: 'var(--chart-1)', label: 'Y' },
  yaml: { color: 'var(--chart-1)', label: 'Y' },
};

function extOf(name) {
  const parts = name.split('.');
  return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
}

function fileIcon(name) {
  const info = FILE_ICON_MAP[extOf(name)];
  if (!info) return { text: '·', color: 'var(--muted-foreground)' };
  return { text: info.label, color: info.color };
}

/**
 * Render a collapsible file tree into `container`.
 * @param {HTMLElement} container
 * @param {{name:string, path:string, type:'dir'|'file', children?:any[]}[]} tree
 * @param {{
 *   onSelect:(node)=>void,
 *   selectedPath?:string,
 *   loadChildren?:(node)=>Promise<any[]>,   // called once per folder, first expand
 * }} opts
 */
export function renderFileTree(container, tree, opts) {
  if (!container._qhExpanded) container._qhExpanded = new Set();
  if (!container._qhLoading) container._qhLoading = new Set();
  const expanded = container._qhExpanded;
  const loading = container._qhLoading;

  container.replaceChildren();

  function row(node, depth) {
    const isFolder = node.type === 'dir';
    const isOpen = isFolder && expanded.has(node.path);
    const isLoading = loading.has(node.path);
    const el = document.createElement('div');
    el.setAttribute('role', 'treeitem');

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ft-row' + (opts.selectedPath === node.path ? ' is-selected' : '');
    btn.style.paddingLeft = `${depth * 12 + 6}px`;

    const caret = document.createElement('span');
    caret.className = 'ft-caret';
    caret.textContent = isFolder ? (isLoading ? '…' : (isOpen ? '▾' : '▸')) : '';
    btn.append(caret);

    const icon = document.createElement('span');
    icon.className = 'ft-icon';
    if (isFolder) {
      icon.textContent = isOpen ? '📂' : '📁';
      icon.style.fontSize = '12px';
    } else {
      const info = fileIcon(node.name);
      icon.textContent = info.text;
      icon.style.color = info.color;
      icon.style.fontSize = '8px';
      icon.style.fontWeight = '700';
    }
    btn.append(icon);

    const label = document.createElement('span');
    label.className = 'ft-label';
    label.textContent = node.name;
    btn.append(label);

    btn.addEventListener('click', async () => {
      opts.onSelect(node);
      if (!isFolder) return;
      if (expanded.has(node.path)) {
        expanded.delete(node.path);
        renderFileTree(container, tree, opts);
        return;
      }
      expanded.add(node.path);
      if (!node.children && opts.loadChildren) {
        loading.add(node.path);
        renderFileTree(container, tree, opts);
        try {
          node.children = await opts.loadChildren(node);
        } catch {
          node.children = [];
        }
        loading.delete(node.path);
      }
      renderFileTree(container, tree, opts);
    });

    el.append(btn);

    if (isFolder && isOpen && Array.isArray(node.children)) {
      const kids = document.createElement('div');
      kids.className = 'ft-children';
      node.children.forEach(child => kids.append(row(child, depth + 1)));
      el.append(kids);
    }
    return el;
  }

  tree.forEach(node => container.append(row(node, 0)));
}

/** Attach `path`/`type` to a static tree (folders identified by having `children`). */
export function withPaths(nodes, prefix = '') {
  return nodes.map(n => {
    const path = prefix ? `${prefix}/${n.name}` : n.name;
    const type = n.children ? 'dir' : 'file';
    return type === 'dir'
      ? { ...n, path, type, children: withPaths(n.children, path) }
      : { ...n, path, type };
  });
}
