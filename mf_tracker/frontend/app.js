const state = {
  health: null,
  amcs: [],
  funds: [],
  holdingsPage: 1,
  comparePage: 1,
  queue: [],
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const indian = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const month = new Intl.DateTimeFormat("en-IN", { month: "short", year: "numeric" });
const dateTime = new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short" });

function formatMonth(value) {
  if (!value) return "—";
  return month.format(new Date(`${value}T00:00:00`));
}

function formatNumber(value, fallback = "—") {
  return value == null ? fallback : indian.format(value);
}

function formatDateTime(value) {
  if (!value) return "—";
  return dateTime.format(new Date(value.endsWith("Z") ? value : `${value}Z`));
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = payload.error || payload.detail || {};
    const thrown = new Error(error.message || `Request failed with ${response.status}`);
    thrown.code = error.code || "request_error";
    thrown.details = error.details;
    thrown.status = response.status;
    throw thrown;
  }
  return payload;
}

function toast(message, type = "info") {
  const element = document.createElement("div");
  element.className = `toast ${type}`;
  element.textContent = message;
  $("#toastRegion").append(element);
  setTimeout(() => element.remove(), 4500);
}

function showConnection(isConnected) {
  $("#connectionBanner").hidden = isConnected;
  $("#health").className = `health ${isConnected ? "ok" : "error"}`;
  $("#health span").textContent = isConnected ? "Archive connected" : "Backend offline";
}

async function checkHealth() {
  try {
    state.health = await api("/api/health");
    showConnection(true);
    renderWorkspaceStatus();
    return true;
  } catch {
    showConnection(false);
    return false;
  }
}

function activeView() {
  const route = window.location.hash.slice(1);
  return ["overview", "holdings", "compare", "imports", "data"].includes(route) ? route : "overview";
}

async function showView(view, updateHash = true) {
  if (updateHash && window.location.hash !== `#${view}`) {
    window.location.hash = view;
    return;
  }
  $$(".view").forEach((section) => { section.hidden = section.dataset.page !== view; });
  $$(".section-tabs button").forEach((button) => button.setAttribute("aria-current", button.dataset.view === view ? "page" : "false"));
  const loaders = { overview: loadOverview, holdings: initializeHoldings, compare: initializeCompare, imports: loadImports, data: loadData };
  try { await loaders[view](); } catch (error) { toast(error.message, "error"); }
}

function fillSelect(select, items, valueKey, label, placeholder, selectedValue = "") {
  const options = [`<option value="">${escapeHtml(placeholder)}</option>`, ...items.map((item) => `<option value="${escapeHtml(item[valueKey])}">${escapeHtml(label(item))}</option>`)].join("");
  select.innerHTML = options;
  if (selectedValue && items.some((item) => String(item[valueKey]) === String(selectedValue))) select.value = selectedValue;
}

async function loadReferenceData(force = false) {
  if (state.amcs.length && state.funds.length && !force) return;
  [state.amcs, state.funds] = await Promise.all([api("/api/amcs"), api("/api/funds")]);
  [$("#overviewAmc"), $("#holdingsAmc"), $("#compareAmc")].forEach((select) => {
    const current = select.value;
    fillSelect(select, state.amcs, "slug", (item) => item.name, select.id === "overviewAmc" ? "All AMCs" : "Choose AMC", current);
  });
}

function renderEmpty(target, colspan, message, action = "") {
  target.innerHTML = `<tr><td colspan="${colspan}" class="empty-state">${escapeHtml(message)}${action}</td></tr>`;
}

async function loadOverview() {
  await loadReferenceData();
  const amc = $("#overviewAmc").value;
  const data = await api(`/api/overview?months=6${amc ? `&amc=${encodeURIComponent(amc)}` : ""}`);
  renderCoverage(data);
  const labels = [
    ["AMCs", data.counts.amc_count], ["Active funds", data.counts.fund_count],
    ["Active snapshots", data.counts.snapshot_count], ["Warnings logged", data.counts.warning_count],
  ];
  $("#overviewCounts").innerHTML = labels.map(([label, value]) => `<div class="register-item"><span>${label}</span><strong>${integer.format(value || 0)}</strong></div>`).join("");
  $("#recentImports").innerHTML = data.recent_imports.length ? data.recent_imports.map((item) => `<div class="activity-item"><div><strong title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</strong><small>${escapeHtml(item.effective_metadata?.amc_name || "Unknown AMC")} · ${formatMonth(item.report_date)}</small></div><span class="status-badge ${item.issue_count ? "warning" : "ok"}">${item.issue_count ? `${item.issue_count} issues` : item.status}</span></div>`).join("") : `<div class="empty-state">No source files have been imported.</div>`;
}

