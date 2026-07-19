const state = {
  health: null,
  amcs: [],
  funds: [],
  timeline: null,
  timelinePeriod: localStorage.getItem("mfTracker.period") || "6m",
  timelineChange: "",
  timelineSort: localStorage.getItem("mfTracker.sort") || "latest",
  sortDate: "",
  sortDirection: "desc",
  renderLimit: 100,
  expandedIdentity: "",
  timelineController: null,
  autoScrollLatest: true,
  queue: [],
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const indian = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const signedInteger = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0, signDisplay: "always" });
const signedNumber = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, signDisplay: "always" });
const fixedWeight = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const month = new Intl.DateTimeFormat("en-IN", { month: "short", year: "numeric" });
const dateTime = new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short" });
const CHANGE_ORDER = ["introduced", "increased", "decreased", "exited", "unchanged"];

function formatMonth(value) {
  if (!value) return "—";
  return month.format(new Date(`${value}T00:00:00`));
}

function formatNumber(value, fallback = "—") {
  return value == null ? fallback : indian.format(value);
}

function formatPercent(value, fallback = "—") {
  return value == null ? fallback : `${indian.format(value * 100)}%`;
}

function formatTimelineWeight(value, fallback = "—") {
  return value == null ? fallback : `${fixedWeight.format(value * 100)}%`;
}

function formatSignedPercent(value, fallback = "—") {
  if (value == null) return fallback;
  const points = value * 100;
  return `${points > 0 ? "+" : ""}${indian.format(points)} pp`;
}

function formatDateTime(value) {
  if (!value) return "—";
  return dateTime.format(new Date(value.endsWith("Z") ? value : `${value}Z`));
}

function holdingDescriptor(item, point) {
  if (!point.present) return { value: "—", unit: "", spoken: "not held" };
  if (point.action_metric === "market_value_lakh") {
    if (point.market_value_lakh == null) return { value: "Unavailable", unit: "", spoken: "market value unavailable" };
    const value = `₹${indian.format(point.market_value_lakh)} L`;
    return { value, unit: "₹ lakh", spoken: `${indian.format(point.market_value_lakh)} rupees lakh` };
  }
  if (point.quantity == null) return { value: "Unavailable", unit: "", spoken: "quantity unavailable" };
  const equity = ["domestic_equity", "foreign_equity"].includes(item.asset_class);
  const unit = equity ? "shares" : "units";
  const value = `${indian.format(point.quantity)} ${unit}`;
  return { value, unit, spoken: value };
}

function movementDisplay(point) {
  if (point.action === "introduced") return { html: '<span class="movement positive">New position</span>', spoken: "new position" };
  if (point.action === "exited") return { html: '<span class="movement negative">Position exited</span>', spoken: "position exited" };
  if (point.action === "unchanged" && point.present) return { html: '<span class="movement muted">• No trade</span>', spoken: "no trade" };
  if (["increased", "decreased"].includes(point.action)) {
    const positive = point.action === "increased";
    const arrow = positive ? "↑" : "↓";
    const direction = positive ? "positive" : "negative";
    const formatted = point.action_delta == null
      ? "Unavailable"
      : point.action_metric === "market_value_lakh"
        ? `${point.action_delta > 0 ? "+" : "−"}₹${indian.format(Math.abs(point.action_delta))} L`
        : signedNumber.format(point.action_delta);
    return { html: `<span class="movement ${direction}">${arrow} ${escapeHtml(formatted)}</span>`, spoken: `${point.action}, ${formatted}` };
  }
  if (!point.present) return { html: '<span class="movement muted">Not held</span>', spoken: "not held" };
  return { html: '<span class="movement muted">Starting point</span>', spoken: "starting point" };
}

