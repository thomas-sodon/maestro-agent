async function fetchJSON(url) {
  const r = await fetch(url);
  if (r.status === 401) { window.location.href = '/login'; throw new Error('Unauthorized'); }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// Double-submit CSRF (proposal 022): echo the JS-readable maestro_csrf
// cookie set by /login as an X-CSRF-Token header on every mutating fetch.
// Returns {} when the cookie is absent (auth disabled — no CSRF middleware).
function csrfHeaders() {
  const m = document.cookie.match(/(?:^|;\s*)maestro_csrf=([^;]+)/);
  return m ? { 'X-CSRF-Token': m[1] } : {};
}

function safeUrl(url) {
  try {
    const u = new URL(url);
    if (u.protocol === 'https:' || u.protocol === 'http:') return escapeHtml(u.href);
  } catch {}
  return '#';
}

// Skip the DOM write when nothing changed — eliminates visual churn from interval polls.
function setHtmlIfChanged(elementId, html) {
  const el = document.getElementById(elementId);
  if (!el) return;
  if (el.innerHTML !== html) el.innerHTML = html;
}

// Return a re-ordered copy of `rows` per `spec` ({ key, dir }). An empty `key`
// (or one with no accessor) means "leave the server/default order untouched" and
// returns a plain copy. Numeric values compare numerically, everything else via
// localeCompare; equal keys keep their original relative order (Array.sort is
// stable, and a zero comparison is never sign-flipped), so the sort is stable in
// both directions. Never mutates `rows` — the caller's cached array and the chart
// that shares it stay in server order.
function sortRows(rows, spec, accessors) {
  const copy = rows.slice();
  const accessor = spec.key ? accessors[spec.key] : null;
  if (!accessor) return copy;
  const factor = spec.dir === 'asc' ? 1 : -1;
  copy.sort((a, b) => {
    const av = accessor(a);
    const bv = accessor(b);
    let cmp;
    if (typeof av === 'number' && typeof bv === 'number') {
      cmp = av - bv;
    } else {
      cmp = String(av).localeCompare(String(bv));
    }
    return cmp * factor;
  });
  return copy;
}

// Emit the `<th>` header row for a sortable table. Each column descriptor is
// `{ key, label, num?, type?, sortable? }`; a `sortable === false` column renders
// as a plain header (the .num class preserved), while a sortable column carries
// `aria-sort` (ascending/descending on the active column, none otherwise) and a
// borderless `.sort-btn` button tagged with `data-sort-table` / `data-sort-key`
// for the delegated click handler, plus an aria-hidden direction arrow shown only
// while it is the active column.
function sortableHeaderCells(tableKey, columns, spec) {
  const cells = columns.map((col) => {
    const numClass = col.num ? ' class="num"' : '';
    if (col.sortable === false) {
      return `<th${numClass}>${escapeHtml(col.label)}</th>`;
    }
    const active = spec.key === col.key;
    const ariaSort = active ? (spec.dir === 'asc' ? 'ascending' : 'descending') : 'none';
    const arrow = active ? (spec.dir === 'asc' ? '▲' : '▼') : '';
    return `<th${numClass} aria-sort="${ariaSort}">`
      + `<button type="button" class="sort-btn${active ? ' active' : ''}"`
      + ` data-sort-table="${escapeHtml(tableKey)}" data-sort-key="${escapeHtml(col.key)}">`
      + `${escapeHtml(col.label)}`
      + `<span class="sort-arrow" aria-hidden="true">${arrow}</span>`
      + '</button></th>';
  }).join('');
  return `<tr>${cells}</tr>`;
}

const PENDING_STATUS = 'pending_review';
const PENDING_PUBLICATION_STATUS = 'pending_approval';

// Known proposal statuses — anything else falls back to 'unknown' so the
// card still renders (with a generic badge and no coloured border) even if
// new statuses land server-side before this code knows about them.
const PROPOSAL_STATUSES = new Set([
  'pending_approval', 'approved', 'rejected',
  'executing', 'published', 'failed',
]);

function publicationStatusClass(status) {
  if (!PROPOSAL_STATUSES.has(status)) return 'unknown';
  if (status === 'pending_approval') return 'pending';
  return status;
}

function buildIdentitySubList(title, items, firstSection) {
  if (!items.length) return '';
  const previewCount = 3;
  const preview = items.slice(0, previewCount);
  const rest = items.slice(previewCount);
  const titleStyle = firstSection ? '' : ' style="margin-top:12px"';
  const previewHtml = '<ul class="identity-list">' +
    preview.map(s => `<li>${escapeHtml(s)}</li>`).join('') + '</ul>';
  let html = `<div class="card-title"${titleStyle}>${escapeHtml(title)}</div>${previewHtml}`;
  if (rest.length) {
    const restHtml = '<ul class="identity-list">' +
      rest.map(s => `<li>${escapeHtml(s)}</li>`).join('') + '</ul>';
    html += `<details class="collapsible-list"><summary>Show ${rest.length} more</summary>${restHtml}</details>`;
  }
  return html;
}

const filterState = {
  journalSource: 'thoughts', journalType: '', journalSearch: '',
  taskOutcome: '', taskSearch: '',
  planStatus: '', planSearch: '',
  publicationStatus: '',
  sdkPhase: '',
};

// Active sort per sortable table, mirroring `filterState`. An empty `key` means
// "server/default order"; `dir` is applied by sortRows. Held in module state and
// re-applied inside the render functions so the 10 s full-refresh never resets
// the operator's chosen order.
const sortState = {
  budgetGroup: { key: '', dir: 'desc' },
  budgetCaller: { key: '', dir: 'desc' },
  versions: { key: '', dir: 'desc' },
};

// Column definitions + row accessors for each sortable table. `columns` drives
// the header row (order, label, .num class, per-column default direction via
// `type`, and which columns are sortable); `accessors` maps a column key to the
// value pulled from a row for comparison. Shared by the render functions and the
// delegated click handler so header markup and sort behaviour never drift.
const SORT_TABLES = {
  budgetGroup: {
    columns: [
      { key: 'group', label: 'Group', type: 'text' },
      { key: 'cost', label: 'Cost', type: 'number', num: true },
      { key: 'calls', label: 'Calls', type: 'number', num: true },
      { key: 'share', label: 'Share', sortable: false },
    ],
    accessors: {
      group: (g) => g.group || '',
      cost: (g) => Number(g.cost_usd) || 0,
      calls: (g) => Number(g.calls) || 0,
    },
  },
  budgetCaller: {
    columns: [
      { key: 'caller', label: 'Caller', type: 'text' },
      { key: 'group', label: 'Group', type: 'text' },
      { key: 'provider', label: 'Provider', type: 'text' },
      { key: 'cost', label: 'Cost', type: 'number', num: true },
      { key: 'calls', label: 'Calls', type: 'number', num: true },
      { key: 'reported', label: 'Reported', type: 'number', num: true },
      { key: 'estimated', label: 'Estimated', type: 'number', num: true },
      { key: 'share', label: 'Share', sortable: false },
    ],
    accessors: {
      caller: (c) => c.caller || '',
      group: (c) => c.group || '',
      provider: (c) => (c.providers || []).join(', '),
      cost: (c) => Number(c.cost_usd) || 0,
      calls: (c) => Number(c.calls) || 0,
      reported: (c) => Number(c.reported_cost_usd) || 0,
      estimated: (c) => Number(c.estimated_cost_usd) || 0,
    },
  },
  versions: {
    columns: [
      { key: 'merged', label: 'Merged', type: 'text' },
      { key: 'plan', label: 'Plan', type: 'text' },
      { key: 'cost', label: 'Cost', type: 'number' },
      { key: 'tasks', label: 'Tasks', type: 'number' },
      { key: 'errors', label: 'Errors', type: 'number' },
      { key: 'qg', label: 'QG', type: 'text' },
    ],
    accessors: {
      merged: (v) => v.merged_at || '',
      plan: (v) => v.plan_title || v.plan_id || '',
      cost: (v) => Number(v.window_sdk_cost_usd) || 0,
      tasks: (v) => Number(v.window_tasks_total) || 0,
      errors: (v) => Number(v.window_error_count) || 0,
      qg: (v) => (v.quality_gate_pass ? 'pass' : 'fail'),
    },
  },
};

const pageSize = { journal: 20, tasks: 20 };

// Server-reported page-size ceilings (`/api/status` -> `limits`). Every
// page-sized fetch is clamped to these so an operator-lowered
// `[dashboard] max_page_size` / `log_buffer_size` can never 422 a request.
let serverLimits = null;

// Clamp a desired page size to the server's ceiling for `capKey`; null while
// the ceiling is not yet known.
function clampPageSize(want, capKey) {
  const cap = serverLimits ? serverLimits[capKey] : undefined;
  if (typeof cap !== 'number' || cap <= 0) return null;
  return Math.min(want, cap);
}

// Append a clamped `n` to `path`. While the ceiling is unknown `n` is omitted
// so the endpoint's own (ceiling-safe) default applies and the request cannot 422.
function withPageSize(path, want, capKey) {
  const n = clampPageSize(want, capKey);
  if (n === null) return path;
  return `${path}${path.includes('?') ? '&' : '?'}n=${n}`;
}

// Pending-plan approval surface pages by walking `offset` (not by growing `n`),
// so every plan stays reachable no matter how large the pending total is.
// `pendingPageSize` is the per-request width; it is clamped down to the
// server-reported `max_page_size` once learned so the client can never request
// an `n` the endpoint would reject with 422. `pendingShown` is the target
// number of plans to display, grown one page at a time by "Show more".
const PENDING_PAGE_SIZE = 25;
let pendingPageSize = PENDING_PAGE_SIZE;
let pendingShown = PENDING_PAGE_SIZE;
// Until a response reveals the server's `max_page_size`, the client cannot know
// a safe explicit `n`: sending PENDING_PAGE_SIZE blindly 422s under an
// operator-lowered cap (< PENDING_PAGE_SIZE) and permanently blanks the panel.
// Stay "unlearned" and omit `n` on the bootstrap request — the endpoint clamps
// its own default to the ceiling, so a request without `n` can never 422 — then
// clamp and issue width-bearing requests once the ceiling is known.
let pendingPageSizeKnown = false;

// Cache raw data so filter changes can re-render without refetching.
const lastData = {
  journal: [], tasks: [], plansAll: [],
  pending: { plans: [], total: 0 },
  publications: [], artifactReviews: [],
  budget: null,
};

function renderStatus(st) {
  const html = `
    <span class="status-dot ${st.running ? 'active' : 'idle'}"></span>
    ${st.running ? 'Running' : 'Idle'}
    ${st.current_task ? ` — ${escapeHtml(st.current_task)}` : ''}
    <br><span style="font-size:0.8em;color:var(--text-dim)">Evolved ${st.evolution_count} times | Alive since ${st.awakened_at ? st.awakened_at.split('T')[0] : '?'}</span>
  `;
  setHtmlIfChanged('status', html);
}

function renderMetrics(m, cr) {
  const rateHtml = cr && cr.total_calls > 0
    ? `<div><div class="stat">$${cr.cost_per_hour.toFixed(2)}/hr</div><div class="stat-label">Run Rate</div></div>`
    : '';
  const html = `
    <div class="metrics-row">
      <div><div class="stat">${(m.success_rate * 100).toFixed(0)}%</div><div class="stat-label">Success Rate</div></div>
      <div><div class="stat">${m.total_tasks}</div><div class="stat-label">Tasks</div></div>
      <div><div class="stat">${m.evolution_count}</div><div class="stat-label">Evolutions</div></div>
      <div><div class="stat">${m.pending_plans}</div><div class="stat-label">Pending</div></div>
      ${rateHtml}
    </div>
  `;
  setHtmlIfChanged('metrics', html);
}

function renderIdentity(c) {
  const html =
    buildIdentitySubList('I am...', c.identity_statements, true) +
    buildIdentitySubList('Values', c.values) +
    buildIdentitySubList('Open Questions', c.open_questions) +
    buildIdentitySubList('Capabilities', c.capabilities_known) +
    buildIdentitySubList('Limitations', c.limitations_known);
  setHtmlIfChanged('identity', html || '<div class="empty">No identity data yet — Maestro is just waking up.</div>');
}

function renderPendingPlans() {
  const { plans, total } = lastData.pending;
  if (!total) {
    setHtmlIfChanged('pending-plans', '<div class="empty">No pending plans.</div>');
    return;
  }
  // "Showing X of Y" makes the count of approvable plans below the render
  // cap discoverable; the load-more button keeps every one reachable.
  const summary = `<div class="plan-summary">Showing ${plans.length} of ${total} pending plan${total === 1 ? '' : 's'}</div>`;
  const more = plans.length < total
    ? `<button class="btn" id="pending-load-more">Show more (${total - plans.length} hidden)</button>`
    : '';
  const cards = plans.map(p => `
    <div class="plan-card pending">
      <div class="plan-title">${escapeHtml(p.title)}</div>
      <div class="plan-meta">${escapeHtml(p.category)} | ${escapeHtml(p.priority || 'capability')} | ${p.created_at.split('T')[0]}${p.refined_at ? ` | Refined: ${p.refined_at.split('T')[0]}` : ''} | Hash: ${escapeHtml((p.plan_content_hash || '').slice(0, 12))}</div>
      <div style="margin-top:8px;font-size:0.9em;">${escapeHtml(p.rationale)}</div>
      <div style="margin-top:8px;font-size:0.85em;color:var(--text-dim)">Impact: ${escapeHtml(p.expected_impact)}</div>
      ${p.rejection_reason ? `<div style="margin-top:4px;font-size:0.85em;color:var(--danger)">Previous failure: ${escapeHtml(p.rejection_reason)}</div>` : ''}
      <div style="margin-top:4px;font-size:0.85em;">
        ${p.proposed_changes.map(c => `<div>• ${escapeHtml(c)}</div>`).join('')}
      </div>
      ${renderPlanFeedback(p.reviewer_feedback || [])}
      <button class="btn btn-approve" data-action="approve" data-plan-id="${escapeHtml(p.plan_id)}" data-content-hash="${escapeHtml(p.plan_content_hash || '')}">Approve</button>
      <button class="btn" data-action="feedback" data-plan-id="${escapeHtml(p.plan_id)}" data-content-hash="${escapeHtml(p.plan_content_hash || '')}">Request changes</button>
      <button class="btn btn-reject" data-action="reject" data-plan-id="${escapeHtml(p.plan_id)}" data-content-hash="${escapeHtml(p.plan_content_hash || '')}">Reject</button>
    </div>
  `).join('');
  setHtmlIfChanged('pending-plans', summary + cards + more);
}

// Reviewer-feedback status on a pending card (proposal 145): a badge while
// any entry awaits the curator, and Maestro's newest response once addressed.
function renderPlanFeedback(entries) {
  const pending = entries.some(f => f.addressed_at === null);
  const addressed = entries.filter(f => f.addressed_at !== null && f.response);
  const latest = addressed.length
    ? addressed.reduce((a, b) => (b.addressed_at > a.addressed_at ? b : a))
    : null;
  const badge = pending
    ? '<div class="plan-feedback-pending" style="margin-top:6px;font-size:0.85em;color:var(--accent)">Feedback pending</div>'
    : '';
  const response = latest
    ? `<div class="plan-feedback-response" style="margin-top:4px;font-size:0.85em;color:var(--text-dim)">Maestro: ${escapeHtml(latest.response)}</div>`
    : '';
  return badge + response;
}

function renderAllPlans() {
  let nonPending = lastData.plansAll.filter(p => p.status !== PENDING_STATUS);
  if (filterState.planStatus) {
    nonPending = nonPending.filter(p => p.status === filterState.planStatus);
  }
  if (filterState.planSearch) {
    const q = filterState.planSearch.toLowerCase();
    nonPending = nonPending.filter(p =>
      (p.title || '').toLowerCase().includes(q) ||
      (p.rationale || '').toLowerCase().includes(q) ||
      (p.category || '').toLowerCase().includes(q),
    );
  }
  if (!nonPending.length) {
    setHtmlIfChanged('all-plans', '<div class="empty">No completed plans yet.</div>');
    return;
  }
  const html = nonPending.map(p => `
    <div class="plan-card ${escapeHtml(p.status)}">
      <div class="plan-title">${escapeHtml(p.title)}</div>
      <div class="plan-meta">${escapeHtml(p.status)} | ${escapeHtml(p.category)} | ${escapeHtml(p.priority || 'capability')} | ${p.created_at.split('T')[0]}
        ${p.pr_url ? ` | <a href="${safeUrl(p.pr_url)}" style="color:var(--accent)" target="_blank" rel="noopener">PR</a>` : ''}
        ${p.rejection_reason ? ` | Reason: ${escapeHtml(p.rejection_reason)}` : ''}
        ${p.obsolete_reason ? ` | Obsolete because: ${escapeHtml(p.obsolete_reason)}` : ''}
      </div>
    </div>
  `).join('');
  setHtmlIfChanged('all-plans', html);
}

function renderJournal() {
  let entries = lastData.journal.slice().reverse();
  if (filterState.journalType) {
    entries = entries.filter(e => e.entry_type === filterState.journalType);
  }
  if (filterState.journalSearch) {
    const q = filterState.journalSearch.toLowerCase();
    entries = entries.filter(e => (e.content || '').toLowerCase().includes(q));
  }
  if (!entries.length) {
    const empty = filterState.journalSource === 'events'
      ? 'No journal events recorded yet.'
      : 'Journal is empty — Maestro hasn\'t written any thoughts yet.';
    setHtmlIfChanged('journal', `<div class="empty">${empty}</div>`);
    return;
  }
  const html = entries.map(e => `
    <div class="journal-entry">
      <div class="journal-meta">
        ${e.timestamp.split('T')[0]} ${e.timestamp.split('T')[1]?.split('.')[0] || ''}
        <span class="journal-type type-${escapeHtml(e.entry_type)}">${escapeHtml(e.entry_type)}</span>
        ${e.triggered_by ? `<span style="margin-left:8px">← ${escapeHtml(e.triggered_by)}</span>` : ''}
      </div>
      <div style="font-size:0.9em;white-space:pre-wrap;">${escapeHtml(e.content)}</div>
    </div>
  `).join('');
  setHtmlIfChanged('journal', html);
}

function renderVersions() {
  const base = (lastData.versions || []).slice().reverse();
  if (!base.length) {
    setHtmlIfChanged('versions', '<div class="empty">No merged versions yet.</div>');
    return;
  }
  // The empty-key default leaves the newest-first order the reverse() produced.
  const cfg = SORT_TABLES.versions;
  const versions = sortRows(base, sortState.versions, cfg.accessors);
  const rows = versions.map(v => {
    const merged = (v.merged_at || '').replace('T', ' ').split('.')[0];
    const cost = Number(v.window_sdk_cost_usd || 0).toFixed(2);
    const rate = v.window_tasks_total
      ? `${v.window_tasks_succeeded}/${v.window_tasks_total}`
      : '0/0';
    const qg = v.quality_gate_pass ? 'pass' : 'fail';
    return `
      <tr>
        <td>${escapeHtml(merged)}</td>
        <td>${escapeHtml(v.plan_title || v.plan_id || '')}</td>
        <td>$${escapeHtml(cost)}</td>
        <td>${escapeHtml(rate)}</td>
        <td>${escapeHtml(String(v.window_error_count || 0))}</td>
        <td>${escapeHtml(qg)}</td>
      </tr>`;
  }).join('');
  // Wrap in the same focusable scroll region the Budget tables use so this wide
  // table scrolls horizontally instead of overflowing the 375 px layout.
  const html = `
    <div class="table-scroll" role="region" tabindex="0" aria-label="Version archive, data table">
    <table class="version-table">
      <thead>${sortableHeaderCells('versions', cfg.columns, sortState.versions)}</thead>
      <tbody>${rows}</tbody>
    </table>
    </div>`;
  setHtmlIfChanged('versions', html);
}

async function refreshVersions() {
  try {
    lastData.versions = await fetchJSON(withPageSize('/api/versions', 20, 'max_page_size'));
    renderVersions();
  } catch (err) {
    console.error('Versions refresh error:', err);
  }
}

function renderTasks() {
  let tasks = lastData.tasks.slice().reverse();
  if (filterState.taskOutcome) {
    tasks = tasks.filter(t => t.outcome === filterState.taskOutcome);
  }
  if (filterState.taskSearch) {
    const q = filterState.taskSearch.toLowerCase();
    tasks = tasks.filter(t => (t.goal || '').toLowerCase().includes(q));
  }
  if (!tasks.length) {
    setHtmlIfChanged('tasks', '<div class="empty">No tasks completed yet.</div>');
    return;
  }
  const html = tasks.map(t => `
    <div class="task-row">
      <span>${escapeHtml(t.goal.substring(0, 80))}</span>
      <span class="outcome-${escapeHtml(t.outcome)}">${escapeHtml(t.outcome)} (${t.duration_secs}s)</span>
    </div>
  `).join('');
  setHtmlIfChanged('tasks', html);
}

function setBadge(id, count) {
  const el = document.getElementById(id);
  if (!el) return;
  if (count > 0) {
    el.textContent = String(count);
    el.style.display = 'inline-block';
  } else {
    el.style.display = 'none';
  }
}

async function refreshJournal() {
  const source = filterState.journalSource;
  const entries = await fetchJSON(withPageSize(`/api/journal?source=${source}`, pageSize.journal, 'max_page_size'));
  // A source toggle mid-fetch supersedes this response.
  if (source !== filterState.journalSource) return;
  lastData.journal = entries;
  renderJournal();
}

async function refreshTasks() {
  lastData.tasks = await fetchJSON(withPageSize('/api/tasks', pageSize.tasks, 'max_page_size'));
  renderTasks();
}

// Fetch up to `target` pending plans by walking `offset` forward in
// fixed-width pages, concatenating each page. Once the server's `max_page_size`
// is learned, the per-request `n` is clamped to it so growing the visible window
// never trips the endpoint's `le=max_page_size` 422. The bootstrap request
// (ceiling not yet known) omits `n` entirely so it, too, cannot 422 under a
// lowered cap. Every plan is reachable regardless of total — the button can no
// longer become a no-op that advertises hidden plans it cannot load.
async function fetchPendingWindow(target) {
  const plans = [];
  let total = 0;
  let offset = 0;
  while (offset < target) {
    // Omit `n` until the ceiling is known so the bootstrap request cannot exceed
    // an unknown `max_page_size` and 422; afterwards clamp to the learned limit.
    let url = `/api/plans/pending?offset=${offset}`;
    if (pendingPageSizeKnown) {
      url += `&n=${Math.min(pendingPageSize, target - offset)}`;
    }
    const page = await fetchJSON(url);
    total = page.total;
    if (typeof page.max_page_size === 'number' && page.max_page_size > 0) {
      // Align the client ceiling to the configurable server limit.
      pendingPageSize = Math.min(PENDING_PAGE_SIZE, page.max_page_size);
      pendingPageSizeKnown = true;
    }
    plans.push(...page.plans);
    offset += page.count;
    if (page.count === 0 || offset >= total) break;
  }
  return { plans, total };
}

async function refreshPlans() {
  // Fetch the two plan sources independently (allSettled) so a failure in one
  // does not discard the other's result and blank both panels. The approval
  // surface walks the paginated pending endpoint so every pending plan stays
  // reachable with a visible X-of-Y total; the non-pending "all plans" tab
  // keeps using the unbounded /api/plans list.
  const [all, pending] = await Promise.allSettled([
    fetchJSON('/api/plans'),
    fetchPendingWindow(pendingShown),
  ]);
  if (all.status === 'fulfilled') {
    lastData.plansAll = all.value;
    renderAllPlans();
  }
  if (pending.status === 'fulfilled') {
    lastData.pending = pending.value;
    renderPendingPlans();
    setBadge('nav-badge-plans', pending.value.total);
  }
}

function getPendingPublications() {
  return lastData.publications.filter(
    p => p.status === PENDING_PUBLICATION_STATUS,
  );
}

function reviewByTaskId(taskId) {
  for (let i = lastData.artifactReviews.length - 1; i >= 0; i--) {
    if (lastData.artifactReviews[i].task_id === taskId) {
      return lastData.artifactReviews[i];
    }
  }
  return null;
}

function renderScrubChips(findings) {
  if (!findings || !findings.length) return '';
  const chips = findings.map(f => {
    const kind = (f || '').split(':')[0] || f;
    const isNarrative = kind.startsWith('narrative_redactions');
    const cls = isNarrative ? 'chip chip-warn' : 'chip';
    return `<span class="${cls}">${escapeHtml(f)}</span>`;
  }).join(' ');
  return `<div style="margin-top:6px;">${chips}</div>`;
}

function renderPendingPublications() {
  const pending = getPendingPublications();
  if (!pending.length) {
    setHtmlIfChanged(
      'pending-publications',
      '<div class="empty">No pending publication proposals.</div>',
    );
    return;
  }
  const html = pending.map(p => {
    const review = reviewByTaskId(p.task_id);
    const reasoning = review ? (review.reasoning || '') : '';
    const readme = p.proposed_readme || '';
    const readmeBlock = readme
      ? `<details class="collapsible-list" style="margin-top:8px;">
           <summary>README preview (${readme.length} chars)</summary>
           <pre class="readme-preview">${escapeHtml(readme)}</pre>
         </details>`
      : '<div style="margin-top:6px;font-size:0.8em;color:var(--text-dim)">No drafted README.</div>';
    return `
      <div class="plan-card pending">
        <div class="plan-title">${escapeHtml(p.proposed_name)}</div>
        <div class="plan-meta">publication | task <a href="#section-tasks" style="color:var(--accent)">${escapeHtml(p.task_id)}</a> | ${(p.created_at || '').split('T')[0]}</div>
        ${p.description ? `<div style="margin-top:6px;font-size:0.9em;">${escapeHtml(p.description)}</div>` : ''}
        ${reasoning ? `<div style="margin-top:6px;font-size:0.85em;color:var(--text-dim);">${escapeHtml(reasoning)}</div>` : ''}
        <div style="margin-top:6px;font-size:0.8em;color:var(--text-dim);">${p.publishable_files.length} file(s)</div>
        ${renderScrubChips(p.scrub_findings)}
        ${readmeBlock}
        <button class="btn btn-approve" data-action="pub-approve" data-proposal-id="${escapeHtml(p.proposal_id)}">Approve</button>
        <button class="btn btn-reject" data-action="pub-reject" data-proposal-id="${escapeHtml(p.proposal_id)}">Reject</button>
      </div>
    `;
  }).join('');
  setHtmlIfChanged('pending-publications', html);
}

function renderAllPublications() {
  let nonPending = lastData.publications.filter(
    p => p.status !== PENDING_PUBLICATION_STATUS,
  );
  if (filterState.publicationStatus) {
    nonPending = nonPending.filter(
      p => p.status === filterState.publicationStatus,
    );
  }
  if (!nonPending.length) {
    setHtmlIfChanged(
      'all-publications',
      '<div class="empty">No resolved publication proposals yet.</div>',
    );
    return;
  }
  const html = nonPending.map(p => {
    const cls = publicationStatusClass(p.status);
    const urlChip = p.published_url
      ? ` | <a href="${safeUrl(p.published_url)}" style="color:var(--accent)" target="_blank" rel="noopener">repo</a>`
      : '';
    const errorBlock = p.error
      ? `<div style="margin-top:4px;font-size:0.85em;color:var(--danger);">Error: ${escapeHtml(p.error)}</div>`
      : '';
    const reasonBlock = p.rejection_reason
      ? `<div style="margin-top:4px;font-size:0.85em;color:var(--text-dim);">Reason: ${escapeHtml(p.rejection_reason)}</div>`
      : '';
    const readme = p.proposed_readme || '';
    const readmeBlock = readme
      ? `<details class="collapsible-list" style="margin-top:8px;">
           <summary>README</summary>
           <pre class="readme-preview">${escapeHtml(readme)}</pre>
         </details>`
      : '';
    return `
      <div class="plan-card ${escapeHtml(cls)}">
        <div class="plan-title">${escapeHtml(p.proposed_name)}</div>
        <div class="plan-meta">${escapeHtml(p.status)} | task ${escapeHtml(p.task_id)} | ${(p.created_at || '').split('T')[0]}${urlChip}</div>
        ${errorBlock}
        ${reasonBlock}
        ${readmeBlock}
      </div>
    `;
  }).join('');
  setHtmlIfChanged('all-publications', html);
}

async function refreshPublications() {
  try {
    lastData.publications = await fetchJSON('/api/publications');
    renderPendingPublications();
    renderAllPublications();
    setBadge('nav-badge-publications', getPendingPublications().length);
  } catch (err) {
    console.error('Publications refresh error:', err);
  }
}

async function refreshArtifactReviews() {
  try {
    lastData.artifactReviews = await fetchJSON(withPageSize('/api/artifact-reviews', 50, 'max_page_size'));
    // Re-render pending publications to pick up newly loaded reasoning.
    renderPendingPublications();
  } catch (err) {
    console.error('Artifact reviews refresh error:', err);
  }
}

function setPublicationButtonsDisabled(proposalId, disabled) {
  document.querySelectorAll(
    `[data-proposal-id="${proposalId}"]`,
  ).forEach(btn => {
    btn.disabled = disabled;
    if (disabled) {
      btn.dataset.origText = btn.textContent;
      btn.textContent = 'Processing...';
      btn.style.opacity = '0.5';
    } else {
      btn.textContent = btn.dataset.origText || btn.textContent;
      btn.style.opacity = '1';
    }
  });
}

async function approvePublication(proposalId) {
  const { confirmed } = await openConfirmDialog({
    title: 'Approve publication proposal',
    message: 'Approve this publication proposal? It will be queued for publishing.',
    confirmLabel: 'Approve',
    tone: 'approve',
  });
  if (!confirmed) return;
  setPublicationButtonsDisabled(proposalId, true);
  try {
    const r = await fetch(
      `/api/publications/${proposalId}/approve`,
      { method: 'POST', headers: csrfHeaders() },
    );
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(`Approval failed: ${err.error || r.statusText}`, 'error');
      setPublicationButtonsDisabled(proposalId, false);
      return;
    }
    showToast('Publication proposal approved.', 'success');
  } catch (e) {
    showToast(`Network error: ${e.message}`, 'error');
    setPublicationButtonsDisabled(proposalId, false);
    return;
  }
  refresh();
}