function renderCoverage(data) {
  const target = $("#coverageMatrix");
  if (!data.funds.length || !data.dates.length) {
    target.innerHTML = `<div class="empty-state">No disclosures yet. Import a supported AMC workbook to begin.</div>`;
    return;
  }
  const cells = new Map(data.cells.map((cell) => [`${cell.fund_id}:${cell.report_date}`, cell]));
  target.innerHTML = `<table class="coverage"><thead><tr><th>Fund</th>${data.dates.map((value) => `<th>${formatMonth(value)}</th>`).join("")}</tr></thead><tbody>${data.funds.map((fund) => `<tr><td class="fund-cell"><strong>${escapeHtml(fund.name)}</strong><small>${escapeHtml(fund.amc_name)} · ${escapeHtml(fund.sheet_code)}</small></td>${data.dates.map((value) => {
    const cell = cells.get(`${fund.id}:${value}`);
    if (!cell) return `<td><button class="coverage-cell" disabled aria-label="No snapshot for ${escapeHtml(fund.name)} in ${formatMonth(value)}">—</button></td>`;
    const warning = Number(cell.issue_count) > 0;
    return `<td><button class="coverage-cell ${warning ? "warning" : "available"}" data-fund="${fund.id}" data-date="${value}" aria-label="Open ${escapeHtml(fund.name)} for ${formatMonth(value)}${warning ? ", has warnings" : ""}">${warning ? `${cell.issue_count} issue${cell.issue_count === 1 ? "" : "s"}` : "Open"}</button></td>`;
  }).join("")}</tr>`).join("")}</tbody></table>`;
  $$(".coverage-cell[data-fund]").forEach((button) => button.addEventListener("click", async () => {
    sessionStorage.setItem("holdingsTarget", JSON.stringify({ fund: button.dataset.fund, date: button.dataset.date }));
    showView("holdings");
  }));
}

function fundsForAmc(amc) { return state.funds.filter((fund) => !amc || fund.amc_slug === amc); }

async function initializeHoldings() {
  await loadReferenceData();
  const target = JSON.parse(sessionStorage.getItem("holdingsTarget") || "null");
  if (target) sessionStorage.removeItem("holdingsTarget");
  let amc = $("#holdingsAmc").value;
  if (target) amc = state.funds.find((fund) => String(fund.id) === String(target.fund))?.amc_slug || amc;
  if (!amc && state.amcs.length) amc = state.amcs[0].slug;
  $("#holdingsAmc").value = amc;
  const funds = fundsForAmc(amc);
  fillSelect($("#holdingsFund"), funds, "id", (item) => item.name, "Choose fund", target?.fund || $("#holdingsFund").value || funds[0]?.id);
  if ($("#holdingsFund").value) await loadHoldingDates(target?.date);
  else renderEmpty($("#holdingsBody"), 7, "Import a workbook to inspect holdings.");
}

async function loadHoldingDates(preferred = "") {
  const snapshots = await api(`/api/funds/${$("#holdingsFund").value}/snapshots`);
  fillSelect($("#holdingsDate"), snapshots, "report_date", (item) => formatMonth(item.report_date), "Choose month", preferred || $("#holdingsDate").value || snapshots[0]?.report_date);
  await loadHoldings();
}