function formatFocusEndpoint(item, quantity, marketValue) {
  if (quantity != null) {
    const unit = ["domestic_equity", "foreign_equity"].includes(item.asset_class) ? "shares" : "units";
    return `${formatNumber(quantity)} ${unit}`;
  }
  return marketValue == null ? null : `₹${formatNumber(marketValue)} L`;
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

function fillSelect(select, items, valueKey, label, placeholder, selectedValue = "") {
  select.innerHTML = [`<option value="">${escapeHtml(placeholder)}</option>`, ...items.map((item) => `<option value="${escapeHtml(item[valueKey])}">${escapeHtml(label(item))}</option>`)].join("");
  if (selectedValue && items.some((item) => String(item[valueKey]) === String(selectedValue))) select.value = selectedValue;
}

async function loadReferenceData(force = false) {
  if (state.amcs.length && state.funds.length && !force) return;
  [state.amcs, state.funds] = await Promise.all([api("/api/amcs"), api("/api/funds")]);
  [$("#overviewAmc"), $("#timelineAmc")].forEach((select) => {
    const saved = select.id === "timelineAmc" ? localStorage.getItem("mfTracker.amc") : "";
    const current = select.value || saved || "";
    fillSelect(select, state.amcs, "slug", (item) => item.name, select.id === "overviewAmc" ? "All AMCs" : "Choose AMC", current);
  });
}

function fundsForAmc(amc) {
  return state.funds.filter((fund) => !amc || fund.amc_slug === amc);
}

async function loadOverview() {
  await loadReferenceData();
  const amc = $("#overviewAmc").value;
  const data = await api(`/api/overview?months=6${amc ? `&amc=${encodeURIComponent(amc)}` : ""}`);
  const labels = [
    ["AMCs", data.counts.amc_count], ["Active funds", data.counts.fund_count],
    ["Disclosures", data.counts.snapshot_count], ["Warnings", data.counts.warning_count],
  ];
  $("#overviewCounts").innerHTML = labels.map(([label, value]) => `<div><span>${label}</span><strong>${integer.format(value || 0)}</strong></div>`).join("");
  renderCoverage(data);
}

function renderCoverage(data) {
  const target = $("#coverageMatrix");
  if (!data.funds.length || !data.dates.length) {
    target.innerHTML = `<div class="empty-state">No disclosures yet. Import a supported AMC workbook to begin.</div>`;
    return;
  }
  const cells = new Map(data.cells.map((cell) => [`${cell.fund_id}:${cell.report_date}`, cell]));
  target.innerHTML = `<table class="coverage"><thead><tr><th scope="col">Fund</th>${data.dates.map((value) => `<th scope="col">${formatMonth(value)}</th>`).join("")}</tr></thead><tbody>${data.funds.map((fund) => `<tr><th scope="row" class="fund-cell"><strong>${escapeHtml(fund.name)}</strong><small>${escapeHtml(fund.amc_name)} · ${escapeHtml(fund.sheet_code)}</small></th>${data.dates.map((value) => {
    const cell = cells.get(`${fund.id}:${value}`);
    if (!cell) return `<td><button class="coverage-cell" disabled aria-label="No snapshot for ${escapeHtml(fund.name)} in ${formatMonth(value)}">—</button></td>`;
    const warning = Number(cell.issue_count) > 0;
    return `<td><button class="coverage-cell ${warning ? "warning" : "available"}" data-fund="${fund.id}" data-date="${value}" aria-label="Open ${escapeHtml(fund.name)} timeline at ${formatMonth(value)}${warning ? ", has warnings" : ""}">${warning ? `${cell.issue_count} issue${cell.issue_count === 1 ? "" : "s"}` : "Open"}</button></td>`;
  }).join("")}</tr>`).join("")}</tbody></table>`;
}

async function initializeTimeline() {
  await loadReferenceData();
  let amc = $("#timelineAmc").value || localStorage.getItem("mfTracker.amc") || state.amcs[0]?.slug || "";
  if (!state.amcs.some((item) => item.slug === amc)) amc = state.amcs[0]?.slug || "";
  $("#timelineAmc").value = amc;
  populateTimelineFunds(localStorage.getItem("mfTracker.fund") || "");
  $("#timelineSort").value = ["latest", "name", "movement"].includes(state.timelineSort) ? state.timelineSort : "latest";
  setPeriod(state.timelinePeriod, false);
  if ($("#timelineFund").value) await loadTimeline();
  else renderTimelineEmpty("Import a disclosure to build a portfolio timeline.");
}

function populateTimelineFunds(preferred = "") {
  const funds = fundsForAmc($("#timelineAmc").value);
  fillSelect($("#timelineFund"), funds, "id", (item) => item.name, "Choose fund", preferred || $("#timelineFund").value || funds[0]?.id);
}

function setPeriod(period, reload = true) {
  state.timelinePeriod = ["6m", "12m", "24m", "all"].includes(period) ? period : "6m";
  localStorage.setItem("mfTracker.period", state.timelinePeriod);
  $$("#periodButtons button").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.period === state.timelinePeriod)));
  state.autoScrollLatest = true;
  if (reload && $("#timelineFund").value) loadTimeline({ resetFocus: true });
}

