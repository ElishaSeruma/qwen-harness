const LABELS = {
  queued: 'Queued', planning: 'Planning', running: 'Running', listening: 'Listening',
  reading: 'Reading context', thinking: 'Thinking', working: 'Using tools',
  waiting_permission: 'Waiting for permission', verifying: 'Verifying',
  completed: 'Complete', happy: 'Complete', failed: 'Failed', confused: 'Needs attention',
  cancelled: 'Cancelled', interrupted: 'Interrupted', idle: 'Idle', sleeping: 'Idle',
};

export function normalizeActivityState(state) {
  if (['queued', 'planning', 'reading', 'thinking', 'working', 'waiting_permission', 'verifying', 'completed', 'failed', 'cancelled', 'interrupted'].includes(state)) return state;
  if (state === 'happy') return 'completed';
  if (state === 'confused') return 'failed';
  if (state === 'listening') return 'reading';
  if (state === 'running') return 'thinking';
  return 'idle';
}

export function createActivityIndicator(state = 'idle', { size = 20, label = null, detail = null } = {}) {
  const normalized = normalizeActivityState(state);
  const root = document.createElement('span');
  root.className = `activity-indicator activity-${normalized}`;
  root.dataset.state = normalized;
  root.style.setProperty('--activity-size', `${size}px`);

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 32 32');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  const ring = document.createElementNS(svg.namespaceURI, 'circle');
  ring.setAttribute('class', 'activity-ring'); ring.setAttribute('cx', '16'); ring.setAttribute('cy', '16'); ring.setAttribute('r', '11');
  const arc = document.createElementNS(svg.namespaceURI, 'path');
  arc.setAttribute('class', 'activity-arc'); arc.setAttribute('d', 'M16 5a11 11 0 0 1 11 11');
  const core = document.createElementNS(svg.namespaceURI, 'circle');
  core.setAttribute('class', 'activity-core'); core.setAttribute('cx', '16'); core.setAttribute('cy', '16'); core.setAttribute('r', '3');
  const mark = document.createElementNS(svg.namespaceURI, 'path');
  mark.setAttribute('class', 'activity-mark'); mark.setAttribute('d', normalized === 'completed' ? 'M10 16l4 4 8-9' : 'M11 11l10 10m0-10L11 21');
  svg.append(ring, arc, core, mark);
  root.append(svg);

  if (label !== false) {
    const copy = document.createElement('span'); copy.className = 'activity-copy';
    const title = document.createElement('span'); title.className = 'activity-label'; title.textContent = label || LABELS[state] || LABELS[normalized];
    copy.append(title);
    if (detail) { const small = document.createElement('small'); small.textContent = detail; copy.append(small); }
    root.append(copy);
  }
  return root;
}

export function updateActivityIndicator(root, state, detail = null) {
  const replacement = createActivityIndicator(state, {
    size: parseInt(root.style.getPropertyValue('--activity-size'), 10) || 20,
    label: root.querySelector('.activity-label')?.textContent || null,
    detail,
  });
  root.replaceWith(replacement);
  return replacement;
}