async function loadHoldings() {
  const fund = $("#holdingsFund").value;
  const reportDate = $("#holdingsDate").value;
  if (!fund || !reportDate) { renderEmpty($("#holdingsBody"), 7, "Choose a fund and reporting month."); return; }
  const params = new URLSearchParams({ report_date: reportDate, search: $("#holdingsSearch").value, asset_class: $("#holdingsAsset").value, page: state.holdingsPage, page_size: 50 });
  const data = await api(`/api/funds/${fund}/holdings?${params}`);
  $("#holdingsCount").textContent = `${integer.format(data.total)} holdings`;
  $("#holdingsExport").href = `/api/funds/${fund}/holdings.csv?${params}`;
  if (!data.items.length) renderEmpty($("#holdingsBody"), 7, "No holdings match these filters.");
  else $("#holdingsBody").innerHTML = data.items.map((item) => `<tr><td title="${escapeHtml(item.display_name)}"><strong>${escapeHtml(item.display_name)}</strong>${item.isin ? `<small>${escapeHtml(item.isin)}</small>` : ""}</td><td><span class="asset-label">${escapeHtml(item.asset_class.replaceAll("_", " "))}</span></td><td>${escapeHtml(item.instrument_type.replaceAll("_", " "))}</td><td>${formatNumber(item.quantity)}</td><td>${formatNumber(item.market_value_lakh)}</td><td>${formatNumber(item.weight)}%</td><td>${escapeHtml(item.industry_rating || item.direction || item.section || "—")}</td></tr>`).join("");
  populateAssetSelect($("#holdingsAsset"), data.items.map((item) => item.asset_class));
  renderPagination($("#holdingsPagination"), data, (page) => { state.holdingsPage = page; loadHoldings(); });
}

function populateAssetSelect(select, values) {
  const current = select.value;
  const existing = new Set([...select.options].map((option) => option.value));
  [...new Set(values)].sort().forEach((value) => {
    if (!existing.has(value)) select.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(value)}">${escapeHtml(value.replaceAll("_", " "))}</option>`);
  });
  select.value = current;
}

function renderPagination(target, data, onPage) {
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  target.innerHTML = `<button type="button" data-page="${data.page - 1}" ${data.page <= 1 ? "disabled" : ""} aria-label="Previous page">←</button><span>Page ${data.page} of ${pages}</span><button type="button" data-page="${data.page + 1}" ${data.page >= pages ? "disabled" : ""} aria-label="Next page">→</button>`;
  target.querySelectorAll("button:not(:disabled)").forEach((button) => button.addEventListener("click", () => onPage(Number(button.dataset.page))));
}

async function initializeCompare() {
  await loadReferenceData();
  let amc = $("#compareAmc").value || state.amcs[0]?.slug || "";
  $("#compareAmc").value = amc;
  const funds = fundsForAmc(amc);
  fillSelect($("#compareFund"), funds, "id", (item) => item.name, "Choose fund", $("#compareFund").value || funds[0]?.id);
  if ($("#compareFund").value) await loadCompareDates();
  else renderEmpty($("#compareBody"), 8, "Import at least two disclosures for a fund to compare them.");
}

async function loadCompareDates() {
  const snapshots = await api(`/api/funds/${$("#compareFund").value}/snapshots`);
  fillSelect($("#compareFrom"), snapshots, "report_date", (item) => formatMonth(item.report_date), "Earlier month", $("#compareFrom").value || snapshots[1]?.report_date || "");
  fillSelect($("#compareTo"), snapshots, "report_date", (item) => formatMonth(item.report_date), "Later month", $("#compareTo").value || snapshots[0]?.report_date || "");
  await loadComparison();
}

async function loadComparison() {
  const fund = $("#compareFund").value;
  const from = $("#compareFrom").value;
  const to = $("#compareTo").value;
  if (!fund || !from || !to) {
    renderEmpty($("#compareBody"), 8, "This fund needs at least two reporting months before it can be compared.");
    $("#changeCounts").innerHTML = "";
    return;
  }
  const params = new URLSearchParams({ from_date: from, to_date: to, search: $("#compareSearch").value, asset_class: $("#compareAsset").value, change_type: $("#compareChange").value, page: state.comparePage, page_size: 50 });
  const data = await api(`/api/funds/${fund}/comparison?${params}`);
  $("#compareExport").href = `/api/funds/${fund}/comparison.csv?${params}`;
  const kinds = ["introduced", "increased", "decreased", "exited", "unchanged"];
  $("#changeCounts").innerHTML = kinds.map((kind) => `<div class="change-stat"><span>${kind}</span><strong>${integer.format(data.counts[kind])}</strong></div>`).join("");
  if (!data.items.length) renderEmpty($("#compareBody"), 8, "No changes match these filters.");
  else $("#compareBody").innerHTML = data.items.map((item) => {
    const name = item.display_name_to || item.display_name;
    const deltaClass = Number(item.quantity_delta) > 0 ? "number-positive" : Number(item.quantity_delta) < 0 ? "number-negative" : "";
    const weightClass = Number(item.weight_delta) > 0 ? "number-positive" : Number(item.weight_delta) < 0 ? "number-negative" : "";
    return `<tr><td title="${escapeHtml(name)}">${escapeHtml(name)}</td><td><span class="change-badge ${item.change_type}">${item.change_type}</span></td><td>${formatNumber(item.quantity)}</td><td>${formatNumber(item.quantity_to)}</td><td class="${deltaClass}">${formatNumber(item.quantity_delta)}</td><td>${formatNumber(item.weight)}%</td><td>${formatNumber(item.weight_to)}%</td><td class="${weightClass}">${formatNumber(item.weight_delta)}%</td></tr>`;
  }).join("");
  populateAssetSelect($("#compareAsset"), data.items.map((item) => item.asset_class_effective));
  renderPagination($("#comparePagination"), data, (page) => { state.comparePage = page; loadComparison(); });
}

