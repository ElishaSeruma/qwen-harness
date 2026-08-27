import {
  apiRunCancel, apiRunCreate, apiRunEvent, apiRunEvents, apiRuns, apiRunsClear, apiRunUpdate,
} from './capabilities.js';


const controllers = new Map();

function broadcast(type, detail) {
  window.dispatchEvent(new CustomEvent(type, { detail }));
}

export async function beginRun(payload) {
  const { run } = await apiRunCreate(payload);
  const controller = new AbortController();
  controllers.set(run.id, controller);
  await apiRunUpdate(run.id, { status: 'running' });
  broadcast('qh-run-changed', { run: { ...run, status: 'running' } });
  return { run: { ...run, status: 'running' }, controller };
}

export async function emitRunEvent(runId, type, data = {}) {
  if (!runId) return null;
  try {
    const result = await apiRunEvent(runId, type, data);
    broadcast('qh-run-event', { runId, event: result.event });
    return result.event;
  } catch {
    return null;
  }
}

export async function finishRun(runId, result) {
  controllers.delete(runId);
  const response = await apiRunUpdate(runId, { status: 'completed', result });
  broadcast('qh-run-changed', response);
  return response.run;
}

export async function updateRun(runId, payload) {
  const response = await apiRunUpdate(runId, payload);
  broadcast('qh-run-changed', response);
  return response.run;
}

export async function failRun(runId, error) {
  controllers.delete(runId);
  const response = await apiRunUpdate(runId, { status: 'failed', error: String(error?.message || error) });
  broadcast('qh-run-changed', response);
  return response.run;
}

export async function cancelRun(runId) {
  controllers.get(runId)?.abort();
  controllers.delete(runId);
  const response = await apiRunCancel(runId);
  broadcast('qh-run-changed', response);
  return response.run;
}

/**
 * Full event log for one run. The list endpoint deliberately strips `events`
 * (40 runs x up to 500 events each), so the audit trail fetches on expand.
 */
export async function loadRunEvents(runId) {
  return (await apiRunEvents(runId, 0)).events || [];
}

/** Drops settled runs from history. In-flight runs are always kept. */
export async function clearFinishedRuns() {
  const result = await apiRunsClear();
  broadcast('qh-run-changed', {});
  return result;
}

export function controllerForRun(runId) {
  return controllers.get(runId) || null;
}

export async function listRuns(projectId = null, limit = 50) {
  return (await apiRuns(projectId, limit)).runs || [];
}

export function subscribeToRun(runId, onEvent) {
  const stream = new EventSource(`/api/runs/stream?run_id=${encodeURIComponent(runId)}`);
  stream.addEventListener('run-event', event => {
    try { onEvent(JSON.parse(event.data)); } catch { /* malformed event is ignored */ }
  });
  return () => stream.close();
}
