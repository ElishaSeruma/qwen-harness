// gui/run-audit.js — turns a run's raw event log into a readable audit trail.
//
// The store already records everything worth knowing (which agent, which tool,
// which arguments, which task) — it was just never shown. This maps those
// events to one plain sentence each, so a run card can answer "what is it
// doing right now" and "what has it actually done" without opening a console.

/** Terminal task states, mirroring TERMINAL_TASK_STATES in run_store.py. */
export const DONE_TASK_STATES = new Set(['completed', 'failed', 'cancelled', 'skipped']);

// A tool call is the most informative thing in the log, so it gets the most
// specific phrasing: the argument that identifies *what* it acted on.
const TOOL_SUBJECT = ['path', 'file', 'query', 'url', 'command', 'pattern', 'name'];

function toolSubject(args) {
  if (!args || typeof args !== 'object') return '';
  for (const key of TOOL_SUBJECT) {
    const value = args[key];
    if (typeof value === 'string' && value.trim()) {
      return value.length > 64 ? `${value.slice(0, 61)}…` : value;
    }
  }
  return '';
}

/**
 * @param {object} event  a run event: { type, data, created_at }
 * @param {(id:string)=>string} nameFor  agent id → display name
 * @returns {{text:string, kind:'done'|'active'|'failed'|'info', at:string}|null}
 *   null for events not worth a line of their own.
 */
export function describeEvent(event, nameFor = (id) => id) {
  const data = event?.data || {};
  const at = event?.created_at || '';
  const who = data.agent_id ? nameFor(data.agent_id) : null;
  const line = (text, kind = 'info') => ({ text, kind, at });

  switch (event?.type) {
    case 'run.created': return line('Run created', 'info');
    case 'run.planning': return line('Planning the task breakdown', 'active');
    case 'run.cancelled': return line('Stopped by you', 'failed');
    case 'run.failed': return line(`Failed${data.error ? `: ${data.error}` : ''}`, 'failed');
    case 'run.completed': return line('Run complete', 'done');
    case 'run.interrupted': return line('Interrupted — the backend restarted', 'failed');
    case 'run.effort_escalated':
      return line(`Effort raised to ${data.to || 4}/4${data.reason ? ` — ${data.reason}` : ''}`, 'info');

    case 'agent.thinking': return line(`${who || data.agent_name || 'Agent'} is thinking`, 'active');
    case 'agent.completed': return line(`${who || 'Agent'} finished its reply`, 'done');
    case 'agent.failed': return line(`${who || 'Agent'} failed${data.error ? `: ${data.error}` : ''}`, 'failed');
    case 'agent.waiting_permission':
      return line(`${who || 'Agent'} is waiting for permission to ${data.scope || data.tool || 'continue'}`, 'active');

    case 'agent.tool.started': {
      const subject = toolSubject(data.args);
      return line(`${who || 'Agent'} → ${data.tool}${subject ? ` (${subject})` : ''}`, 'active');
    }
    case 'agent.tool.completed': return line(`${who || 'Agent'} → tool returned`, 'done');

    default:
      // task.<status> carries the task title, which is the most human line here.
      if (event?.type?.startsWith('task.')) {
        const status = event.type.slice(5);
        const kind = status === 'completed' ? 'done' : DONE_TASK_STATES.has(status) ? 'failed' : 'active';
        return line(`${data.title || 'Task'} — ${status}${who ? ` (${who})` : ''}`, kind);
      }
      // run.running / run.updated are bookkeeping, not history worth a line.
      return null;
  }
}

/**
 * Collapses a run's events into an audit trail, newest last.
 * Consecutive `agent.tool.completed` lines are folded into the preceding tool
 * call so a ten-step tool loop reads as ten steps, not twenty.
 */
export function buildAuditTrail(run, nameFor, limit = 40) {
  const trail = [];
  for (const event of run?.events || []) {
    const described = describeEvent(event, nameFor);
    if (!described) continue;
    const previous = trail.at(-1);
    if (described.text.endsWith('tool returned') && previous?.kind === 'active' && previous.text.includes('→')) {
      previous.kind = 'done';
      continue;
    }
    trail.push(described);
  }
  return trail.slice(-limit);
}

/** The one line that answers "what is it doing right now?". */
export function currentActivityLine(run, nameFor) {
  if (!run) return '';
  const settled = ['completed', 'failed', 'cancelled', 'interrupted'].includes(run.status);
  if (settled) {
    const tasks = run.tasks || [];
    const done = tasks.filter(t => t.status === 'completed').length;
    if (tasks.length) return `${done}/${tasks.length} subtasks completed`;
    return run.error ? `Ended: ${run.error}` : 'Finished';
  }
  for (let i = (run.events || []).length - 1; i >= 0; i--) {
    const described = describeEvent(run.events[i], nameFor);
    if (described?.kind === 'active') return described.text;
  }
  // The list endpoint strips `events` but carries the last meaningful one,
  // which is what the drawer actually polls.
  const described = describeEvent(run.last_activity, nameFor);
  if (described) return described.text;
  return 'Working…';
}