function importForm(file, replace = false) {
  const form = new FormData();
  form.append("file", file);
  form.append("replace", replace);
  form.append("amc", $("#importAmc").value);
  const fields = [["report_date", "#overrideDate"], ["fund_code", "#overrideFundCode"], ["fund_name", "#overrideFundName"], ["amc_name", "#overrideAmcName"]];
  fields.forEach(([name, selector]) => { if ($(selector).value.trim()) form.append(name, $(selector).value.trim()); });
  return form;
}

function addFiles(files) {
  const accepted = [...files].slice(0, 20 - state.queue.length).filter((file) => /\.xlsx?$/i.test(file.name) && file.size <= 25 * 1024 * 1024);
  const rejected = [...files].length - accepted.length;
  if (rejected) toast(`${rejected} file${rejected === 1 ? " was" : "s were"} skipped. Use XLS/XLSX files up to 25 MiB.`, "error");
  state.queue.push(...accepted.map((file) => ({ id: crypto.randomUUID(), file, status: "ready", result: null, error: null })));
  renderQueue();
}

function renderQueue() {
  $("#queueSection").hidden = !state.queue.length;
  $("#queueSummary").textContent = `${state.queue.length} file${state.queue.length === 1 ? "" : "s"} · validate before import`;
  $("#fileQueue").innerHTML = state.queue.map((item) => {
    const summary = item.result ? `${escapeHtml(item.result.amc_name)} · ${formatMonth(item.result.report_date)} · ${integer.format(item.result.holding_count)} holdings${item.result.issues.length ? ` · ${item.result.issues.length} warnings` : ""}` : item.error ? `<span class="file-error">${escapeHtml(item.error.message)}</span>` : "Ready to inspect";
    let actions = `<button class="secondary small" data-action="validate" data-id="${item.id}">Validate</button>`;
    if (item.status === "validated") actions = `<button class="primary small" data-action="import" data-id="${item.id}">Import</button>`;
    if (item.status === "conflict") actions = `<button class="primary small" data-action="replace" data-id="${item.id}">Replace active</button>`;
    if (["validating", "importing"].includes(item.status)) actions = `<button class="secondary small" disabled>${item.status === "validating" ? "Validating…" : "Importing…"}</button>`;
    if (["imported", "duplicate"].includes(item.status)) actions = `<span class="status-badge ok">${item.status}</span>`;
    return `<article class="file-row"><div><strong title="${escapeHtml(item.file.name)}">${escapeHtml(item.file.name)}</strong><small>${formatNumber(item.file.size / 1024)} KB · ${escapeHtml(item.status)}</small></div><div class="file-result">${summary}</div><div class="file-actions">${actions}<button class="secondary small" data-action="remove" data-id="${item.id}" aria-label="Remove ${escapeHtml(item.file.name)}">×</button></div></article>`;
  }).join("");
  $$("#fileQueue [data-action]").forEach((button) => button.addEventListener("click", () => queueAction(button.dataset.id, button.dataset.action)));
}

async function queueAction(id, action) {
  const item = state.queue.find((candidate) => candidate.id === id);
  if (!item) return;
  if (action === "remove") { state.queue = state.queue.filter((candidate) => candidate.id !== id); renderQueue(); return; }
  item.status = action === "validate" ? "validating" : "importing";
  item.error = null;
  renderQueue();
  try {
    const endpoint = action === "validate" ? "/api/imports/validate" : "/api/imports";
    item.result = await api(endpoint, { method: "POST", body: importForm(item.file, action === "replace") });
    item.status = action === "validate" ? "validated" : item.result.status === "duplicate" ? "duplicate" : "imported";
    if (action !== "validate") { toast(`${item.file.name} imported.`); await loadReferenceData(true); await loadImports(); }
  } catch (error) {
    item.error = error;
    item.status = error.code === "snapshot_conflict" ? "conflict" : "error";
  }
  renderQueue();
}

