/**
 * The shop screen.
 *
 * Two jobs: run the step loop for ever, and show what the agent says about this
 * shop. Nothing else. There is no interaction — the pointer is hidden by the
 * stylesheet and nothing here listens for a click.
 *
 * It renders immediately, before the first /status has returned, because the
 * screen goes up when the agent starts and a black rectangle above a counter
 * reads as a broken machine rather than a starting one.
 */

'use strict';

// ── the loop ────────────────────────────────────────────────────────────────
//
// Long enough to read a step standing up and glancing away; short enough that
// somebody waiting for their print sees the whole thing more than once.
const STEP_MS = 6000;

const steps = Array.from(document.querySelectorAll('.step'));
const ruleFill = document.getElementById('rule-fill');
let current = -1;

function light(index) {
  steps.forEach((step, i) => step.classList.toggle('on', i === index));
  // The rule under the steps fills as the loop goes round: proof to somebody
  // who glanced up that the screen is live rather than frozen on step three.
  ruleFill.style.width = `${((index + 1) / steps.length) * 100}%`;
}

function advance() {
  current = (current + 1) % steps.length;
  light(current);
}

advance();
setInterval(advance, STEP_MS);

// ── this shop ───────────────────────────────────────────────────────────────

const POLL_MS = 2000;

// After this long with no answer the figures stop being presented as live.
// Three missed polls rather than one: a single slow response on a shop PC
// doing a print is ordinary, and a panel that flickered "not connected" every
// few minutes would teach a shopkeeper to ignore it.
const STALE_MS = 8000;

const el = (id) => document.getElementById(id);
const stamp = el('stamp');
const stampWord = el('stamp-word');
const stampWhy = el('stamp-why');
const paperBlock = el('paper-block');
const paperFill = el('paper-fill');
const paperLeft = el('paper-left');
const paperOf = el('paper-of');
const jobBlock = el('job-block');
const jobSheets = el('job-sheets');
const jobOf = el('job-of');
const queueBlock = el('queue-block');
const queueN = el('queue-n');
const shopName = el('shopname');
const asOf = el('asof');

let lastGood = 0;

/** The one sentence under the stamp. Says what to do, not what went wrong. */
function why(status) {
  if (!status) return 'Waking up…';
  if (!status.connected) return 'This shop cannot reach Printvendo right now.';
  if (!status.printer_ok) return 'The printer needs attention. Please ask at the counter.';
  if (status.paper && status.paper.remaining <= 0) return 'Out of paper. Please ask at the counter.';
  if (status.job) return 'A job is on the machine now.';
  return 'Ready. Send yours from the app.';
}

function render(status) {
  if (status.kiosk_name) shopName.textContent = status.kiosk_name;

  const printing = Boolean(status.job);
  const tone = status.online ? (printing ? 'busy' : 'ok') : 'bad';
  stamp.dataset.tone = tone;
  stampWord.textContent = status.online ? (printing ? 'Printing' : 'Ready') : 'Offline';
  stampWhy.textContent = why(status);

  if (status.paper) {
    paperBlock.hidden = false;
    const fraction = status.paper.fraction;
    paperFill.style.width = `${Math.round(fraction * 100)}%`;
    // Colour, not length. A shopkeeper glancing up should not have to judge a
    // bar against its track to know whether to refill.
    paperFill.classList.toggle('low', fraction <= 0.2 && fraction > 0);
    paperFill.classList.toggle('out', status.paper.remaining <= 0);
    paperLeft.textContent = String(status.paper.remaining);
    paperOf.textContent = `of ${status.paper.capacity} sheets`;
  } else {
    paperBlock.hidden = true;
  }

  if (status.job) {
    jobBlock.hidden = false;
    // The stamp above already says PRINTING and the label says PRINTING NOW.
    // What neither carries is how big the job is, which is the only thing here
    // that tells somebody whether to wait.
    const sheets = status.job.sheets;
    jobSheets.textContent = sheets == null ? '—' : String(sheets);
    jobOf.textContent = sheets === 1 ? 'sheet' : 'sheets';
  } else {
    jobBlock.hidden = true;
  }

  // Only worth a panel when somebody is actually waiting behind somebody.
  if (status.queue_depth) {
    queueBlock.hidden = false;
    queueN.textContent = String(status.queue_depth);
  } else {
    queueBlock.hidden = true;
  }
}

function markStale() {
  document.body.classList.add('stale');
  stamp.dataset.tone = 'unknown';
  stampWord.textContent = 'Offline';
  stampWhy.textContent = 'This screen has lost touch with the machine.';
  asOf.textContent = 'Not connected';
}

async function poll() {
  try {
    const response = await fetch('/status', { cache: 'no-store' });
    if (!response.ok) throw new Error(String(response.status));
    const status = await response.json();

    lastGood = Date.now();
    document.body.classList.remove('stale');
    render(status);
    asOf.textContent = status.online ? 'Live' : 'Last known';
  } catch {
    // The agent is not answering. Whatever is on screen stays, so a shop that
    // has been printing all day keeps showing its real paper count -- but it
    // stops looking live, because a stale number presented as current is worse
    // than an honest gap.
    if (Date.now() - lastGood > STALE_MS) markStale();
  }
}

poll();
setInterval(poll, POLL_MS);