async function rejectPublication(proposalId) {
  const { confirmed, reason: rawReason } = await openConfirmDialog({
    title: 'Reject publication proposal',
    message: 'Reject this publication proposal?',
    confirmLabel: 'Reject',
    tone: 'reject',
    withReason: true,
  });
  if (!confirmed) return;
  const reason = rawReason || 'No reason provided';
  setPublicationButtonsDisabled(proposalId, true);
  try {
    const r = await fetch(`/api/publications/${proposalId}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
      body: JSON.stringify({ reason }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(`Rejection failed: ${err.error || r.statusText}`, 'error');
      setPublicationButtonsDisabled(proposalId, false);
      return;
    }
    showToast('Publication proposal rejected.', 'info');
  } catch (e) {
    showToast(`Network error: ${e.message}`, 'error');
    setPublicationButtonsDisabled(proposalId, false);
    return;
  }
  refresh();
}

function formatTokenRate(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k';
  return n.toFixed(0);
}

function renderDiagnostics(d, cr) {
  const sdk = d.sdk_usage;
  let html = `<div style="font-size:0.85em;">${escapeHtml(d.summary || '')}</div>`;
  if (sdk) {
    const cachePct = (sdk.cache_hit_rate * 100).toFixed(0);
    const cacheClass = sdk.cache_hit_rate < 0.3 ? 'outcome-failure'
                     : sdk.cache_hit_rate < 0.6 ? 'outcome-partial'
                     : 'outcome-success';
    const rlClass = sdk.rate_limit_hits > 0 ? 'outcome-failure' : 'outcome-success';
    html += `
      <div style="display:flex;gap:24px;margin-top:12px;flex-wrap:wrap;">
        <div><div class="stat">${sdk.total_calls}</div><div class="stat-label">SDK Calls</div></div>
        <div><div class="stat">${(sdk.total_input_tokens / 1000).toFixed(1)}k</div><div class="stat-label">Tokens In</div></div>
        <div><div class="stat">${(sdk.total_output_tokens / 1000).toFixed(1)}k</div><div class="stat-label">Tokens Out</div></div>
        <div><div class="stat ${cacheClass}">${cachePct}%</div><div class="stat-label">Cache Hit</div></div>
        <div><div class="stat">$${sdk.total_cost_usd.toFixed(2)}</div><div class="stat-label">Cost</div></div>
        <div><div class="stat ${rlClass}">${sdk.rate_limit_hits}</div><div class="stat-label">Rate-Limit Hits</div></div>
      </div>
    `;
    const callers = Object.keys(sdk.by_caller).sort(
      (a, b) => (sdk.cost_by_caller[b] || 0) - (sdk.cost_by_caller[a] || 0),
    );
    if (callers.length) {
      html += '<div class="card-title" style="margin-top:16px">By Caller</div>';
      html += '<table class="diag-table" style="width:100%;font-size:0.85em;border-collapse:collapse;">';
      html += '<tr style="color:var(--text-dim);text-align:left;"><th>Caller</th><th style="text-align:right;">Calls</th><th style="text-align:right;">Cost</th></tr>';
      for (const c of callers) {
        html += `<tr><td>${escapeHtml(c)}</td><td style="text-align:right;">${sdk.by_caller[c]}</td><td style="text-align:right;">$${(sdk.cost_by_caller[c] || 0).toFixed(3)}</td></tr>`;
      }
      html += '</table>';
    }
  } else {
    html += '<div class="empty" style="padding:8px 0">No SDK calls observed in the recent log window.</div>';
  }
  if (cr && cr.total_calls > 0) {
    html += `
      <div class="card-title" style="margin-top:16px">Run Rates (last ${cr.window_seconds >= 3600 ? (cr.window_seconds / 3600).toFixed(1) + 'h' : Math.round(cr.window_seconds) + 's'})</div>
      <div style="display:flex;gap:24px;margin-top:8px;flex-wrap:wrap;">
        <div><div class="stat">$${cr.cost_per_hour.toFixed(2)}/hr</div><div class="stat-label">Cost Rate</div></div>
        <div><div class="stat">$${cr.cost_per_day.toFixed(1)}/day</div><div class="stat-label">Projected</div></div>
        <div><div class="stat">$${cr.cost_per_month.toFixed(0)}/mo</div><div class="stat-label">Projected</div></div>
        <div><div class="stat">${formatTokenRate(cr.tokens_in_per_hour)}/hr</div><div class="stat-label">Tokens In</div></div>
        <div><div class="stat">${formatTokenRate(cr.tokens_out_per_hour)}/hr</div><div class="stat-label">Tokens Out</div></div>
      </div>
    `;
  }
  setHtmlIfChanged('diagnostics', html);
}

let activeBudgetWindow = '1';

// Format a cost figure as "$N.NN", or as an em-dash when the spend is
// unmetered (a usage_metering=False provider could not measure it, so the
// cost is unknown rather than zero — proposal 058 Step 13).
function formatCost(value, unmetered, digits = 2) {
  if (unmetered) return '—';
  return '$' + value.toFixed(digits);
}

// Ranked horizontal bar chart of spend by phase group, drawn as inline SVG
// from the same `by_group` records the table below it lists (proposal 122).
// The payload already arrives sorted descending by cost, so nothing is ranked
// client-side. Every bar carries its own text value, so meaning is never
// encoded in bar length or colour alone, and the <svg role="img"> takes its
// single accessible name from the visible caption rendered above it.
//
// The SVG is `width="100%"` with an explicit pixel height and no viewBox, so
// it is fluid horizontally without the uniform-scale text distortion a
// viewBox + preserveAspectRatio pairing would introduce.
const CHART_ROW_HEIGHT = 34;  // px per group row: label line, bar, and gap
const CHART_BAR_HEIGHT = 8;   // matches the .bar-track share bars in the tables

function renderGroupChart(byGroup, totalCostUsd, allUnmetered) {
  // A window whose groups are all unmetered has a maximum cost of 0, so the
  // bar-width divisor is guarded: those rows render as zero-width bars carrying
  // the honest em-dash from formatCost rather than dividing by zero or
  // implying a measured $0.00.
  const maxCost = Math.max(...byGroup.map((g) => g.cost_usd));
  const top = byGroup[0];
  const topPct = (top.share_of_total * 100).toFixed(0);
  const groupWord = byGroup.length === 1 ? 'group' : 'groups';
  const caption = `${escapeHtml(top.group)} leads at ${topPct}% of `
    + `${formatCost(totalCostUsd, allUnmetered)} across ${byGroup.length} ${groupWord}`;

  let rows = '';
  byGroup.forEach((g, i) => {
    const y = i * CHART_ROW_HEIGHT;
    const pct = (g.share_of_total * 100).toFixed(0);
    const width = maxCost > 0 ? (g.cost_usd / maxCost) * 100 : 0;
    // The label is clipped to the reserved left column (see the clipPath below)
    // so a long group name can never overrun the right-anchored value it shares
    // a baseline with — SVG <text> has no native ellipsis to fall back on.
    rows += `<text class="budget-chart-label" x="0" y="${y + 11}"`
      + ' clip-path="url(#budget-chart-label-clip)">'
      + `${escapeHtml(g.group)}</text>`
      + `<text class="budget-chart-value" x="100%" y="${y + 11}" text-anchor="end">`
      + `${formatCost(g.cost_usd, g.unmetered, 3)} · ${pct}%</text>`
      + `<rect class="budget-chart-bar" x="0" y="${y + 18}" width="${width.toFixed(2)}%"`
      + ` height="${CHART_BAR_HEIGHT}" rx="3"></rect>`;
  });

  // Reserve a fixed right-hand column for the value text: the clip rect's
  // width="100%" attribute is a graceful fallback (labels stay full-width if the
  // stylesheet fails to load, never vanish), overridden in CSS to
  // calc(100% - value gutter) so the label region always stops short of the value.
  const defs = '<defs><clipPath id="budget-chart-label-clip">'
    + '<rect class="budget-chart-label-clip-rect" x="0" y="0" width="100%" height="100%"></rect>'
    + '</clipPath></defs>';

  const html = '<div class="budget-chart">'
    + `<div class="budget-chart-caption" id="budget-chart-caption">${caption}</div>`
    + '<svg class="budget-chart-svg" role="img" aria-labelledby="budget-chart-caption"'
    + ` width="100%" height="${byGroup.length * CHART_ROW_HEIGHT}">${defs}${rows}</svg>`
    + '</div>';
  setHtmlIfChanged('budget-group-chart', html);
}

function renderBudget(b) {
  // The whole window is unmetered only when every caller is — then even the
  // top-line totals are unknown rather than zero.
  const allUnmetered = b.by_caller.length > 0 && b.by_caller.every((c) => c.unmetered);
  const summary = `
    <div><div class="stat">${formatCost(b.total_cost_usd, allUnmetered)}</div><div class="stat-label">Total</div></div>
    <div><div class="stat">${formatCost(b.reported_cost_usd, allUnmetered)}</div><div class="stat-label">Reported</div></div>
    <div><div class="stat">${formatCost(b.estimated_cost_usd, allUnmetered)}</div><div class="stat-label">Estimated</div></div>
    <div><div class="stat">${b.total_calls}</div><div class="stat-label">Calls</div></div>
  `;
  setHtmlIfChanged('budget-summary', summary);

  if (!b.by_group.length) {
    // Clear the chart container too — without this its cold-state skeleton is
    // never replaced and shimmers forever on an idle window.
    setHtmlIfChanged('budget-group-chart', '<div class="empty">No spend in this window.</div>');
    setHtmlIfChanged('budget-by-group', '<div class="empty">No spend in this window.</div>');
    setHtmlIfChanged('budget-by-caller', '<div class="empty">No spend in this window.</div>');
    return;
  }

  renderGroupChart(b.by_group, b.total_cost_usd, allUnmetered);

  // Sort a copy under the by-group sort state; the empty-key default preserves
  // the server's cost-descending order, so the composition chart above (which
  // reads the untouched b.by_group) and this table stay in sync until the
  // operator picks a column.
  const groupCfg = SORT_TABLES.budgetGroup;
  const groupRows = sortRows(b.by_group, sortState.budgetGroup, groupCfg.accessors);
  let groupHtml = '<table class="budget-table">'
    + sortableHeaderCells('budgetGroup', groupCfg.columns, sortState.budgetGroup);
  for (const g of groupRows) {
    const pct = (g.share_of_total * 100).toFixed(0);
    groupHtml += `<tr>
      <td><span class="group-tag">${escapeHtml(g.group)}</span></td>
      <td class="num">${formatCost(g.cost_usd, g.unmetered, 3)}</td>
      <td class="num">${g.calls}</td>
      <td class="share-cell"><div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div><div style="font-size:0.75em;color:var(--text-dim);margin-top:2px;">${pct}%</div></td>
    </tr>`;
  }
  groupHtml += '</table>';
  // The chart above draws this same series. Wrapping the table in a focusable
  // region gives a keyboard user a reachable data equivalent of the graphic,
  // and gives a wide table a horizontal scroll affordance instead of breaking
  // the narrow layout. The native <table> keeps its implicit role, so no
  // redundant role attribute is added to it.
  setHtmlIfChanged(
    'budget-by-group',
    '<div class="table-scroll" role="region" tabindex="0" '
      + 'aria-label="Spend by phase group, data table">' + groupHtml + '</div>',
  );

  const callerCfg = SORT_TABLES.budgetCaller;
  const callerRows = sortRows(b.by_caller, sortState.budgetCaller, callerCfg.accessors);
  let callerHtml = '<table class="budget-table">'
    + sortableHeaderCells('budgetCaller', callerCfg.columns, sortState.budgetCaller);
  for (const c of callerRows) {
    const pct = (c.share_of_total * 100).toFixed(0);
    // Producing provider(s) for the caller's calls (proposal 067 Step 2) —
    // names who produced the spend, so an unmetered '—' cost row is readable
    // as "unknown by design under cli_paced" rather than silently zero.
    // Pre-attribution rows carry no provider; render the same honest em-dash.
    const providers = (c.providers || []).length ? c.providers.join(', ') : '—';
    callerHtml += `<tr>
      <td>${escapeHtml(c.caller)}</td>
      <td><span class="group-tag">${escapeHtml(c.group)}</span></td>
      <td>${escapeHtml(providers)}</td>
      <td class="num">${formatCost(c.cost_usd, c.unmetered, 3)}</td>
      <td class="num">${c.calls}</td>
      <td class="num">${formatCost(c.reported_cost_usd, c.unmetered, 3)}</td>
      <td class="num">${formatCost(c.estimated_cost_usd, c.unmetered, 3)}</td>
      <td class="share-cell"><div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div><div style="font-size:0.75em;color:var(--text-dim);margin-top:2px;">${pct}%</div></td>
    </tr>`;
  }
  callerHtml += '</table>';
  // Wrap the wide by-caller table in the same focusable scroll region the
  // by-group table uses, so it stays keyboard-reachable and scrolls horizontally
  // instead of overflowing the narrow layout.
  setHtmlIfChanged(
    'budget-by-caller',
    '<div class="table-scroll" role="region" tabindex="0" '
      + 'aria-label="Spend by caller, data table">' + callerHtml + '</div>',
  );
}

async function refreshBudget() {
  try {
    const b = await fetchJSON(`/api/budget?window_hours=${activeBudgetWindow}`);
    // Cache so a sort activation can re-render from data already fetched without
    // re-hitting the API — the pattern renderVersions() uses via lastData.versions.
    lastData.budget = b;
    renderBudget(b);
  } catch (err) {
    console.error('Budget refresh error:', err);
  }
}

async function refresh() {
  try {
    const [st, m, c, d, cr] = await Promise.all([
      fetchJSON('/api/status'),
      fetchJSON('/api/metrics'),
      fetchJSON('/api/consciousness'),
      fetchJSON('/api/diagnostics'),
      fetchJSON('/api/cost-rates'),
    ]);
    if (st && st.limits) serverLimits = st.limits;
    renderStatus(st);
    renderMetrics(m, cr);
    renderIdentity(c);
    renderDiagnostics(d, cr);
    await Promise.all([
      refreshPlans(),
      refreshJournal(),
      refreshTasks(),
      refreshMessages().catch(() => {}),
      refreshBudget(),
      refreshVersions(),
      refreshPublications(),
      refreshArtifactReviews(),
    ]);
  } catch (err) {
    console.error('Refresh error:', err);
  }
}

// Append a toast into the persistent #toast-region live region so the
// role="status" / aria-live="polite" pair announces it. Falls back to a no-op
// if the region is absent. Preserves the 3 s visible + 0.5 s fade dismissal.
function showToast(msg, type) {
  const region = document.getElementById('toast-region');
  if (!region) return;
  const t = document.createElement('div');
  const tone = type === 'success' ? 'success' : type === 'error' ? 'error' : 'info';
  t.className = `toast toast-${tone}`;
  t.textContent = msg;
  region.appendChild(t);
  setTimeout(() => { t.classList.add('toast-fade-out'); setTimeout(() => t.remove(), 500); }, 3000);
}

// Reusable accessible confirmation dialog (proposal 122). Populates the shared
// <dialog id="confirm-dialog">, shows the reason textarea only when withReason,
// and resolves {confirmed, reason}. The native <dialog> supplies the focus trap,
// Escape-to-cancel, and focus restoration to the trigger; the close event reads a
// confirm flag so Escape / Cancel / backdrop-click all resolve confirmed:false.
function openConfirmDialog({ title, message, confirmLabel = 'Confirm', tone = 'default', withReason = false }) {
  return new Promise((resolve) => {
    const dialog = document.getElementById('confirm-dialog');
    const titleEl = document.getElementById('confirm-dialog-title');
    const messageEl = document.getElementById('confirm-dialog-message');
    const reasonEl = document.getElementById('confirm-dialog-reason');
    const confirmBtn = document.getElementById('confirm-dialog-confirm');
    const cancelBtn = document.getElementById('confirm-dialog-cancel');

    titleEl.textContent = title;
    messageEl.textContent = message;
    confirmBtn.textContent = confirmLabel;
    confirmBtn.className = `btn ${tone === 'reject' ? 'btn-reject' : 'btn-approve'}`;
    reasonEl.hidden = !withReason;
    if (withReason) reasonEl.value = '';

    let confirmed = false;
    let pressedOnDialog = false;
    const onConfirm = () => { confirmed = true; dialog.close(); };
    const onCancel = () => { dialog.close(); };
    // A click whose target is the <dialog> itself is either the ::backdrop
    // (outside the element's box) or the dialog's own padding (inside it, when
    // the pointer misses every child). Only a genuine outside-the-box click
    // should cancel, so compare the pointer against the bounding rect — a click
    // on the padding must not dismiss the dialog. A `click` also fires on the
    // dialog when the press *begins* on a child (e.g. drag-selecting the reason
    // textarea) and the release lands outside the box: the click's target is
    // still the dialog and its release coordinates fall on the backdrop. Require
    // the press to have started on the dialog element itself before treating the
    // click as a backdrop dismissal, so a drag-out cannot discard a typed reason.
    const onPointerDown = (e) => { pressedOnDialog = e.target === dialog; };
    const onBackdrop = (e) => {
      if (e.target !== dialog || !pressedOnDialog) return;
      const rect = dialog.getBoundingClientRect();
      const inside = e.clientX >= rect.left && e.clientX <= rect.right &&
                     e.clientY >= rect.top && e.clientY <= rect.bottom;
      if (!inside) dialog.close();
    };
    const onClose = () => {
      confirmBtn.removeEventListener('click', onConfirm);
      cancelBtn.removeEventListener('click', onCancel);
      dialog.removeEventListener('pointerdown', onPointerDown);
      dialog.removeEventListener('click', onBackdrop);
      resolve({ confirmed, reason: withReason ? reasonEl.value.trim() : '' });
    };

    confirmBtn.addEventListener('click', onConfirm);
    cancelBtn.addEventListener('click', onCancel);
    dialog.addEventListener('pointerdown', onPointerDown);
    dialog.addEventListener('click', onBackdrop);
    dialog.addEventListener('close', onClose, { once: true });

    // A re-entrant open — e.g. a fast double-click on an action button before
    // setButtonsDisabled() has disabled the trigger — reaches showModal() with the
    // dialog already open, which throws InvalidStateError. Catch it and resolve the
    // duplicate call as a plain cancel via onClose(), so it never surfaces as an
    // unhandled promise rejection nor silently drops a governance action mid-flow;
    // the first dialog stays live for the user to confirm or cancel.
    try {
      dialog.showModal();
    } catch {
      onClose();
      return;
    }
    // Default focus to the least-destructive control.
    cancelBtn.focus();
  });
}

function setButtonsDisabled(planId, disabled) {
  document.querySelectorAll(`[data-plan-id="${planId}"]`).forEach(btn => {
    btn.disabled = disabled;
    if (disabled) { btn.dataset.origText = btn.textContent; btn.textContent = 'Processing...'; btn.style.opacity = '0.5'; }
    else { btn.textContent = btn.dataset.origText || btn.textContent; btn.style.opacity = '1'; }
  });
}

async function approvePlan(planId, contentHash) {
  const { confirmed } = await openConfirmDialog({
    title: 'Approve evolution plan',
    message: 'Approve this evolution plan? Implementation will start immediately.',
    confirmLabel: 'Approve',
    tone: 'approve',
  });
  if (!confirmed) return;
  setButtonsDisabled(planId, true);
  try {
    const r = await fetch(`/api/plans/${planId}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
      body: JSON.stringify({ content_hash: contentHash || '' }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(`Approval failed: ${err.error || r.statusText}`, 'error');
      setButtonsDisabled(planId, false);
      return;
    }
    showToast('Plan approved — implementation starting...', 'success');
  } catch (e) {
    showToast(`Network error: ${e.message}`, 'error');
    setButtonsDisabled(planId, false);
    return;
  }
  refresh();
}

async function rejectPlan(planId, contentHash) {
  const { confirmed, reason: rawReason } = await openConfirmDialog({
    title: 'Reject evolution plan',
    message: 'Reject this evolution plan?',
    confirmLabel: 'Reject',
    tone: 'reject',
    withReason: true,
  });
  if (!confirmed) return;
  const reason = rawReason || 'No reason provided';
  setButtonsDisabled(planId, true);
  try {
    const r = await fetch(`/api/plans/${planId}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
      body: JSON.stringify({ reason, content_hash: contentHash || '' }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(`Rejection failed: ${err.error || r.statusText}`, 'error');
      setButtonsDisabled(planId, false);
      return;
    }
    showToast('Plan rejected — learning from feedback.', 'info');
  } catch (e) {
    showToast(`Network error: ${e.message}`, 'error');
    setButtonsDisabled(planId, false);
    return;
  }
  refresh();
}

async function requestPlanChanges(planId, contentHash) {
  const { confirmed, reason: text } = await openConfirmDialog({
    title: 'Request changes',
    message: 'Describe what Maestro should revise in this plan.',
    confirmLabel: 'Send feedback',
    withReason: true,
  });
  if (!confirmed || !text) return;
  setButtonsDisabled(planId, true);
  try {
    const r = await fetch(`/api/plans/${planId}/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
      body: JSON.stringify({ text, content_hash: contentHash || '' }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showToast(`Feedback failed: ${err.error || r.statusText}`, 'error');
      setButtonsDisabled(planId, false);
      return;
    }
    showToast('Feedback queued — Maestro will revise the plan.', 'success');
  } catch (e) {
    showToast(`Network error: ${e.message}`, 'error');
    setButtonsDisabled(planId, false);
    return;
  }
  refresh();
}

async function refreshMessages() {
  try {
    const r = await fetch(withPageSize('/api/messages', 50, 'max_page_size'));
    if (r.status === 401) { window.location.href = '/login'; return; }
    if (!r.ok) return;
    const msgs = await r.json();
    if (!msgs.length) {
      setHtmlIfChanged('messages', '<div class="empty">No messages yet — say hello!</div>');
      return;
    }
    const html = msgs.map(m => {
      const who = m.sender === 'thomas' ? 'thomas' : 'maestro';
      const label = m.sender === 'thomas' ? 'You' : 'Maestro';
      const time = (m.timestamp || '').replace('T', ' ').split('.')[0];
      // A reply threads back to the question via ``reply_to`` so an answer to a
      // blocked plan's question re-admits or abandons that plan (#4154).
      const reply = (m.sender === 'maestro' && m.message_type === 'question')
        ? ` · <button type="button" class="message-reply" data-reply-to="${escapeHtml(m.message_id)}">Reply</button>`
        : '';
      return `
        <div class="message-bubble ${who}">
          ${escapeHtml(m.content)}
          <div class="message-meta">${escapeHtml(label)} · ${escapeHtml(time)}${reply}</div>
        </div>`;
    }).join('');
    setHtmlIfChanged('messages', html);
    // Auto-stick to newest only when the user is already near the bottom — don't
    // yank them out of older messages they're reading.
    const container = document.getElementById('messages');
    if (container.scrollTop + container.clientHeight >= container.scrollHeight - 80) {
      container.scrollTop = container.scrollHeight;
    }
  } catch (err) {
    console.error('Message refresh error:', err);
  }
}

// The question ``message_id`` the next send answers, or null for an instruction.
let pendingReplyTo = null;

function setReplyTarget(messageId) {
  pendingReplyTo = messageId;
  const bar = document.getElementById('compose-reply');
  bar.hidden = messageId === null;
  document.getElementById('compose-reply-id').textContent = messageId || '';
}

async function sendMessage(content, replyTo = null) {
  const body = replyTo
    ? { content, message_type: 'answer', reply_to: replyTo }
    : { content, message_type: 'instruction' };
  const r = await fetch('/api/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || r.statusText);
  }
}

let activeLogLevel = '';
let activeLogSource = '';

// Filter button groups: clicking sets state and re-renders. Each entry maps a
// data-attribute to the side effect of selecting one of its buttons.
const FILTER_GROUPS = [
  { attr: 'data-log-source',          apply: v => { activeLogSource = v; refreshLogs(); } },
  { attr: 'data-log-level',           apply: v => { activeLogLevel = v;  refreshLogs(); } },
  { attr: 'data-plan-status',         apply: v => { filterState.planStatus  = v; renderAllPlans(); } },
  { attr: 'data-publication-status',  apply: v => { filterState.publicationStatus = v; renderAllPublications(); } },
  { attr: 'data-journal-source',      apply: v => { filterState.journalSource = v; refreshJournal(); } },
  { attr: 'data-journal-type',        apply: v => { filterState.journalType = v; renderJournal();  } },
  { attr: 'data-task-outcome',        apply: v => { filterState.taskOutcome = v; renderTasks();    } },
  { attr: 'data-budget-window',       apply: v => { activeBudgetWindow = v; refreshBudget(); } },
  { attr: 'data-sdk-phase',           apply: v => { filterState.sdkPhase = v; loadSdkSessions(); } },
];

// Search inputs: each input id maps a filterState field plus its renderer.
const SEARCH_INPUTS = {
  'plan-search':    { key: 'planSearch',    render: renderAllPlans },
  'journal-search': { key: 'journalSearch', render: renderJournal },
  'task-search':    { key: 'taskSearch',    render: renderTasks },
};

document.addEventListener('click', (e) => {
  const replyBtn = e.target.closest('.message-reply');
  if (replyBtn) {
    setReplyTarget(replyBtn.getAttribute('data-reply-to'));
    document.getElementById('compose-content').focus();
    return;
  }
  if (e.target.closest('#compose-reply-clear')) {
    setReplyTarget(null);
    return;
  }
  for (const group of FILTER_GROUPS) {
    const btn = e.target.closest(`[${group.attr}]`);
    if (btn) {
      document.querySelectorAll(`[${group.attr}]`).forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      group.apply(btn.getAttribute(group.attr));
      return;
    }
  }
  const sortBtn = e.target.closest('.sort-btn');
  if (sortBtn) {
    const tableKey = sortBtn.dataset.sortTable;
    const colKey = sortBtn.dataset.sortKey;
    const state = sortState[tableKey];
    const cfg = SORT_TABLES[tableKey];
    if (state && cfg) {
      if (state.key === colKey) {
        // Re-activating the active column flips its direction.
        state.dir = state.dir === 'asc' ? 'desc' : 'asc';
      } else {
        // A new column adopts a type-appropriate default: descending for numbers
        // (largest first), ascending for text/date.
        const col = cfg.columns.find((c) => c.key === colKey);
        state.key = colKey;
        state.dir = col && col.type === 'number' ? 'desc' : 'asc';
      }
      if (tableKey === 'versions') renderVersions();
      else renderBudget(lastData.budget);
      // The innerHTML swap replaced the clicked button; move focus onto the
      // freshly-rendered one so keyboard operation is not interrupted.
      const fresh = document.querySelector(
        `.sort-btn[data-sort-table="${tableKey}"][data-sort-key="${colKey}"]`,
      );
      if (fresh) fresh.focus();
    }
    return;
  }
  if (e.target.id === 'journal-load-more') {
    pageSize.journal = Math.min(200, pageSize.journal * 2);
    refreshJournal();
    return;
  }
  if (e.target.id === 'tasks-load-more') {
    pageSize.tasks = Math.min(200, pageSize.tasks * 2);
    refreshTasks();
    return;
  }
  if (e.target.id === 'pending-load-more') {
    pendingShown += PENDING_PAGE_SIZE;
    refreshPlans();
    return;
  }
  if (e.target.id === 'btn-sdk-follow') {
    sdkFollow = !sdkFollow;
    e.target.classList.toggle('active', sdkFollow);
    e.target.setAttribute('aria-pressed', sdkFollow ? 'true' : 'false');
    e.target.textContent = sdkFollow ? 'Following' : 'Follow';
    if (sdkFollow) loadSdkSessions();
    else setSdkStatus(sdkStatusText());
    return;
  }
  const sessionItem = e.target.closest('.sdk-session-item');
  if (sessionItem) {
    // Picking a session by hand is an explicit "stop tailing": otherwise the
    // next poll would yank the pane straight back to the newest transcript.
    sdkFollow = false;
    const followBtn = document.getElementById('btn-sdk-follow');
    if (followBtn) {
      followBtn.classList.remove('active');
      followBtn.setAttribute('aria-pressed', 'false');
      followBtn.textContent = 'Follow';
    }
    sdkSelected = { date: sessionItem.dataset.date, filename: sessionItem.dataset.filename };
    sdkDetailNotice = SDK_NO_SELECTION;
    renderSdkSessions();
    setSdkStatus(sdkStatusText());
    loadSdkSessionDetail(sdkSelected.date, sdkSelected.filename, sdkStamp(sdkSummaryFor(sdkSelected)));
    return;
  }
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const planId = btn.dataset.planId;
  const proposalId = btn.dataset.proposalId;
  if (btn.dataset.action === 'approve') approvePlan(planId, btn.dataset.contentHash);
  else if (btn.dataset.action === 'reject') rejectPlan(planId, btn.dataset.contentHash);
  else if (btn.dataset.action === 'feedback') requestPlanChanges(planId, btn.dataset.contentHash);
  else if (btn.dataset.action === 'pub-approve') approvePublication(proposalId);
  else if (btn.dataset.action === 'pub-reject') rejectPublication(proposalId);
});

document.addEventListener('input', (e) => {
  const cfg = SEARCH_INPUTS[e.target.id];
  if (cfg) {
    filterState[cfg.key] = e.target.value;
    cfg.render();
  } else if (e.target.id === 'log-search') {
    refreshLogs();
  }
});

document.addEventListener('submit', async (e) => {
  if (e.target.id !== 'compose-form') return;
  e.preventDefault();
  const ta = document.getElementById('compose-content');
  const btn = document.getElementById('compose-send');
  const content = ta.value.trim();
  if (!content) return;
  btn.disabled = true;
  try {
    await sendMessage(content, pendingReplyTo);
    ta.value = '';
    setReplyTarget(null);
    showToast('Message sent', 'success');
    await refreshMessages();
  } catch (err) {
    showToast(`Send failed: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
});

const DEFAULT_TAB = 'identity';
const VALID_TABS = new Set(['diagnostics','identity','plans','publications','journal','tasks','messages','budget','versions','sessions','logs']);

// Proposal 122 v2 navigation shell. Each entry groups a subset of the twelve
// panels under a labelled cluster; the union is every VALID_TABS name, in the
// visual order the grouped sidebar rail renders.
const NAV_GROUPS = [
  { label: 'Monitor', items: ['diagnostics', 'budget', 'sessions', 'logs'] },
  { label: 'Govern', items: ['plans', 'publications', 'messages'] },
  { label: 'Introspect', items: ['identity', 'journal', 'tasks'] },
  { label: 'System', items: ['versions'] },
];

// Hand-built inline SVG icons (Phosphor-style line glyphs) matching the
// classed-and-CSS-styled convention of renderGroupChart — no webfont, no
// third-party fetch. currentColor lets each icon inherit its nav-link colour;
// aria-hidden keeps it decorative so AT reads only the text label.
function navIcon(inner) {
  return '<svg class="nav-icon" viewBox="0 0 16 16" fill="none" '
    + 'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" '
    + 'stroke-linejoin="round" aria-hidden="true">' + inner + '</svg>';
}

const NAV_ICONS = {
  diagnostics: navIcon('<polyline points="1 8 4.5 8 6.5 3.5 9.5 12.5 11.5 8 15 8"/>'),
  identity: navIcon('<circle cx="8" cy="5.5" r="2.75"/><path d="M2.75 14.5a5.25 5.25 0 0 1 10.5 0"/>'),
  plans: navIcon('<rect x="3.25" y="2.5" width="9.5" height="11.5" rx="1.25"/><line x1="5.75" y1="6" x2="10.25" y2="6"/><line x1="5.75" y1="8.5" x2="10.25" y2="8.5"/><line x1="5.75" y1="11" x2="8.5" y2="11"/>'),
  publications: navIcon('<circle cx="8" cy="8" r="6"/><line x1="2" y1="8" x2="14" y2="8"/><ellipse cx="8" cy="8" rx="3" ry="6"/>'),
  journal: navIcon('<rect x="3.5" y="2.5" width="9" height="11" rx="1"/><line x1="6" y1="2.5" x2="6" y2="13.5"/>'),
  tasks: navIcon('<polyline points="2.5 5 4 6.5 6.5 3.5"/><line x1="8.5" y1="5" x2="13.5" y2="5"/><polyline points="2.5 10.5 4 12 6.5 9"/><line x1="8.5" y1="11" x2="13.5" y2="11"/>'),
  messages: navIcon('<path d="M2.5 4.5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H6l-3.5 3v-8z"/>'),
  budget: navIcon('<line x1="2.5" y1="13.5" x2="13.5" y2="13.5"/><rect x="3.5" y="8" width="2.5" height="5.5"/><rect x="7" y="5" width="2.5" height="8.5"/><rect x="10.5" y="9.5" width="2.5" height="4"/>'),
  versions: navIcon('<path d="M2.5 2.5h5l6 6-5 5-6-6z"/><circle cx="5" cy="5" r="0.9"/>'),
  sessions: navIcon('<path d="M2 8c1.5-3.5 3-3.5 4.5 0s3 3.5 4.5 0 3-3.5 3 0"/>'),
  logs: navIcon('<line x1="2.5" y1="4" x2="13.5" y2="4"/><line x1="2.5" y1="8" x2="13.5" y2="8"/><line x1="2.5" y1="12" x2="10" y2="12"/>'),
};

// Progressive enhancement, gated on the server-injected body marker. A no-op
// when the marker is absent, so the flat pill nav is untouched when the flag is
// off. Turns the flat anchor row into an ARIA-correct grouped tablist with a
// skip-link, a role="main" focus target, per-tab icons, and roving-tabindex
// arrow traversal.
function enhanceNavV2() {
  if (!document.body.dataset.accountUiV2) return;
  const nav = document.getElementById('nav-bar');
  if (!nav || nav.dataset.enhanced) return;
  nav.dataset.enhanced = 'true';

  // Skip-to-content link as the very first focusable element.
  const skip = document.createElement('a');
  skip.className = 'skip-link';
  skip.href = '#main-content';
  skip.textContent = 'Skip to main content';
  document.body.insertBefore(skip, document.body.firstChild);

  // Wrap every panel in a role="main" focus target the skip-link jumps to.
  const panels = Array.from(document.querySelectorAll('.tab-panel'));
  const main = document.createElement('main');
  main.id = 'main-content';
  main.setAttribute('role', 'main');
  main.setAttribute('tabindex', '-1');
  if (panels.length) {
    panels[0].parentNode.insertBefore(main, panels[0]);
    panels.forEach(p => main.appendChild(p));
  }

  // Activate the skip-link by moving focus to the main region directly rather
  // than letting the browser navigate to #main-content: that foreign hash would
  // fire hashchange, and currentTabFromHash() maps it to DEFAULT_TAB, silently
  // resetting the active tab to Identity. preventDefault keeps the current tab
  // intact while still delivering the a11y focus jump for both click and Enter.
  skip.addEventListener('click', (e) => {
    e.preventDefault();
    main.focus();
  });

  // Two-column shell: the nav rail beside the main content column. Only build it
  // when the main region is actually attached (panels present) — with zero
  // panels main is detached, main.parentNode is null, and both the insertBefore
  // dereference and appending nav into a detached shell would break the nav
  // rather than enhance it. Matches the defensive guards on every other lookup.
  if (main.parentNode) {
    const shell = document.createElement('div');
    shell.className = 'account-shell';
    main.parentNode.insertBefore(shell, main);
    shell.appendChild(nav);
    shell.appendChild(main);
  }

  // tablist / tab roles + per-tab icon.
  nav.setAttribute('role', 'tablist');
  nav.setAttribute('aria-label', 'Dashboard sections');
  // aria-orientation must track the rendered axis: the rail is vertical at the
  // >=1024px breakpoint and collapses to a horizontal strip below it (mirrors
  // the CSS media queries). Reflect the live match and keep it in sync as the
  // viewport crosses the breakpoint.
  const railQuery = window.matchMedia('(min-width: 1024px)');
  const syncOrientation = () => {
    nav.setAttribute('aria-orientation', railQuery.matches ? 'vertical' : 'horizontal');
  };
  syncOrientation();
  railQuery.addEventListener('change', syncOrientation);
  document.querySelectorAll('.nav-link[data-nav]').forEach(link => {
    const name = link.dataset.nav;
    link.setAttribute('role', 'tab');
    if (!link.id) link.id = `nav-tab-${name}`;
    link.setAttribute('aria-controls', `panel-${name}`);
    link.setAttribute('aria-selected', link.classList.contains('active') ? 'true' : 'false');
    if (NAV_ICONS[name]) link.insertAdjacentHTML('afterbegin', NAV_ICONS[name]);
  });

  // tabpanel roles, labelled by their owning tab.
  panels.forEach(panel => {
    const name = panel.dataset.tab;
    panel.id = `panel-${name}`;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('tabindex', '-1');
    const tab = nav.querySelector(`.nav-link[data-nav="${name}"]`);
    if (tab) panel.setAttribute('aria-labelledby', tab.id);
  });

  // Group the anchors into labelled clusters. The wrappers are role=presentation
  // so the tablist still owns the tabs directly for assistive tech; the visible
  // group label is aria-hidden decoration.
  NAV_GROUPS.forEach(group => {
    const wrap = document.createElement('div');
    wrap.className = 'nav-group';
    wrap.setAttribute('role', 'presentation');
    const label = document.createElement('div');
    label.className = 'nav-group-label';
    label.textContent = group.label;
    label.setAttribute('aria-hidden', 'true');
    wrap.appendChild(label);
    group.items.forEach(name => {
      const link = nav.querySelector(`.nav-link[data-nav="${name}"]`);
      if (link) wrap.appendChild(link);
    });
    nav.appendChild(wrap);
  });

  // Roving-tabindex arrow traversal across the tablist (manual activation:
  // arrows move focus, Enter follows the anchor href to activate).
  nav.addEventListener('keydown', (e) => {
    const order = Array.from(nav.querySelectorAll('.nav-link[data-nav]'));
    const idx = order.indexOf(document.activeElement);
    if (idx === -1) return;
    let next = -1;
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') next = (idx + 1) % order.length;
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') next = (idx - 1 + order.length) % order.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = order.length - 1;
    else return;
    e.preventDefault();
    const target = order[next];
    order.forEach(l => l.setAttribute('tabindex', l === target ? '0' : '-1'));
    target.focus();
  });
}

function showTab(name, { moveFocus = false } = {}) {
  if (!VALID_TABS.has(name)) name = DEFAULT_TAB;
  const navV2 = !!document.body.dataset.accountUiV2;
  let activePanel = null;
  document.querySelectorAll('.tab-panel').forEach(p => {
    const on = p.dataset.tab === name;
    p.classList.toggle('active', on);
    if (on) activePanel = p;
  });
  document.querySelectorAll('.nav-link[data-nav]').forEach(l => {
    const on = l.dataset.nav === name;
    l.classList.toggle('active', on);
    if (navV2) {
      l.setAttribute('aria-selected', on ? 'true' : 'false');
      l.setAttribute('tabindex', on ? '0' : '-1');
      if (on) l.setAttribute('aria-current', 'page');
      else l.removeAttribute('aria-current');
    }
  });
  // v2 moves focus onto the activated panel, but only on explicit user
  // activation (click / Enter / arrow); the initial hash-driven call passes
  // moveFocus=false to avoid yanking focus on load.
  if (navV2 && moveFocus && activePanel) {
    activePanel.focus();
  } else {
    window.scrollTo(0, 0);
  }
  // The transcript view is poll-driven and only ticks while its tab is on
  // screen, so activating it must fetch immediately rather than leave the
  // operator staring at an empty pane until the next tick.
  if (name === 'sessions') loadSdkSessions();
}

function currentTabFromHash() {
  const h = (location.hash || '').replace(/^#section-/, '');
  return VALID_TABS.has(h) ? h : DEFAULT_TAB;
}

function initTabs() {
  enhanceNavV2();
  showTab(currentTabFromHash());
  window.addEventListener('hashchange', () => showTab(currentTabFromHash(), { moveFocus: true }));
}

async function refreshLogs() {
  try {
    const logsBase = withPageSize('/api/logs', 200, 'log_buffer_size');
    const sep = logsBase.includes('?') ? '&' : '?';
    const params = [];
    if (activeLogLevel) params.push(`level=${activeLogLevel}`);
    if (activeLogSource) params.push(`source=${activeLogSource}`);
    const url = params.length ? `${logsBase}${sep}${params.join('&')}` : logsBase;
    const filtersActive = activeLogLevel || activeLogSource;
    // Badge always reflects unfiltered errors so level/source filters and the
    // search box can never mask the true error state.
    const [unfiltered, filteredFetch] = await Promise.all([
      fetchJSON(logsBase),
      filtersActive ? fetchJSON(url) : Promise.resolve(null),
    ]);
    const errorCount = unfiltered.filter(e => e.level === 'ERROR' || e.level === 'CRITICAL').length;
    setBadge('nav-badge-logs', errorCount);
    const fetched = filteredFetch ?? unfiltered;

    const searchInput = document.getElementById('log-search');
    const searchVal = searchInput ? searchInput.value.toLowerCase() : '';
    const entries = searchVal
      ? fetched.filter(e => (e.message || '').toLowerCase().includes(searchVal))
      : fetched;

    const container = document.getElementById('logs');
    const wasAtBottom = container.scrollTop + container.clientHeight >= container.scrollHeight - 20;
    const html = entries.length
      ? entries.map(e => {
          const time = e.timestamp.split('T')[1]?.split('+')[0] || '';
          const isSdk = e.source === 'sdk';
          return `<div class="log-entry"><span class="log-time">${escapeHtml(time)}</span><span class="log-level ${escapeHtml(e.level)}">${escapeHtml(e.level)}</span> <span class="log-msg${isSdk ? ' SDK' : ''}">${escapeHtml(e.message)}</span></div>`;
        }).join('')
      : '<div class="empty">No log entries yet.</div>';
    if (container.innerHTML !== html) {
      container.innerHTML = html;
      if (wasAtBottom) container.scrollTop = container.scrollHeight;
    }
  } catch (err) {
    console.error('Log refresh error:', err);
  }
}

// Sessions tab state. `sdkSessionsCache` holds the summaries the list pane
// renders; `sdkSelected` is the transcript shown in the right pane; `sdkFollow`
// makes the view tail the live session — every poll re-selects the newest
// session and pins the transcript to its foot, so the operator watches Maestro
// think without touching anything. A phase-filter change re-fetches rather than
// re-rendering the cache, because narrowing the list also moves the head that
// following targets.
let sdkSessionsCache = [];
let sdkSelected = null;
let sdkFollow = true;
let sdkDetailToken = 0;
// Signatures of what each pane currently shows, so a tick that changed nothing
// repaints nothing. Both matter: an unconditional innerHTML rebuild resets the
// list's scroll and wipes any transcript text the operator is selecting, and
// the detail endpoint reads the whole sidecar, so re-fetching an unchanged
// transcript every 5s costs a full read+parse+serialize in the loop container.
let sdkListSignature = '';
let sdkDetailStamp = '';

const SDK_NO_SELECTION = 'Select a session to view its transcript.';
const SDK_GONE = 'That session is no longer available.';
// What the transcript pane says while nothing is selected. It is sticky: a
// session that vanished mid-watch keeps saying so until the operator picks
// another one (or follow finds a live session), rather than being quietly
// replaced by the generic prompt on the next 5s tick.
let sdkDetailNotice = SDK_NO_SELECTION;

// Identity of a summary/selection, used to tell "same transcript, new turns"
// (re-render in place, keep scroll) from "different transcript" (reset scroll).
function sdkKey(s) {
  return s ? `${s.date}/${s.filename}` : '';
}

// Identity *plus* growth: the summary fields that change exactly when the
// transcript gained turns. Unknown summary (aged out of the list) stamps '',
// which never compares equal to a painted stamp, so it always re-fetches.
function sdkStamp(s) {
  return s ? `${sdkKey(s)}:${s.size_bytes}:${s.turn_count}` : '';
}

function sdkSummaryFor(sel) {
  const key = sdkKey(sel);
  return key ? sdkSessionsCache.find(s => sdkKey(s) === key) : undefined;
}

function setSdkStatus(text) {
  const el = document.getElementById('sdk-session-status');
  if (el) el.textContent = text;
}

// The status line describes the tail, not the poll: the list and the selected
// transcript keep refreshing while paused, so the wording must not imply
// fetching stopped — only that the view no longer re-targets the newest session.
function sdkStatusText() {
  if (sdkFollow) return sdkSelected ? 'Following live session' : 'Following — no sessions yet';
  return sdkSelected ? `Not following — pinned to ${sdkSelected.filename}` : 'Not following — no session selected';
}

// Empty / unavailable state for the transcript pane, mirroring the list pane's
// own empty state so the tab never shows a bare bordered box.
function setSdkDetailPlaceholder(text) {
  const el = document.getElementById('sdk-session-content');
  if (!el) return;
  sdkDetailStamp = '';
  el.dataset.sdkKey = '';
  el.classList.add('visible', 'typed');
  el.innerHTML = `<div class="empty">${escapeHtml(text)}</div>`;
}

// The sessions tab polls on its own cadence, but only while it is the active
// tab: an operator sitting on Plans should not pay for transcript fetches.
function sessionsTabActive() {
  const panel = document.querySelector('.tab-panel[data-tab="sessions"]');
  return !!panel && panel.classList.contains('active');
}

async function loadSdkSessions() {
  try {
    sdkSessionsCache = await fetchJSON('/api/logs/sdk-sessions');
  } catch (err) {
    console.error('SDK sessions error:', err);
    setSdkStatus('Could not load sessions');
    return;
  }
  // Following means the newest visible session is always the selected one, so a
  // session that starts while the operator watches takes over the right pane.
  // An empty list clears the selection instead of re-fetching a stale pick.
  const visible = sdkVisibleSessions();
  if (sdkFollow && visible.length) {
    sdkSelected = { date: visible[0].date, filename: visible[0].filename };
    sdkDetailNotice = SDK_NO_SELECTION;
  } else if (sdkFollow) {
    sdkSelected = null;
  }
  renderSdkSessions();
  if (!sdkSelected) {
    setSdkDetailPlaceholder(sdkDetailNotice);
    setSdkStatus(sdkDetailNotice === SDK_GONE ? SDK_GONE : sdkStatusText());
    return;
  }
  // Skip the detail fetch when the summary says the transcript has not grown.
  const stamp = sdkStamp(sdkSummaryFor(sdkSelected));
  if (stamp !== sdkDetailStamp) {
    const ok = await loadSdkSessionDetail(sdkSelected.date, sdkSelected.filename, stamp);
    // A failed load already wrote its own status; never stamp it "live".
    if (!ok) return;
  }
  setSdkStatus(sdkStatusText());
}

// One poll tick: a no-op unless the tab is on screen.
function tickSdkSessions() {
  if (!sessionsTabActive()) return;
  loadSdkSessions();
}

// The phase filter narrows the list; "All" (empty string) shows everything.
// A stale filterState.sdkPhase whose phase has aged out of the window would
// strand the list blank, so an unmatched phase falls back to "show all".
function sdkActivePhase() {
  const phases = new Set(sdkSessionsCache.map(s => s.phase).filter(p => p));
  return phases.has(filterState.sdkPhase) ? filterState.sdkPhase : '';
}

function sdkVisibleSessions() {
  const phase = sdkActivePhase();
  return phase ? sdkSessionsCache.filter(s => s.phase === phase) : sdkSessionsCache;
}

// Render the cached summaries: a phase-filter row (All + one button per distinct
// non-null phase) plus the attributed session list. With the transcript writer
// flag off every summary carries a null phase, so the phase set is empty, the
// filter row stays blank, and the list degrades to a filename-only view —
// backward-compatible by construction.
function renderSdkSessions() {
  const panel = document.getElementById('sdk-sessions-panel');
  const filters = document.getElementById('sdk-session-filters');
  if (!panel) return;
  const phases = [...new Set(sdkSessionsCache.map(s => s.phase).filter(p => p))].sort();
  const activePhase = sdkActivePhase();
  const visibleSessions = sdkVisibleSessions();
  const selectedKey = sdkKey(sdkSelected);
  // Repaint only when the rendered identity actually changed: rebuilding this
  // pane's innerHTML on every tick would reset its scroll position and wipe
  // any text the operator is selecting.
  const signature = JSON.stringify([phases, activePhase, selectedKey, visibleSessions.map(sdkStamp)]);
  if (signature === sdkListSignature) return;
  sdkListSignature = signature;
  if (!sdkSessionsCache.length) {
    if (filters) filters.innerHTML = '';
    panel.innerHTML = '<div class="empty">No SDK sessions recorded yet.</div>';
    return;
  }
  if (filters) {
    if (phases.length) {
      let fhtml = `<button class="log-filter-btn${activePhase ? '' : ' active'}" data-sdk-phase="">All</button>`;
      for (const p of phases) {
        const active = activePhase === p ? ' active' : '';
        fhtml += `<button class="log-filter-btn${active}" data-sdk-phase="${escapeHtml(p)}">${escapeHtml(p)}</button>`;
      }
      filters.innerHTML = fhtml;
    } else {
      filters.innerHTML = '';
    }
  }
  let lastDate = '';
  let html = '';
  for (const s of visibleSessions) {
    if (s.date !== lastDate) {
      lastDate = s.date;
      html += `<div class="sdk-session-date">${escapeHtml(s.date)}</div>`;
    }
    const label = escapeHtml(s.phase || s.filename);
    const meta = [];
    if (s.session) meta.push(`session ${escapeHtml(s.session)}`);
    if (s.iteration !== null && s.iteration !== undefined) meta.push(`iter ${escapeHtml(String(s.iteration))}`);
    if (s.task) meta.push(`task ${escapeHtml(s.task)}`);
    if (s.turn_count !== null && s.turn_count !== undefined) meta.push(`${escapeHtml(String(s.turn_count))} turns`);
    const metaHtml = meta.length ? `<span class="sdk-session-meta">${meta.join(' · ')}</span>` : '';
    const sel = sdkKey(s) === selectedKey ? ' selected' : '';
    html += `<div class="sdk-session-item${sel}" data-date="${escapeHtml(s.date)}" data-filename="${escapeHtml(s.filename)}">
      <span class="sdk-session-info"><span class="sdk-session-label">${label}</span>${metaHtml}</span>
      <span style="color:var(--text-dim)">${(s.size_bytes / 1024).toFixed(1)} KB</span>
    </div>`;
  }
  panel.innerHTML = html;
}

const SDK_TURN_KINDS = ['prompt', 'thinking', 'text', 'tool_call', 'tool_result', 'event'];

// Build the typed-turn detail HTML for a stamped .jsonl record: a provenance
// header (phase/session/iter/task/started_at) followed by one block per turn,
// each labelled by kind + role. Every field is untrusted (raw prompts, tool
// output) so all of it — provenance, kind, role, content — passes through
// escapeHtml before insertion; the per-kind CSS class uses a validated kind so
// a malformed kind can never break out of the class attribute.
function renderSdkTranscript(data) {
  const header = [];
  if (data.caller) header.push(`<span class="sdk-prov-field">phase: ${escapeHtml(data.caller)}</span>`);
  if (data.session_label) header.push(`<span class="sdk-prov-field">session: ${escapeHtml(data.session_label)}</span>`);
  if (data.iteration !== null && data.iteration !== undefined) header.push(`<span class="sdk-prov-field">iter: ${escapeHtml(String(data.iteration))}</span>`);
  if (data.task_id) header.push(`<span class="sdk-prov-field">task: ${escapeHtml(data.task_id)}</span>`);
  if (data.started_at) header.push(`<span class="sdk-prov-field">${escapeHtml(data.started_at)}</span>`);
  const headerHtml = header.length ? `<div class="sdk-prov-header">${header.join('')}</div>` : '';
  const turnsHtml = data.turns.map(t => {
    const kindClass = SDK_TURN_KINDS.includes(t.kind) ? t.kind : 'event';
    return `<div class="sdk-turn sdk-turn-${kindClass}"><div class="sdk-turn-head"><span class="sdk-turn-kind">${escapeHtml(t.kind)}</span><span class="sdk-turn-role">${escapeHtml(t.role)}</span></div><div class="sdk-turn-content">${escapeHtml(t.content)}</div></div>`;
  }).join('');
  return headerHtml + turnsHtml;
}

async function loadSdkSessionDetail(date, filename, stamp = '') {
  const el = document.getElementById('sdk-session-content');
  if (!el) return false;
  // A poll and a click can be in flight together; only the newest request may
  // paint, so a slow earlier fetch cannot overwrite a freshly clicked session.
  const token = ++sdkDetailToken;
  const sameTranscript = el.dataset.sdkKey === `${date}/${filename}`;
  // Tail semantics: keep the operator's scroll position when the same
  // transcript simply grew, unless they are already parked at the foot (or
  // following), in which case stay pinned to the newest turn.
  const atFoot = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  const prevScroll = el.scrollTop;
  let data;
  try {
    data = await fetchJSON(`/api/logs/sdk-sessions/${encodeURIComponent(date)}/${encodeURIComponent(filename)}`);
  } catch (err) {
    console.error('SDK session detail error:', err);
    if (token !== sdkDetailToken) return false;
    // The session aged out of the retention window, or its file rotated: drop
    // the selection and say so, rather than leaving a dead transcript on screen
    // under a status claiming it is live.
    if (sdkKey(sdkSelected) === `${date}/${filename}`) sdkSelected = null;
    sdkDetailNotice = SDK_GONE;
    setSdkDetailPlaceholder(SDK_GONE);
    renderSdkSessions();
    setSdkStatus(SDK_GONE);
    return false;
  }
  if (token !== sdkDetailToken) return false;
  el.classList.add('visible');
  el.dataset.sdkKey = `${date}/${filename}`;
  if (data.legacy || !Array.isArray(data.turns)) {
    // Legacy .log (or an unreadable / malformed sidecar): keep the raw-blob
    // fallback — textContent, so nothing in the blob is interpreted as HTML.
    el.classList.remove('typed');
    el.textContent = data.content;
  } else {
    el.classList.add('typed');
    el.innerHTML = renderSdkTranscript(data);
  }
  if (!sameTranscript) el.scrollTop = 0;
  else if (sdkFollow || atFoot) el.scrollTop = el.scrollHeight;
  else el.scrollTop = prevScroll;
  sdkDetailStamp = stamp;
  return true;
}

// Sign-out control (proposal 122). The control POSTs to the CSRF-exempt /logout
// route, which is only registered when auth is enabled. Reveal it only when the
// JS-readable maestro_csrf cookie is present — reusing csrfHeaders()' presence
// inference — so it never shows, and never 404s to a missing /logout, in the
// no-auth local-dev configuration.
function revealSignOut() {
  const form = document.querySelector('.sign-out');
  if (form && csrfHeaders()['X-CSRF-Token']) form.hidden = false;
}

// Sticky-header clearance (proposal 122). Step 8's acceptance asks that a focus
// target is "not obscured by the sticky header", but the header's height is
// content-dependent — h1 + subtitle + the Status/Metrics cards, plus the nav row
// in the flat layout — and it grows again once the skeletons are replaced by
// real content, so any fixed scroll-margin-top under-clears it. Measure the
// element that actually pins and publish it as --header-offset for the
// scroll-margin-top rule in index.css to consume.
function syncHeaderOffset() {
  const header = document.querySelector('.sticky-header');
  if (!header) return;
  // enhanceNavV2() has already run by now and only *moves* #nav-bar into the
  // rail, never replaces it, so this one lookup stays valid for the page's life.
  const nav = document.getElementById('nav-bar');
  const measure = () => {
    // Below 640px .sticky-header is display:contents, so it has no box and
    // offsetHeight is 0 — there .nav-bar is what pins. Measure whichever element
    // is really obstructing the top of the viewport.
    const pinned = header.offsetHeight ? header : nav;
    const height = pinned ? pinned.offsetHeight : 0;
    // Nothing measurable pins: *clear* the property rather than writing '0px'.
    // A written '0px' is a defined value, so the `var(--header-offset, 6rem)`
    // fallback the CSS documents would never apply and scroll-margin-top would
    // collapse to the --space-2 gap alone. Removing it restores that fallback.
    if (height > 0) {
      document.documentElement.style.setProperty('--header-offset', `${height}px`);
    } else {
      document.documentElement.style.removeProperty('--header-offset');
    }
  };
  measure();
  // --header-offset only feeds scroll-margin-top, which never affects layout, so
  // writing it back from a resize callback cannot feed a resize loop.
  const observer = new ResizeObserver(measure);
  observer.observe(header);
  if (nav) observer.observe(nav);
  // The display:contents header reports no box changes, so the breakpoint
  // crossing itself is picked up from the viewport instead.
  window.addEventListener('resize', measure);
}

refresh();
refreshLogs();
initTabs();
revealSignOut();
syncHeaderOffset();
setInterval(refresh, 10000);
setInterval(refreshLogs, 3000);
setInterval(tickSdkSessions, 5000);