async function loadImports() {
  const data = await api("/api/imports?page_size=50");
  if (!data.items.length) renderEmpty($("#importsBody"), 6, "No files have entered the archive yet.");
  else $("#importsBody").innerHTML = data.items.map((item) => {
    const funds = item.effective_metadata?.funds?.map((fund) => fund.fund_code).join(", ") || "—";
    return `<tr><td title="${escapeHtml(item.filename)}"><strong>${escapeHtml(item.filename)}</strong><small>${escapeHtml(item.reader)} · ${escapeHtml(item.parser_version)}</small></td><td>${escapeHtml(item.effective_metadata?.amc_name || "—")} · ${escapeHtml(funds)}</td><td>${formatMonth(item.report_date)}</td><td><span class="status-badge ${item.issue_count ? "warning" : "ok"}">${item.lifecycle_status}</span></td><td>${item.active_snapshot_count}/${item.snapshot_count}</td><td>${formatDateTime(item.ingested_at)}</td></tr>`;
  }).join("");
}

function renderWorkspaceStatus() {
  if (!state.health) return;
  const rows = [["Service", "Online"], ["Database", state.health.database], ["Schema", `v${state.health.schema_version}`], ["Source archive", state.health.archive]];
  $("#workspaceStatus").innerHTML = rows.map(([term, value]) => `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
}

async function loadData() { if (!state.health) await checkHealth(); renderWorkspaceStatus(); }

function debounce(callback, delay = 250) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => callback(...args), delay); };
}

$$('[data-view]').forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
window.addEventListener("hashchange", () => showView(activeView(), false));
$("#retryConnection").addEventListener("click", async () => { if (await checkHealth()) showView(activeView(), false); });
$("#overviewAmc").addEventListener("change", loadOverview);
$("#holdingsAmc").addEventListener("change", () => { const funds = fundsForAmc($("#holdingsAmc").value); fillSelect($("#holdingsFund"), funds, "id", (item) => item.name, "Choose fund", funds[0]?.id); loadHoldingDates(); });
$("#holdingsFund").addEventListener("change", () => loadHoldingDates());
$("#holdingsDate").addEventListener("change", () => { state.holdingsPage = 1; loadHoldings(); });
$("#holdingsSearch").addEventListener("input", debounce(() => { state.holdingsPage = 1; loadHoldings(); }));
$("#holdingsAsset").addEventListener("change", () => { state.holdingsPage = 1; loadHoldings(); });
$("#compareAmc").addEventListener("change", () => { const funds = fundsForAmc($("#compareAmc").value); fillSelect($("#compareFund"), funds, "id", (item) => item.name, "Choose fund", funds[0]?.id); loadCompareDates(); });
$("#compareFund").addEventListener("change", loadCompareDates);
[$("#compareFrom"), $("#compareTo"), $("#compareChange"), $("#compareAsset")].forEach((element) => element.addEventListener("change", () => { state.comparePage = 1; loadComparison(); }));
$("#compareSearch").addEventListener("input", debounce(() => { state.comparePage = 1; loadComparison(); }));
$("#browseFiles").addEventListener("click", () => $("#fileInput").click());
$("#fileInput").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
const dropZone = $("#dropZone");
["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add("dragging"); }));
["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove("dragging"); }));
dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
$("#clearQueue").addEventListener("click", () => { state.queue = state.queue.filter((item) => !["imported", "duplicate"].includes(item.status)); renderQueue(); });
$("#refreshImports").addEventListener("click", loadImports);
$("#verifyArchive").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true; button.textContent = "Verifying…";
  try {
    const report = await api("/api/archive/verify", { method: "POST" });
    const issues = Object.values(report).flat().length;
    toast(issues ? `Verification found ${issues} archive issue${issues === 1 ? "" : "s"}.` : "Every archived source matches its recorded hash.", issues ? "error" : "info");
  } catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; button.textContent = "Run verification"; }
});

const connected = await checkHealth();
if (connected) await showView(activeView(), false);
