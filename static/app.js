const app = {
  data: null,
  latestRunId: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(value) {
  if (!value) return "—";
  try { return new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }); }
  catch { return value; }
}

function setMessage(selector, text, kind = "") {
  const element = $(selector);
  element.textContent = text || "";
  element.className = `form-message ${kind}`.trim();
}

function adminHeaders() {
  const token = $("#adminToken")?.value?.trim();
  return token ? { "X-Admin-Token": token } : {};
}

function userHeaders() {
  const userId = $("#userIdentity")?.value?.trim() || "anonymous";
  return { "X-User-Id": userId, ...adminHeaders() };
}

async function api(path, options = {}) {
  const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) };
  const response = await fetch(path, { ...options, headers });
  let payload = null;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function renderMetrics() {
  const values = [
    ["mcps", "MCPs"], ["skills", "Skills"], ["tools", "Tools"], ["policies", "Policies"],
    ["contracts", "Contracts"], ["runs", "Runs"], ["memory_items", "Memory"], ["active_agents", "Active agents"],
  ];
  $("#metricsGrid").innerHTML = values.map(([key, label]) => `
    <div class="metric-card"><div class="eyebrow">${escapeHtml(label)}</div><div class="metric-value">${escapeHtml(app.data.overview[key])}</div><div class="metric-label">runtime count</div></div>
  `).join("");
}

function renderRuntime() {
  const runtime = app.data.runtime;
  const badge = $("#runtimeBadge");
  const label = runtime.execution_mode === "native-dsh"
    ? "DeepSeek Harness · native"
    : runtime.model_mode === "deepseek"
      ? "DeepSeek API"
      : "Deterministic demo · simulated local agents";
  badge.textContent = `${label} · online`;
  badge.className = `status-pill ${runtime.execution_mode === "native-dsh" ? "success" : "neutral"}`;
}

function renderContracts() {
  const contracts = app.data.contracts || [];
  const select = $("#contractSelect");
  const current = select.value;
  select.innerHTML = contracts.map((contract) => `<option value="${escapeHtml(contract.id)}">${escapeHtml(contract.name)} · max ${escapeHtml(contract.max_agents)}</option>`).join("");
  if (contracts.some((contract) => contract.id === current)) select.value = current;
  renderContractPreview();
  $("#contractList").innerHTML = contracts.length ? contracts.map((contract) => `
    <div class="contract-card"><header><strong>${escapeHtml(contract.name)}</strong><span class="tag">${escapeHtml(contract.version || "v1")}</span></header>
      <p>${escapeHtml(contract.description || "Bounded goal execution contract.")}</p>
      <div class="tag-row"><span class="tag">max ${escapeHtml(contract.max_agents)} agents</span><span class="tag">${escapeHtml((contract.required_skills || []).length)} skills</span><span class="tag">${escapeHtml((contract.allowed_tools || []).length)} tools</span></div>
    </div>
  `).join("") : `<div class="empty-state">No contracts configured.</div>`;
}

function renderContractPreview() {
  const contract = (app.data.contracts || []).find((item) => item.id === $("#contractSelect").value);
  if (!contract) { $("#contractPreview").innerHTML = ""; return; }
  $("#contractPreview").innerHTML = `<strong>${escapeHtml(contract.name)}</strong> · ${escapeHtml(contract.acceptance_criteria?.length || 0)} acceptance criteria · governance: ${escapeHtml(contract.governance_policy_id || "default")}`;
}

function renderRegistry(kind, listId, countId) {
  const entries = app.data.registries?.[kind] || [];
  $(countId).textContent = entries.length;
  $(listId).innerHTML = entries.length ? entries.map((entry) => `
    <div class="registry-item"><div><strong>${escapeHtml(entry.name)}</strong><small>${escapeHtml(entry.description || entry.endpoint || "Ready for runtime use")}</small><code>${escapeHtml(entry.id)}</code></div><button class="remove-button" type="button" data-remove-kind="${escapeHtml(kind)}" data-remove-id="${escapeHtml(entry.id)}" title="Remove entry">×</button></div>
  `).join("") : `<div class="empty-state">No entries yet.</div>`;
}

function renderPolicies() {
  const policies = app.data.policies || [];
  $("#policyList").innerHTML = policies.length ? policies.map((policy) => `
    <div class="policy-card"><header><strong>${escapeHtml(policy.name)}</strong><span class="tag">${escapeHtml(policy.status || "active")}</span></header>
      <p>${escapeHtml(policy.description || "Governance preflight policy.")}</p>
      <div class="tag-row">${(policy.deny_patterns || []).slice(0, 4).map((item) => `<span class="tag">blocks: ${escapeHtml(item)}</span>`).join("")}</div>
    </div>
  `).join("") : `<div class="empty-state">No governance policies configured.</div>`;
}

