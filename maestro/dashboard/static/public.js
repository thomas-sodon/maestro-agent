const PHASE_META = {
  ensure_main_branch:            { label: "Tending branches",      pattern: "housekeeping" },
  check_contributions:           { label: "Listening for commits", pattern: "outward" },
  process_messages:              { label: "Reading messages",      pattern: "inward" },
  tend_evolution_prs:            { label: "Tending PRs",           pattern: "housekeeping" },
  attend:                        { label: "Selecting context",     pattern: "inward" },
  contemplate:                   { label: "Contemplating",         pattern: "inward" },
  respond_to_messages:           { label: "Responding",            pattern: "outward" },
  implement_approved_plans:      { label: "Building",              pattern: "structured" },
  check_merged_plans:            { label: "Verifying merges",      pattern: "housekeeping" },
  review_artifacts:              { label: "Reviewing artifacts",   pattern: "inward" },
  execute_approved_publications: { label: "Publishing",            pattern: "outward" },
  process_rejected_plans:        { label: "Reviewing rejections",  pattern: "housekeeping" },
  generate_task:                 { label: "Imagining next task",   pattern: "structured" },
  run_task:                      { label: "Working a task",        pattern: "structured" },
  evolve:                        { label: "Evolving",              pattern: "shimmer" },
  audit_self_model:              { label: "Auditing self",         pattern: "inward" },
  curate_pending_plans:          { label: "Curating plans",        pattern: "housekeeping" },
  dream:                         { label: "Dreaming",              pattern: "drift" },
  budget_throttle:               { label: "Throttling · budget",   pattern: "housekeeping" },
  idle:                          { label: "Idle",                  pattern: "drift" },
  offline:                       { label: "Offline",               pattern: "drift" },
};

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

// The hero subtitle is a single stage-independent projection of the served
// `framing` (PUBLIC_FRAMING): one source of truth, so a framing change lands in
// one place and the page can never claim arrival. A missing field falls back to
// a neutral identity line rather than the retired "seeking" copy.
function renderSubtitle(framing) {
  setText("hero-subtitle", framing || "A self-improving engineering agent.");
}

function renderIdentity(statements) {
  const el = document.getElementById("identity-list");
  const section = document.getElementById("identity-section");
  if (!statements || !statements.length) {
    section.style.display = "none";
    return;
  }
  section.style.display = "";
  el.innerHTML = statements.map(s => `<div class="quote">${escapeHtml(s)}</div>`).join("");
}

function renderValues(values) {
  const section = document.getElementById("values-section");
  const el = document.getElementById("values-list");
  if (!values || !values.length) {
    section.style.display = "none";
    return;
  }
  section.style.display = "";
  el.innerHTML = values.map(v => `<li>${escapeHtml(v)}</li>`).join("");
}

function renderCardList(id, items, emptyLabel) {
  const el = document.getElementById(id);
  if (!items || !items.length) {
    el.innerHTML = `<li style="opacity:0.6;font-style:italic">${escapeHtml(emptyLabel)}</li>`;
    return;
  }
  el.innerHTML = items.map(i => `<li>${escapeHtml(i)}</li>`).join("");
}

function renderPhase(phase) {
  const orb = document.getElementById("phase-orb");
  if (!orb) return;
  const key = phase && PHASE_META[phase] ? phase : "idle";
  const meta = PHASE_META[key];
  if (orb.dataset.phase !== key) {
    orb.dataset.phase = key;
    orb.className = "phase-orb pattern-" + meta.pattern;
  }
  setText("phase-caption", meta.label);
}

function formatAwakenedSince(awakenedAt) {
  if (!awakenedAt) return "";
  const start = new Date(awakenedAt);
  if (Number.isNaN(start.getTime())) return "";
  const month = start.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
  return `Awakened ${month} ${start.getUTCDate()}, ${start.getUTCFullYear()}`;
}

function formatUptime(awakenedAt) {
  if (!awakenedAt) return "";
  const start = new Date(awakenedAt);
  if (Number.isNaN(start.getTime())) return "";
  const ms = Date.now() - start.getTime();
  if (ms < 0) return "alive for a moment";
  const totalMinutes = Math.floor(ms / 60000);
  const days = Math.floor(totalMinutes / (60 * 24));
  const hours = Math.floor((totalMinutes % (60 * 24)) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `Alive for ${days} day${days === 1 ? "" : "s"}, ${hours} hour${hours === 1 ? "" : "s"}`;
  if (hours > 0) return `Alive for ${hours} hour${hours === 1 ? "" : "s"}, ${minutes} minute${minutes === 1 ? "" : "s"}`;
  return `Alive for ${minutes} minute${minutes === 1 ? "" : "s"}`;
}

let awakenedAt = null;

function tickUptime() {
  const text = formatUptime(awakenedAt);
  const el = document.getElementById("uptime");
  if (!el) return;
  el.innerHTML = text ? `<strong>${escapeHtml(text)}</strong>` : "";
}

async function loadIdentity() {
  try {
    const r = await fetch("/api/public/identity", { headers: { "Accept": "application/json" } });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();

    awakenedAt = data.awakened_at || null;
    const isAlive = Boolean(awakenedAt);
    const dot = document.getElementById("status-dot");
    dot.classList.toggle("running", isAlive);
    setText("status-text", isAlive ? "Awake" : "Not yet awakened");

    renderIdentity(data.identity_statements || []);
    renderCardList("capabilities-list", data.capabilities_known || [], "(none discovered yet)");
    renderCardList("limitations-list", data.limitations_known || [], "(none discovered yet)");
    renderValues(data.values || []);
    setText("stat-evolutions", data.evolution_count ?? 0);
    renderSubtitle(data.framing);
    setText("awakened-since", formatAwakenedSince(awakenedAt));
    renderPhase(data.current_phase);
    tickUptime();
  } catch (err) {
    setText("status-text", "Offline");
    renderPhase("offline");
  }
}

loadIdentity();
setInterval(tickUptime, 60000);
setInterval(loadIdentity, 30000);
