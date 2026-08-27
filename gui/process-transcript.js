// gui/process-transcript.js — the collapsed "what the model is doing" panel.
//
// Appears in the feed the moment a prompt is sent, sits collapsed showing one
// live summary line, and expands to the full process: the model's reasoning,
// every tool call with its arguments, what each call returned, and the
// sandbox verify/promote steps.
//
// It is deliberately a <details> element rather than a custom widget: it gets
// keyboard support, correct semantics, and open/closed state for free.

import { friendlyToolName } from './tools-panel.js';

const MAX_STEPS = 200;
// Long tool payloads are the norm (a scraped page, a file read). Clip in the
// panel; the full value is still in the run's event log.
const MAX_VALUE = 600;

function clip(text, limit = MAX_VALUE) {
  const value = String(text ?? '');
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

function describeArgs(args) {
  if (!args || typeof args !== 'object' || !Object.keys(args).length) return '';
  const parts = [];
  for (const [key, value] of Object.entries(args)) {
    const rendered = typeof value === 'string' ? value : JSON.stringify(value);
    parts.push(`${key}: ${clip(rendered, 120)}`);
  }
  return parts.join('\n');
}

/**
 * @param {{label?:string, agentName?:string}} options
 * @returns {{el:HTMLElement, step:(s:object)=>void, reasoning:(t:string)=>void,
 *            setState:(s:string)=>void, done:(summary?:string)=>void}}
 */
export function createProcessTranscript({ label = 'Working', agentName = null } = {}) {
  const el = document.createElement('details');
  el.className = 'process-transcript';
  el.dataset.state = 'running';

  const summary = document.createElement('summary');
  const caret = document.createElement('span'); caret.className = 'process-caret'; caret.setAttribute('aria-hidden', 'true');
  const title = document.createElement('strong'); title.textContent = agentName ? `${agentName} · process` : 'Process';
  const live = document.createElement('span'); live.className = 'process-live'; live.textContent = label;
  const count = document.createElement('span'); count.className = 'process-count'; count.textContent = '0';
  summary.append(caret, title, live, count);
  el.append(summary);

  const body = document.createElement('div');
  body.className = 'process-body';

  // Reasoning gets its own block at the top, appended to as tokens stream,
  // rather than one step per chunk.
  const thinking = document.createElement('div');
  thinking.className = 'process-thinking';
  thinking.hidden = true;
  const thinkingHead = document.createElement('span');
  thinkingHead.className = 'process-thinking-head';
  thinkingHead.textContent = 'Reasoning';
  const thinkingText = document.createElement('pre');
  thinking.append(thinkingHead, thinkingText);

  const steps = document.createElement('ol');
  steps.className = 'process-steps';
  body.append(thinking, steps);
  el.append(body);

  let stepCount = 0;

  const setLive = (text) => { live.textContent = text; };

  return {
    el,

    /** One process step. kind: 'tool' | 'result' | 'note' | 'plan' | 'verify' | 'error' */
    step({ kind = 'note', text = '', name = null, args = null, ok = null }) {
      if (stepCount >= MAX_STEPS) return;
      stepCount += 1;
      count.textContent = String(stepCount);

      const item = document.createElement('li');
      item.dataset.kind = kind;
      if (ok !== null) item.dataset.ok = String(Boolean(ok));

      const headline = document.createElement('span');
      headline.className = 'process-step-head';
      headline.textContent = name ? `${friendlyToolName(name)} → ${name}` : text;
      item.append(headline);

      const detail = name ? describeArgs(args) : (name ? '' : '');
      if (detail) {
        const pre = document.createElement('pre');
        pre.className = 'process-step-detail';
        pre.textContent = detail;
        item.append(pre);
      }
      if (kind === 'result' && text) {
        const pre = document.createElement('pre');
        pre.className = 'process-step-detail';
        pre.textContent = clip(text);
        item.append(pre);
      }
      steps.append(item);

      if (kind === 'tool' && name) setLive(`using ${friendlyToolName(name).toLowerCase()}`);
      else if (kind === 'verify') setLive('running tests');
      else if (kind === 'plan') setLive('planning');
    },

    reasoning(text) {
      if (!text) return;
      thinking.hidden = false;
      thinkingText.textContent = clip(text, 6000);
      setLive('thinking');
    },

    setState(state) { el.dataset.state = state; },

    /** A turn that ended without an answer — shown, but never persisted. */
    incomplete(reason, thought) {
      el.dataset.state = 'failed';
      setLive(reason || 'no answer produced');
      if (thought) {
        thinking.hidden = false;
        thinkingText.textContent = clip(thought, 6000);
        thinkingHead.textContent = 'Abandoned reasoning (not saved to history)';
      }
    },

    done(summaryText = null) {
      el.dataset.state = 'done';
      setLive(summaryText || `${stepCount} step${stepCount === 1 ? '' : 's'}`);
    },

    fail(message) {
      el.dataset.state = 'failed';
      setLive(message ? `failed — ${clip(message, 80)}` : 'failed');
    },
  };
}