function renderRun() {
  const run = app.data.runs?.[0];
  if (!run) {
    $("#runTitle").textContent = "No mission executed yet";
    $("#runStatus").textContent = "Waiting";
    $("#runStatus").className = "status-pill neutral";
    $("#governanceSummary").className = "governance-summary empty-state";
    $("#governanceSummary").textContent = "Run a mission to see the governance decision.";
    $("#agentGraph").className = "agent-graph empty-state";
    $("#agentGraph").textContent = "The graph will appear here after a run.";
    $("#runOutput").className = "output-text empty-state";
    $("#runOutput").textContent = "The final synthesis will appear here.";
    $("#eventTimeline").className = "event-timeline empty-state";
    $("#eventTimeline").textContent = "No events yet.";
    $("#agentLifecycle").textContent = "0 spawned · 0 destroyed";
    $("#memoryHitCount").textContent = "0 memory hits";
    return;
  }
  app.latestRunId = run.id;
  $("#runTitle").textContent = run.goal || run.id;
  const statusClass = run.status === "completed" ? "success" : run.status === "blocked" ? "danger" : "warning";
  $("#runStatus").textContent = run.status;
  $("#runStatus").className = `status-pill ${statusClass}`;
  const allowed = Boolean(run.governance?.allowed);
  const reasons = run.governance?.reasons || [];
  const governance = $("#governanceSummary");
  governance.className = `governance-summary ${allowed ? "allowed" : "blocked"}`;
  governance.innerHTML = `<strong>${allowed ? "Governance allowed" : "Governance blocked"}</strong> · ${escapeHtml(run.governance?.policy_name || "policy")}${reasons.length ? `<br><span>${escapeHtml(reasons.join(" · "))}</span>` : " · contract and registry checks passed"}`;
  const nodes = run.graph?.nodes || [];
  $("#agentGraph").className = "agent-graph";
  $("#agentGraph").innerHTML = nodes.length ? nodes.map((node) => `<div class="agent-node"><div class="role">${escapeHtml(node.role)}</div><div class="goal">${escapeHtml(node.goal)}</div><span class="node-status ${node.status === "destroyed" ? "" : "blocked"}">${escapeHtml(node.status)}</span></div>`).join("") : `<div class="empty-state">No agent nodes were created.</div>`;
  $("#agentLifecycle").textContent = `${run.metrics?.agents_spawned || 0} spawned · ${run.metrics?.agents_destroyed || 0} destroyed`;
  $("#memoryHitCount").textContent = `${run.memory_hits?.length || 0} memory hits`;
  $("#runOutput").className = "output-text";
  $("#runOutput").textContent = run.output || "No final output.";
  const events = run.events || [];
  $("#eventTimeline").className = "event-timeline";
  $("#eventTimeline").innerHTML = events.length ? events.map((event) => `<div class="event"><span class="event-marker"></span><div><strong>${escapeHtml(event.type.replaceAll("_", " "))}</strong><p>${escapeHtml(event.message)}</p><time>${escapeHtml(formatDate(event.at))}</time></div></div>`).join("") : `<div class="empty-state">No events recorded.</div>`;
}

function renderMemory() {
  const memory = app.data.memory || [];
  $("#memoryList").className = memory.length ? "memory-list" : "memory-list empty-state";
  $("#memoryList").innerHTML = memory.length ? memory.slice(0, 6).map((item) => `<article class="memory-card"><p>${escapeHtml(item.text)}</p><footer><span>${escapeHtml(item.kind || "memory")}</span><span>${escapeHtml(formatDate(item.created_at))}</span></footer></article>`).join("") : "No memory items yet.";
}

function renderAll() {
  renderRuntime();
  renderMetrics();
  renderContracts();
  renderRegistry("mcps", "#mcpList", "#mcpCount");
  renderRegistry("skills", "#skillList", "#skillCount");
  renderRegistry("tools", "#toolList", "#toolCount");
  renderPolicies();
  renderRun();
  renderMemory();
}

async function refresh() {
  try {
    app.data = await api("/api/state", { headers: userHeaders() });
    renderAll();
  } catch (error) {
    $("#runtimeBadge").textContent = "Offline";
    $("#runtimeBadge").className = "status-pill danger";
    setMessage("#missionMessage", error.message, "error");
  }
}

$$(".nav-button").forEach((button) => button.addEventListener("click", () => {
  $$(".nav-button").forEach((item) => item.classList.toggle("active", item === button));
  $$(".plane").forEach((plane) => plane.classList.toggle("active-plane", plane.id === button.dataset.plane));
}));

$("#contractSelect").addEventListener("change", renderContractPreview);

