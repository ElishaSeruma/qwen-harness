import { runAgentTurn } from './agent-loop.js';


function parsePlan(text) {
  const cleaned = String(text || '').replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/i, '').trim();
  const start = cleaned.indexOf('['), end = cleaned.lastIndexOf(']');
  if (start < 0 || end <= start) throw new Error('Planner returned no task array');
  const rows = JSON.parse(cleaned.slice(start, end + 1));
  if (!Array.isArray(rows) || !rows.length) throw new Error('Planner returned an empty task array');
  // Task ids must be unique: they key dependency resolution AND every
  // task-scoped run event. Planners routinely hand back the same id twice
  // (observed: both tasks returned id "agent-2"), which silently collapsed
  // two tasks into one in the audit trail.
  const seen = new Set();
  const uniqueId = (raw, index) => {
    let id = String(raw || `task-${index + 1}`);
    if (seen.has(id)) {
      let n = 2;
      while (seen.has(`${id}-${n}`)) n += 1;
      id = `${id}-${n}`;
    }
    seen.add(id);
    return id;
  };
  return rows.slice(0, 8).map((row, index) => ({
    id: uniqueId(row.id, index),
    title: String(row.title || `Task ${index + 1}`).slice(0, 120),
    instruction: String(row.instruction || row.title || '').slice(0, 6000),
    role: String(row.role || 'executor').toLowerCase(),
    depends_on: Array.isArray(row.depends_on) ? row.depends_on.map(String) : [],
    status: 'pending', agent_id: null, output: null, error: null,
  }));
}

function fallbackPlan(instruction) {
  return [
    { id: 'analyze', title: 'Analyze the request', instruction: `Inspect the request and project context. Identify constraints, relevant files, and a concrete implementation approach for: ${instruction}`, role: 'researcher', depends_on: [], status: 'pending' },
    { id: 'execute', title: 'Implement the solution', instruction, role: 'executor', depends_on: ['analyze'], status: 'pending' },
    { id: 'verify', title: 'Verify and review', instruction: `Review the implementation for correctness, regressions, security, and missing tests. Original request: ${instruction}`, role: 'critic', depends_on: ['execute'], status: 'pending' },
  ].map(task => ({ ...task, agent_id: null, output: null, error: null }));
}

export async function createTaskPlan({ instruction, agents, modelKey, context = '', signal, runId, onToken, effort = 1, onEffortEscalation }) {
  const roster = agents.map(agent => ({ id: agent.id, name: agent.name, model: agent.modelKey }));
  try {
    const text = await runAgentTurn({
      modelKey, signal, runId, toolsEnabled: false, onToken, effort, onEffortEscalation,
      systemPrompt: 'You are the planning controller for a desktop multi-agent coding system. Return ONLY a JSON array. Each item must contain id, title, instruction, role, and depends_on. Create 2-6 concrete tasks. Independent tasks may share no dependencies; implementation tasks should depend on analysis when necessary; finish with verification. Roles should be planner, researcher, executor, writer, or critic.',
      userText: `Instruction:\n${instruction}\n\nAvailable agents:\n${JSON.stringify(roster)}\n\nProject context:\n${context.slice(0, 12000)}`,
    });
    return parsePlan(text);
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    return fallbackPlan(instruction);
  }
}

function pickAgent(task, agents, index) {
  const role = task.role.toLowerCase();
  const exact = agents.find(agent => agent.name.toLowerCase().includes(role));
  if (exact) return exact;
  if (role === 'executor') return agents.find(agent => /file|code|writer/i.test(agent.name)) || agents[index % agents.length];
  if (role === 'critic') return agents.find(agent => /critic|review/i.test(agent.name)) || agents[index % agents.length];
  if (role === 'researcher') return agents.find(agent => /research/i.test(agent.name)) || agents[index % agents.length];
  return agents[index % agents.length];
}

export async function executeTaskGraph({ tasks, agents, worker, onChange, signal, maxConcurrency = 3 }) {
  const rows = tasks.map(task => ({ ...task }));
  let assignmentIndex = 0;
  while (rows.some(task => task.status === 'pending')) {
    if (signal?.aborted) throw new DOMException('Run cancelled', 'AbortError');
    const completeIds = new Set(rows.filter(task => task.status === 'completed').map(task => task.id));
    const failedIds = new Set(rows.filter(task => task.status === 'failed').map(task => task.id));
    for (const task of rows.filter(task => task.status === 'pending' && task.depends_on.some(id => failedIds.has(id)))) {
      task.status = 'blocked'; task.error = 'Dependency failed';
    }
    const ready = rows.filter(task => task.status === 'pending' && task.depends_on.every(id => completeIds.has(id))).slice(0, maxConcurrency);
    if (!ready.length) {
      const pending = rows.filter(task => task.status === 'pending');
      if (pending.length) pending.forEach(task => { task.status = 'blocked'; task.error = 'Unresolvable task dependencies'; });
      break;
    }
    await Promise.all(ready.map(async task => {
      const agent = pickAgent(task, agents, assignmentIndex++);
      task.agent_id = agent.id; task.status = 'running'; await onChange(rows, task);
      const dependencyContext = rows.filter(row => task.depends_on.includes(row.id)).map(row => `${row.title}:\n${row.output || row.error || ''}`).join('\n\n');
      try {
        task.output = await worker(task, agent, dependencyContext);
        task.status = 'completed';
      } catch (error) {
        if (error.name === 'AbortError') throw error;
        task.error = error.message; task.status = 'failed';
      }
      await onChange(rows, task);
    }));
  }
  return rows;
}