function timelineParams({ includeChange = true } = {}) {
  const params = new URLSearchParams({
    period: state.timelinePeriod,
    search: $("#timelineSearch").value.trim(),
    asset_class: $("#timelineAsset").value,
    change_type: includeChange ? state.timelineChange : "",
  });
  if ($("#focusFrom").value) params.set("focus_from", $("#focusFrom").value);
  if ($("#focusTo").value) params.set("focus_to", $("#focusTo").value);
  return params;
}

async function loadTimeline({ resetFocus = false } = {}) {
  const fund = $("#timelineFund").value;
  if (!fund) { renderTimelineEmpty("Choose a fund to read its timeline."); return; }
  state.timelineController?.abort();
  state.timelineController = new AbortController();
  if (resetFocus) {
    $("#focusFrom").value = "";
    $("#focusTo").value = "";
  }
  $("#timelineCount").textContent = "Reading disclosures…";
  $("#timelineBody").setAttribute("aria-busy", "true");
  try {
    const params = timelineParams();
    const data = await api(`/api/funds/${fund}/timeline?${params}`, { signal: state.timelineController.signal });
    state.timeline = data;
    state.renderLimit = 100;
    state.expandedIdentity = "";
    fillTimelineDateControls(data);
    fillTimelineAssets(data.asset_classes);
    renderMovementCounts(data.focus.counts);
    renderTimeline();
    updateExportLinks();
    $("#timelineCount").textContent = `${integer.format(data.total)} instrument${data.total === 1 ? "" : "s"}`;
    localStorage.setItem("mfTracker.amc", $("#timelineAmc").value);
    localStorage.setItem("mfTracker.fund", fund);
  } catch (error) {
    if (error.name === "AbortError") return;
    renderTimelineEmpty(error.message);
    toast(error.message, "error");
  } finally {
    $("#timelineBody").removeAttribute("aria-busy");
  }
}

function fillTimelineDateControls(data) {
  const dates = data.dates;
  fillSelect($("#focusFrom"), dates, "report_date", (item) => formatMonth(item.report_date), "Earlier month", data.focus.from_date);
  fillSelect($("#focusTo"), dates, "report_date", (item) => formatMonth(item.report_date), "Later month", data.focus.to_date);
  constrainFocusOptions();
}

function constrainFocusOptions() {
  const from = $("#focusFrom").value;
  const to = $("#focusTo").value;
  [...$("#focusFrom").options].forEach((option) => { option.disabled = Boolean(option.value && to && option.value > to); });
  [...$("#focusTo").options].forEach((option) => { option.disabled = Boolean(option.value && from && option.value < from); });
}