$("#missionForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  setMessage("#missionMessage", "Running planner → researcher → builder → reviewer → synthesizer…");
  try {
     await api("/api/runs", { method: "POST", headers: userHeaders(), body: JSON.stringify({ goal: $("#missionGoal").value, contract_id: $("#contractSelect").value, requested_agents: Number($("#agentCount").value) }) });
    setMessage("#missionMessage", "Mission completed. Review the graph and audit trail below.", "success");
    await refresh();
  } catch (error) {
    setMessage("#missionMessage", error.message, "error");
  } finally { button.disabled = false; }
});

$("#feedbackButton").addEventListener("click", async () => {
  if (!app.latestRunId) { setMessage("#feedbackMessage", "Run a mission before adding feedback.", "error"); return; }
  try {
     await api("/api/feedback", { method: "POST", headers: userHeaders(), body: JSON.stringify({ run_id: app.latestRunId, text: $("#feedbackText").value, score: Number($("#feedbackScore").value) }) });
    $("#feedbackText").value = "";
    setMessage("#feedbackMessage", "Feedback saved to continual-learning memory.", "success");
    await refresh();
  } catch (error) { setMessage("#feedbackMessage", error.message, "error"); }
});

$$(".registry-form").forEach((form) => form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(form).entries());
  const kind = form.dataset.kind;
  const payload = { name: values.name, description: values.description || "" };
  if (kind === "mcps") payload.endpoint = values.endpoint || "builtin://custom";
  try {
    await api(`/api/registries/${kind}`, { method: "POST", headers: adminHeaders(), body: JSON.stringify(payload) });
    form.reset();
    setMessage("#adminMessage", `${kind.slice(0, -1).toUpperCase()} added to the registry.`, "success");
    await refresh();
  } catch (error) { setMessage("#adminMessage", error.message, "error"); }
}));

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-remove-kind]");
  if (!button) return;
  try {
    await api(`/api/registries/${button.dataset.removeKind}/${button.dataset.removeId}`, { method: "DELETE", headers: adminHeaders() });
    setMessage("#adminMessage", "Registry entry removed.", "success");
    await refresh();
  } catch (error) { setMessage("#adminMessage", error.message, "error"); }
});

$("#policyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  try {
    await api("/api/governance/policies", { method: "POST", headers: adminHeaders(), body: JSON.stringify({ name: values.name, deny_patterns: values.deny_patterns.split(",").map((item) => item.trim()).filter(Boolean), approval_required_for: values.approval_required_for.split(",").map((item) => item.trim()).filter(Boolean) }) });
    event.currentTarget.reset();
    setMessage("#adminMessage", "Governance policy created.", "success");
    await refresh();
  } catch (error) { setMessage("#adminMessage", error.message, "error"); }
});

$("#contractForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget).entries());
  const base = app.data.contracts?.[0] || {};
  try {
    await api("/api/contracts", { method: "POST", headers: adminHeaders(), body: JSON.stringify({ name: values.name, max_agents: Number(values.max_agents), acceptance_criteria: values.acceptance_criteria.split(",").map((item) => item.trim()).filter(Boolean), governance_policy_id: base.governance_policy_id || "default-safe", required_skills: base.required_skills || [], allowed_tools: base.allowed_tools || [], agent_roles: base.agent_roles || ["planner", "reviewer", "synthesizer"] }) });
    event.currentTarget.reset();
    setMessage("#adminMessage", "Agent contract created.", "success");
    await refresh();
  } catch (error) { setMessage("#adminMessage", error.message, "error"); }
});

$("#resetButton").addEventListener("click", async () => {
  if (!window.confirm("Reset registries, contracts, runs, and memory to the demo defaults?")) return;
  try {
    await api("/api/reset", { method: "POST", headers: adminHeaders(), body: "{}" });
    setMessage("#adminMessage", "Demo state reset.", "success");
    await refresh();
  } catch (error) { setMessage("#adminMessage", error.message, "error"); }
});

async function loadInspection(path, selector) {
  try {
    const payload = await api(path, { headers: adminHeaders() });
    $(selector).className = "inspection-output";
    $(selector).textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    $(selector).className = "inspection-output empty-state";
    $(selector).textContent = error.message;
  }
}

$("#auditRefreshButton").addEventListener("click", () => loadInspection("/api/admin/audit", "#auditOutput"));
$("#errorsRefreshButton").addEventListener("click", () => loadInspection("/api/admin/errors", "#errorsOutput"));

$("#adminToken").value = sessionStorage.getItem("adyaAdminToken") || "";
$("#adminToken").addEventListener("input", (event) => sessionStorage.setItem("adyaAdminToken", event.target.value));
$("#userIdentity").value = sessionStorage.getItem("adyaUserId") || "anonymous";
$("#userIdentity").addEventListener("input", (event) => {
  sessionStorage.setItem("adyaUserId", event.target.value || "anonymous");
});

refresh();