function fillTimelineAssets(values) {
  const current = $("#timelineAsset").value;
  $("#timelineAsset").innerHTML = `<option value="">All classes</option>${values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value.replaceAll("_", " "))}</option>`).join("")}`;
  if (values.includes(current)) $("#timelineAsset").value = current;
}

function renderMovementCounts(counts) {
  $("#changeCounts").innerHTML = CHANGE_ORDER.map((kind) => `<button type="button" class="change-stat ${state.timelineChange === kind ? "active" : ""}" data-change="${kind}" aria-pressed="${state.timelineChange === kind}"><span>${kind}</span><strong>${integer.format(counts[kind] || 0)}</strong></button>`).join("");
}

function sortedTimelineItems() {
  if (!state.timeline) return [];
  const items = [...state.timeline.items];
  if (state.sortDate) {
    const dateIndex = state.timeline.dates.findIndex((item) => item.report_date === state.sortDate);
    items.sort((a, b) => {
      const av = a.points[dateIndex]?.weight;
      const bv = b.points[dateIndex]?.weight;
      if (av == null && bv == null) return a.display_name.localeCompare(b.display_name);
      if (av == null) return 1;
      if (bv == null) return -1;
      return state.sortDirection === "asc" ? av - bv : bv - av;
    });
  } else if (state.timelineSort === "name") {
    items.sort((a, b) => a.display_name.localeCompare(b.display_name));
  } else if (state.timelineSort === "movement") {
    items.sort((a, b) => Math.abs(b.focus.quantity_delta ?? b.focus.market_value_delta ?? 0) - Math.abs(a.focus.quantity_delta ?? a.focus.market_value_delta ?? 0));
  }
  return items;
}

function renderTimeline() {
  if (!state.timeline) return;
  const dates = [...state.timeline.dates].reverse();
  renderTimelineHeaders(dates);
  const items = sortedTimelineItems();
  const rendered = items.slice(0, state.renderLimit);
  $("#timelineBody").innerHTML = rendered.length ? rendered.map(renderTimelineRow).join("") : `<tr><td colspan="${dates.length + 2}" class="empty-state">No instruments match these filters.</td></tr>`;
  $("#timelineSentinel").hidden = state.renderLimit >= items.length;
  requestAnimationFrame(() => {
    syncScrollWidth();
    if (state.autoScrollLatest) {
      jumpToLatest();
      state.autoScrollLatest = false;
    }
  });
}

function renderTimelineHeaders(dates) {
  const focusRange = `${formatMonth(state.timeline.focus.from_date)} → ${formatMonth(state.timeline.focus.to_date)}`;
  $("#focusVisualHead").innerHTML = `Focused Δ<small>${focusRange}</small>`;
  const mobileFocus = `<div class="visual-head mobile-focus-visual">Focused Δ<small>${focusRange}</small></div>`;
  const monthHeaders = dates.map((item, index) => {
    const latest = index === 0;
    const sorted = state.sortDate === item.report_date;
    return `<div class="visual-head month-visual-head ${latest ? "latest" : ""}"><button type="button" data-sort-date="${item.report_date}" aria-label="Sort by ${formatMonth(item.report_date)} portfolio weight">${formatMonth(item.report_date)}<small>${integer.format(item.holding_count)} positions${sorted ? ` · ${state.sortDirection === "desc" ? "↓" : "↑"}` : ""}</small></button></div>`;
  }).join("");
  $("#monthHeaderTrack").innerHTML = `${mobileFocus}${monthHeaders}`;
  $("#timelineCols").innerHTML = `<col class="instrument-col"><col class="focus-col">${dates.map(() => '<col class="month-col">').join("")}`;
  $("#timelineHead").innerHTML = `<tr><th scope="col"><span class="sr-only">Instrument</span></th><th scope="col"><span class="sr-only">Focused movement, ${focusRange}</span></th>${dates.map((item) => `<th scope="col"><span class="sr-only">${formatMonth(item.report_date)} weight, disclosed holding, and monthly activity</span></th>`).join("")}</tr>`;
}

function renderTimelineRow(item) {
  const expanded = state.expandedIdentity === item.identity_key;
  const focus = item.focus;
  const focusDelta = focus.quantity_delta ?? focus.market_value_delta;
  const focusUnit = focus.quantity_delta == null ? " ₹L" : "";
  const displayPoints = [...item.points].reverse();
  const cells = displayPoints.map((point, index) => {
    const latest = index === 0;
    const holding = holdingDescriptor(item, point);
    const movement = movementDisplay(point);
    const weight = point.present ? formatTimelineWeight(point.weight, "Unavailable") : "—";
    const label = `${formatMonth(point.report_date)}, weight ${weight}, holding ${holding.spoken}, ${movement.spoken}`;
    return `<td class="month-cell ${latest ? "latest" : ""}" aria-label="${escapeHtml(label)}"><div class="month-cell-stack"><strong>${weight}</strong><span class="holding-value">${escapeHtml(holding.value)}</span>${movement.html}</div></td>`;
  }).join("");
  const focusLabel = focus.change_type || "not held";
  let focusValue = focusDelta == null ? "—" : `${signedNumber.format(focusDelta)}${focusUnit}`;
  if (focus.change_type === "introduced") {
    const introduced = formatFocusEndpoint(item, focus.quantity_to, focus.market_value_to);
    focusValue = introduced == null ? "New position" : `New · ${introduced}`;
  } else if (focus.change_type === "exited") {
    const exited = formatFocusEndpoint(item, focus.quantity_from, focus.market_value_from);
    focusValue = exited == null ? "Position exited" : `Exit · ${exited}`;
  }
  const row = `<tr class="timeline-row ${expanded ? "expanded" : ""}"><th scope="row" class="instrument-cell"><button type="button" class="instrument-button" data-expand="${escapeHtml(item.identity_key)}" aria-expanded="${expanded}"><span>${escapeHtml(item.display_name)}</span><small>${escapeHtml(item.asset_class.replaceAll("_", " "))}${item.isin ? ` · ${escapeHtml(item.isin)}` : ""}</small></button></th><td class="focus-cell"><span class="change-badge ${focus.change_type || "dormant"}">${focusLabel}</span><strong class="${focusDelta > 0 ? "number-positive" : focusDelta < 0 ? "number-negative" : ""}">${escapeHtml(focusValue)}</strong><small>${formatSignedPercent(focus.weight_delta)}</small></td>${cells}</tr>`;
  return expanded ? `${row}${renderExpandedRow(item)}` : row;
}

function renderExpandedRow(item) {
  const focus = item.focus;
  return `<tr class="detail-row"><td colspan="${item.points.length + 2}"><div class="detail-grid"><div class="detail-identity"><p class="eyebrow">Position detail</p><h3>${escapeHtml(item.display_name)}</h3><p>${escapeHtml(item.instrument_type.replaceAll("_", " "))}${item.industry_rating ? ` · ${escapeHtml(item.industry_rating)}` : ""}</p><dl><div><dt>ISIN</dt><dd>${escapeHtml(item.isin || "—")}</dd></div><div><dt>Section</dt><dd>${escapeHtml(item.section || "—")}</dd></div></dl></div>${detailMetric("Quantity", focus.quantity_from, focus.quantity_to, focus.quantity_delta, formatNumber)}${detailMetric("Market value", focus.market_value_from, focus.market_value_to, focus.market_value_delta, (value) => `${formatNumber(value)} ₹L`)}${detailMetric("Portfolio weight", focus.weight_from, focus.weight_to, focus.weight_delta, formatPercent)}</div></td></tr>`;
}

function detailMetric(label, from, to, delta, formatter) {
  return `<div class="detail-metric"><span>${label}</span><div><small>From</small><strong>${formatter(from)}</strong></div><div><small>To</small><strong>${formatter(to)}</strong></div><div><small>Change</small><strong class="${delta > 0 ? "number-positive" : delta < 0 ? "number-negative" : ""}">${label === "Portfolio weight" ? formatSignedPercent(delta) : delta == null ? "—" : signedInteger.format(delta)}</strong></div></div>`;
}

function renderTimelineEmpty(message) {
  state.timeline = null;
  $("#timelineHead").innerHTML = "";
  $("#timelineCols").innerHTML = "";
  $("#focusVisualHead").innerHTML = "Focused Δ";
  $("#monthHeaderTrack").innerHTML = "";
  $("#timelineBody").innerHTML = `<tr><td class="empty-state">${escapeHtml(message)}</td></tr>`;
  $("#timelineCount").textContent = "";
  $("#changeCounts").innerHTML = "";
  $("#timelineSentinel").hidden = true;
}

function syncScrollWidth() {
  $("#topScrollSpacer").style.width = `${$("#timelineScroll").scrollWidth}px`;
  syncTimelineHeader();
}

function syncTimelineHeader() {
  $("#monthHeaderTrack").style.transform = `translateX(-${$("#timelineScroll").scrollLeft}px)`;
}

function jumpToLatest() {
  const scroll = $("#timelineScroll");
  scroll.scrollLeft = 0;
  $("#topScroll").scrollLeft = scroll.scrollLeft;
  syncTimelineHeader();
}

function updateExportLinks() {
  const fund = $("#timelineFund").value;
  if (!fund || !state.timeline) return;
  $("#timelineExport").href = `/api/funds/${fund}/timeline.csv?${timelineParams()}`;
  const focusParams = new URLSearchParams({
    from_date: state.timeline.focus.from_date,
    to_date: state.timeline.focus.to_date,
    search: $("#timelineSearch").value.trim(),
    asset_class: $("#timelineAsset").value,
    change_type: state.timelineChange,
  });
  $("#focusExport").href = `/api/funds/${fund}/comparison.csv?${focusParams}`;
}

async function openCoverageCell(button) {
  const fund = state.funds.find((item) => String(item.id) === String(button.dataset.fund));
  if (!fund) return;
  $("#timelineAmc").value = fund.amc_slug;
  populateTimelineFunds(String(fund.id));
  state.timelinePeriod = "6m";
  setPeriod("6m", false);
  state.autoScrollLatest = true;
  await loadTimeline({ resetFocus: true });
  const index = state.timeline?.dates.findIndex((item) => item.report_date === button.dataset.date) ?? -1;
  if (index >= 0) {
    $("#focusTo").value = state.timeline.dates[index].report_date;
    $("#focusFrom").value = state.timeline.dates[Math.max(0, index - 1)].report_date;
    constrainFocusOptions();
    await loadTimeline();
  }
  $("#portfolioTimeline").scrollIntoView({ behavior: "smooth", block: "start" });
  $("#timelineHeading").focus?.();
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
    if (action !== "validate") {
      toast(`${item.file.name} imported.`);
      await loadReferenceData(true);
      await Promise.all([loadOverview(), loadImports(), initializeTimeline()]);
    }
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

function renderEmpty(target, colspan, message) {
  target.innerHTML = `<tr><td colspan="${colspan}" class="empty-state">${escapeHtml(message)}</td></tr>`;
}

function renderWorkspaceStatus() {
  if (!state.health) return;
  const rows = [["Service", "Online"], ["Database", state.health.database], ["Schema", `v${state.health.schema_version}`], ["Source archive", state.health.archive]];
  $("#workspaceStatus").innerHTML = rows.map(([term, value]) => `<div><dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
}

function debounce(callback, delay = 250) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => callback(...args), delay); };
}

function setupScrollObservers() {
  const sectionObserver = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    $$(".section-tabs a").forEach((link) => link.setAttribute("aria-current", link.dataset.section === visible.target.id ? "page" : "false"));
  }, { rootMargin: "-18% 0px -68%", threshold: [0, 0.1, 0.3] });
  $$(".workspace-section").forEach((section) => sectionObserver.observe(section));

  const sentinelObserver = new IntersectionObserver((entries) => {
    if (!entries.some((entry) => entry.isIntersecting) || !state.timeline) return;
    const total = sortedTimelineItems().length;
    if (state.renderLimit < total) {
      state.renderLimit += 100;
      renderTimeline();
    }
  }, { rootMargin: "500px" });
  sentinelObserver.observe($("#timelineSentinel"));
}

$("#overviewAmc").addEventListener("change", loadOverview);
$("#timelineAmc").addEventListener("change", () => { populateTimelineFunds(); state.autoScrollLatest = true; loadTimeline({ resetFocus: true }); });
$("#timelineFund").addEventListener("change", () => { state.autoScrollLatest = true; loadTimeline({ resetFocus: true }); });
$("#periodButtons").addEventListener("click", (event) => { const button = event.target.closest("button[data-period]"); if (button) setPeriod(button.dataset.period); });
$("#focusFrom").addEventListener("change", () => { constrainFocusOptions(); loadTimeline(); });
$("#focusTo").addEventListener("change", () => { constrainFocusOptions(); loadTimeline(); });
$("#timelineSearch").addEventListener("input", debounce(() => loadTimeline()));
$("#timelineAsset").addEventListener("change", () => loadTimeline());
$("#timelineSort").addEventListener("change", () => { state.timelineSort = $("#timelineSort").value; state.sortDate = ""; localStorage.setItem("mfTracker.sort", state.timelineSort); renderTimeline(); });
$("#changeCounts").addEventListener("click", (event) => { const button = event.target.closest("button[data-change]"); if (!button) return; state.timelineChange = state.timelineChange === button.dataset.change ? "" : button.dataset.change; loadTimeline(); });
$("#timelineHeaderShell").addEventListener("click", (event) => { const button = event.target.closest("button[data-sort-date]"); if (!button) return; if (state.sortDate === button.dataset.sortDate) state.sortDirection = state.sortDirection === "desc" ? "asc" : "desc"; else { state.sortDate = button.dataset.sortDate; state.sortDirection = "desc"; } renderTimeline(); });
$("#timelineBody").addEventListener("click", (event) => { const button = event.target.closest("button[data-expand]"); if (!button) return; state.expandedIdentity = state.expandedIdentity === button.dataset.expand ? "" : button.dataset.expand; renderTimeline(); });
$("#jumpLatest").addEventListener("click", jumpToLatest);
$("#coverageMatrix").addEventListener("click", (event) => { const button = event.target.closest("button[data-fund]"); if (button) openCoverageCell(button); });
$("#importShortcut").addEventListener("click", () => { $("#imports").scrollIntoView({ behavior: "smooth", block: "start" }); $("#browseFiles").focus({ preventScroll: true }); });
$("#retryConnection").addEventListener("click", async () => { if (await checkHealth()) await Promise.all([loadOverview(), initializeTimeline(), loadImports()]); });

let syncingScroll = false;
$("#timelineScroll").addEventListener("scroll", () => { syncTimelineHeader(); if (syncingScroll) return; syncingScroll = true; $("#topScroll").scrollLeft = $("#timelineScroll").scrollLeft; syncingScroll = false; });
$("#topScroll").addEventListener("scroll", () => { if (syncingScroll) return; syncingScroll = true; $("#timelineScroll").scrollLeft = $("#topScroll").scrollLeft; syncTimelineHeader(); syncingScroll = false; });
window.addEventListener("resize", syncScrollWidth);

$("#browseFiles").addEventListener("click", () => $("#fileInput").click());
$("#fileInput").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
const dropZone = $("#dropZone");
["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add("dragging"); }));
["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove("dragging"); }));
dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
$("#fileQueue").addEventListener("click", (event) => { const button = event.target.closest("[data-action]"); if (button) queueAction(button.dataset.id, button.dataset.action); });
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

window.addEventListener("resize", syncScrollWidth);
setupScrollObservers();
const connected = await checkHealth();
if (connected) {
  const results = await Promise.allSettled([loadOverview(), initializeTimeline(), loadImports()]);
  results.filter((result) => result.status === "rejected").forEach((result) => toast(result.reason.message, "error"));
}
