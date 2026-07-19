const API_BASE = "http://127.0.0.1:8000";
const PAGE_SIZE = 50;

const state = {
  active: null,
  current: null,
  snapshots: [],
  targetWeights: {},
  rebalanceResult: null,
  saveTimer: null,
  saveInFlight: null,
  dirty: false,
  busy: false,
  kite: { configured: false, authenticated: false, login_time: null },
  allocationSearch: "",
  allocationPage: 1,
  tradePage: 1,
  expandedSymbols: new Set(),
};

const elementIds = [
  "connectionBanner", "retryConnection", "portfolioMeta", "portfolioSource", "saveStatus",
  "priceMetadata", "headerActions", "updateHoldingsButton", "overflowButton", "actionMenu",
  "refreshPricesButton", "exportBackupButton", "kiteDisconnectButton", "historicalBanner",
  "returnActiveButton", "restoreButton", "emptyState", "emptyUpdateButton", "importBackupLabel",
  "backupFile", "workspace", "totalMarketValue", "totalCost", "unrealizedPnl", "holdingCount",
  "quickActions", "useCurrentButton", "distributeButton", "clearTargetsButton", "allocationTools",
  "allocationSearch", "allocationResultCount", "holdingsBody", "allocationPagination",
  "allocationTotal", "allocationTotalLabel", "allocationValidation", "allocationProgress",
  "freshCash", "cashHelp", "roundingMode", "rebalanceButton", "previewPanel", "exportButton",
  "totalBuy", "totalSell", "netCash", "cashDeltaLabel", "cashDelta", "rebalanceBody",
  "tradePagination", "updateDialog", "portfolioFile", "selectedFileName", "uploadButton",
  "kiteStatus", "kitePrimaryButton", "dialogMessage", "historyButton", "historyDialog",
  "historyList", "confirmDialog", "cancelClearButton", "confirmClearButton", "toastRegion",
];
const elements = Object.fromEntries(elementIds.map((id) => [id, document.querySelector(`#${id}`)]));

const currency = new Intl.NumberFormat("en-IN", {
  style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const decimal = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const quantity = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 4 });

function formatMoney(value) { return currency.format(Number(value || 0)); }
function formatPct(value) { return `${decimal.format(Number(value || 0))}%`; }
function formatSignedMoney(value) {
  const numberValue = Number(value || 0);
  return `${numberValue > 0 ? "+" : ""}${formatMoney(numberValue)}`;
}
function formatDate(value) {
  return new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function create(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function showToast(message, type = "info") {
  const toast = create("div", `toast ${type === "error" ? "error" : ""}`, message);
  elements.toastRegion.appendChild(toast);
  window.setTimeout(() => toast.remove(), 4500);
}

function setDialogMessage(message = "") {
  elements.dialogMessage.textContent = message;
  elements.dialogMessage.hidden = !message;
}

function setSaveStatus(status, text) {
  elements.saveStatus.className = `save-indicator ${status || ""}`;
  elements.saveStatus.querySelector("span").textContent = text;
}

async function apiFetch(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg).join("; ")
      : detail?.message || detail;
    throw new Error(message || `Request failed with ${response.status}`);
  }
  return payload;
}

async function initialize() {
  setBusy(true);
  try {
    const [, workspace, kiteStatus] = await Promise.all([
      apiFetch("/api/health"), apiFetch("/api/portfolio"), apiFetch("/api/kite/status"),
    ]);
    elements.connectionBanner.hidden = true;
    state.kite = kiteStatus;
    applyWorkspace(workspace);
    renderKiteStatus();
    const callback = new URLSearchParams(window.location.search).get("kite");
    if (callback) {
      window.history.replaceState({}, "", window.location.pathname);
      showToast(callback === "connected"
        ? "Kite connected. Import holdings when you are ready."
        : "Kite login was not completed.", callback === "connected" ? "info" : "error");
      if (callback === "connected") openDialog(elements.updateDialog);
    }
  } catch (error) {
    elements.connectionBanner.hidden = false;
    showToast(error.message, "error");
    renderWorkspaceState();
  } finally {
    setBusy(false);
  }
}

function applyWorkspace(workspace) {
  state.active = workspace.active;
  state.snapshots = workspace.snapshots || [];
  setPortfolio(workspace.active);
  renderHistory();
  renderKiteStatus();
}

function setPortfolio(portfolio) {
  state.current = portfolio;
  state.targetWeights = Object.fromEntries(
    (portfolio?.target_weights || []).map((item) => [item.symbol, Number(item.target_weight_pct)]),
  );
  state.dirty = false;
  state.allocationPage = 1;
  state.tradePage = 1;
  state.expandedSymbols.clear();
  clearTimeout(state.saveTimer);
  elements.freshCash.value = portfolio?.fresh_cash ?? 0;
  elements.roundingMode.value = portfolio?.rounding_mode ?? "nearest";
  setSaveStatus("", "Saved locally");
  clearRebalance();
  renderWorkspaceState();
}

function renderWorkspaceState() {
  const hasPortfolio = Boolean(state.current);
  elements.emptyState.hidden = hasPortfolio;
  elements.workspace.hidden = !hasPortfolio;
  elements.portfolioMeta.hidden = !hasPortfolio;
  elements.headerActions.hidden = !hasPortfolio;
  elements.historicalBanner.hidden = !hasPortfolio || state.current.is_active;
  if (!hasPortfolio) {
    elements.portfolioSource.textContent = "No active portfolio";
    updateControls();
    return;
  }
  renderHeader();
  renderSummary();
  renderHoldings();
  renderAllocationTotal();
  renderRebalance();
  renderHistory();
  updateControls();
}

function renderHeader() {
  const current = state.current;
  const sourceName = current.source.source_type === "kite" ? "Zerodha holdings" : current.source.filename;
  elements.portfolioSource.textContent = sourceName;
  elements.priceMetadata.textContent = current.latest_price_at
    ? `Prices updated ${formatDate(current.latest_price_at)}`
    : `Imported ${formatDate(current.source.imported_at)}`;
}

function renderSummary() {
  const summary = state.current?.summary;
  elements.totalMarketValue.textContent = summary ? formatMoney(summary.total_market_value) : "—";
  elements.totalCost.textContent = summary ? formatMoney(summary.total_cost) : "—";
  elements.holdingCount.textContent = summary?.holding_count ?? "—";
  elements.unrealizedPnl.className = "";
  if (!summary) {
    elements.unrealizedPnl.textContent = "—";
    return;
  }
  elements.unrealizedPnl.textContent = `${formatSignedMoney(summary.unrealized_pnl)} · ${summary.unrealized_pnl_pct > 0 ? "+" : ""}${formatPct(summary.unrealized_pnl_pct)}`;
  if (summary.unrealized_pnl > 0) elements.unrealizedPnl.classList.add("positive");
  if (summary.unrealized_pnl < 0) elements.unrealizedPnl.classList.add("negative");
}

function allocationValues() {
  return (state.current?.holdings || []).map((holding) => Number(state.targetWeights[holding.symbol] ?? 0));
}

function allocationStatus() {
  const values = allocationValues();
  const total = values.reduce((sum, value) => sum + (Number.isFinite(value) ? value : 0), 0);
  const individualValid = values.every((value) => Number.isFinite(value) && value >= 0 && value <= 100);
  const cash = Number(elements.freshCash.value);
  return {
    total,
    targetsValid: individualValid && total <= 100.000001,
    cashValid: Number.isFinite(cash) && cash >= 0,
  };
}

function renderAllocationTotal() {
  if (!state.current) return;
  const { total, targetsValid, cashValid } = allocationStatus();
  const cashWeight = Math.max(0, 100 - total);
  elements.allocationTotal.classList.toggle("invalid", !targetsValid);
  elements.allocationProgress.style.width = `${Math.min(100, Math.max(0, total))}%`;
  if (!targetsValid) {
    const over = Math.max(0, total - 100);
    elements.allocationTotalLabel.textContent = `${decimal.format(total)}% allocated`;
    elements.allocationValidation.textContent = over > 0
      ? `${decimal.format(over)}% over the allocation limit.`
      : "Each target must be between 0% and 100%.";
  } else if (total === 0) {
    elements.allocationTotalLabel.textContent = "0.00% invested · 100.00% cash";
    elements.allocationValidation.textContent = "A preview at 0% sells all holdings into cash.";
  } else if (Math.abs(total - 100) < .000001) {
    elements.allocationTotalLabel.textContent = "100.00% invested";
    elements.allocationValidation.textContent = "The target portfolio is fully invested.";
  } else {
    elements.allocationTotalLabel.textContent = `${decimal.format(total)}% invested · ${decimal.format(cashWeight)}% cash`;
    elements.allocationValidation.textContent = "Unallocated target weight remains as cash.";
  }
  elements.cashHelp.classList.toggle("error", !cashValid);
  elements.cashHelp.textContent = cashValid
    ? "Deposit-only cash available for this plan."
    : "Fresh cash must be zero or greater.";
  updateControls();
}

function filteredHoldings() {
  const holdings = state.current?.holdings || [];
  const query = state.allocationSearch.trim().toLowerCase();
  return query ? holdings.filter((holding) => holding.symbol.toLowerCase().includes(query)) : holdings;
}

function renderHoldings() {
  const holdings = filteredHoldings();
  const allCount = state.current?.holdings?.length || 0;
  elements.holdingsBody.replaceChildren();
  elements.allocationTools.hidden = allCount <= PAGE_SIZE;
  elements.allocationResultCount.textContent = `${holdings.length.toLocaleString("en-IN")} holding${holdings.length === 1 ? "" : "s"}`;
  if (!holdings.length) {
    elements.holdingsBody.appendChild(create("div", "empty-row", allCount ? "No symbols match this search." : "This portfolio has no equity holdings."));
    renderPagination(elements.allocationPagination, 0, 1, "allocation");
    return;
  }
  const totalPages = Math.max(1, Math.ceil(holdings.length / PAGE_SIZE));
  state.allocationPage = Math.min(state.allocationPage, totalPages);
  const start = (state.allocationPage - 1) * PAGE_SIZE;
  const fragment = document.createDocumentFragment();
  holdings.slice(start, start + PAGE_SIZE).forEach((holding) => {
    fragment.appendChild(buildAllocationRow(holding));
    fragment.appendChild(buildHoldingDetail(holding));
  });
  elements.holdingsBody.appendChild(fragment);
  renderPagination(elements.allocationPagination, totalPages, state.allocationPage, "allocation");
}

function buildAllocationRow(holding) {
  const row = create("div", "allocation-row");
  row.dataset.symbol = holding.symbol;

  const symbolCell = create("div", "symbol-cell");
  const symbolButton = create("button", "symbol-button");
  symbolButton.type = "button";
  symbolButton.dataset.action = "toggle-details";
  symbolButton.dataset.symbol = holding.symbol;
  symbolButton.setAttribute("aria-expanded", String(state.expandedSymbols.has(holding.symbol)));
  symbolButton.setAttribute("aria-label", `Show details for ${holding.symbol}`);
  symbolButton.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 3 5 5-5 5"/></svg>';
  symbolButton.appendChild(create("strong", "", holding.symbol));
  symbolCell.appendChild(symbolButton);
  row.appendChild(symbolCell);
  row.appendChild(create("span", "money-cell", formatMoney(holding.market_value)));
  row.appendChild(create("span", "weight-cell", formatPct(holding.current_weight_pct)));

  const targetField = create("label", "target-field");
  const input = document.createElement("input");
  input.className = "target-input";
  input.type = "number";
  input.min = "0";
  input.max = "100";
  input.step = "0.01";
  input.inputMode = "decimal";
  input.value = Number(state.targetWeights[holding.symbol] ?? 0).toFixed(2);
  input.dataset.symbol = holding.symbol;
  input.setAttribute("aria-label", `Target weight for ${holding.symbol}`);
  input.disabled = !state.current.is_active || state.busy;
  targetField.append(input, create("span", "", "%"));
  row.appendChild(targetField);

  const target = Number(state.targetWeights[holding.symbol] ?? 0);
  const difference = target - holding.current_weight_pct;
  const aligned = Math.abs(difference) < .005;
  const differenceCell = create("span", "difference-cell");
  differenceCell.appendChild(create("strong", "", aligned ? "0.00%" : `${difference > 0 ? "+" : ""}${formatPct(difference)}`));
  differenceCell.appendChild(create("span", "", aligned ? "Aligned" : difference > 0 ? "Increase" : "Decrease"));
  row.appendChild(differenceCell);

  const compareCell = create("span", "compare-cell");
  const labels = create("span", "compare-labels");
  labels.append(create("span", "", formatPct(holding.current_weight_pct)), create("span", "", formatPct(target)));
  const track = create("span", "compare-track");
  const current = create("span", "current");
  current.style.width = `${Math.min(100, Math.max(0, holding.current_weight_pct))}%`;
  const marker = create("span", "target");
  marker.style.left = `calc(${Math.min(100, Math.max(0, target))}% - 1px)`;
  track.append(current, marker);
  compareCell.append(labels, track);
  row.appendChild(compareCell);
  return row;
}

function buildHoldingDetail(holding) {
  const detail = create("div", "holding-detail");
  detail.dataset.detailFor = holding.symbol;
  detail.hidden = !state.expandedSymbols.has(holding.symbol);
  const facts = [
    ["Quantity", quantity.format(holding.quantity)],
    ["Average price", formatMoney(holding.avg_price)],
    ["LTP", formatMoney(holding.ltp)],
    ["Unrealized P&L", `${formatSignedMoney(holding.unrealized_pnl)} · ${holding.unrealized_pnl_pct > 0 ? "+" : ""}${formatPct(holding.unrealized_pnl_pct)}`],
  ];
  facts.forEach(([label, value]) => {
    const fact = create("span", "", label);
    fact.appendChild(create("strong", "", value));
    detail.appendChild(fact);
  });
  return detail;
}

function renderPagination(container, totalPages, currentPage, kind) {
  container.replaceChildren();
  container.hidden = totalPages <= 1;
  if (totalPages <= 1) return;
  const previous = create("button", "", "Previous");
  previous.type = "button";
  previous.dataset.pageKind = kind;
  previous.dataset.page = String(currentPage - 1);
  previous.disabled = currentPage === 1;
  const label = create("span", "", `Page ${currentPage} of ${totalPages}`);
  const next = create("button", "", "Next");
  next.type = "button";
  next.dataset.pageKind = kind;
  next.dataset.page = String(currentPage + 1);
  next.disabled = currentPage === totalPages;
  container.append(previous, label, next);
}

function renderHistory() {
  elements.historyList.replaceChildren();
  const fragment = document.createDocumentFragment();
  state.snapshots.forEach((snapshot) => {
    const button = create("button", "history-item");
    button.type = "button";
    button.dataset.snapshotId = snapshot.snapshot_id;
    if (snapshot.snapshot_id === state.current?.snapshot_id) button.classList.add("current");
    const copy = create("span");
    copy.append(create("strong", "", snapshot.filename), create("span", "", formatDate(snapshot.created_at)));
    const badge = create("span", `history-badge ${snapshot.lifecycle_status === "active" ? "active" : ""}`, snapshot.lifecycle_status);
    button.append(copy, badge);
    fragment.appendChild(button);
  });
  elements.historyList.appendChild(fragment);
}

function clearRebalance() {
  state.rebalanceResult = null;
  state.tradePage = 1;
  elements.previewPanel.hidden = true;
  elements.rebalanceBody.replaceChildren();
  updateControls();
}

function renderRebalance() {
  const result = state.rebalanceResult;
  elements.previewPanel.hidden = !result;
  if (!result) return;
  const cash = result.cash_impact;
  elements.totalBuy.textContent = formatMoney(cash.total_buy_value);
  elements.totalSell.textContent = formatMoney(cash.total_sell_value);
  elements.netCash.textContent = formatMoney(cash.net_cash_required);
  elements.cashDeltaLabel.textContent = cash.cash_surplus_or_shortfall < 0 ? "Cash shortfall" : "Cash remaining";
  elements.cashDelta.textContent = formatMoney(Math.abs(cash.cash_surplus_or_shortfall));
  [elements.totalBuy, elements.totalSell, elements.netCash, elements.cashDelta].forEach((element) => { element.className = ""; });
  elements.totalBuy.classList.add("positive");
  elements.totalSell.classList.add("negative");
  elements.cashDelta.classList.add(cash.cash_surplus_or_shortfall < 0 ? "negative" : "positive");

  elements.rebalanceBody.replaceChildren();
  const rows = result.rows || [];
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.tradePage = Math.min(state.tradePage, totalPages);
  const start = (state.tradePage - 1) * PAGE_SIZE;
  const fragment = document.createDocumentFragment();
  rows.slice(start, start + PAGE_SIZE).forEach((item) => {
    const row = create("div", "trade-row");
    row.appendChild(create("span", "", item.symbol));
    const actionCell = create("span");
    actionCell.appendChild(create("span", `badge ${item.action.toLowerCase()}`, item.action.toLowerCase()));
    row.appendChild(actionCell);
    row.appendChild(create("span", "", `${item.trade_qty > 0 ? "+" : ""}${quantity.format(item.trade_qty)}`));
    row.appendChild(create("span", "", formatMoney(item.ltp)));
    row.appendChild(create("span", "", formatMoney(Math.abs(item.trade_value))));
    row.appendChild(create("span", "", item.action === "BUY" ? "Increase to target" : item.action === "SELL" ? "Reduce to target" : "No trade required"));
    fragment.appendChild(row);
  });
  elements.rebalanceBody.appendChild(fragment);
  renderPagination(elements.tradePagination, totalPages, state.tradePage, "trade");
}

function workingStatePayload() {
  return {
    target_weights: (state.current?.holdings || []).map((holding) => ({
      symbol: holding.symbol,
      target_weight_pct: Number(state.targetWeights[holding.symbol] || 0),
    })),
    fresh_cash: Number(elements.freshCash.value || 0),
    rounding_mode: elements.roundingMode.value,
  };
}

function scheduleSave() {
  if (!state.current?.is_active) return;
  state.dirty = true;
  clearTimeout(state.saveTimer);
  const { targetsValid, cashValid } = allocationStatus();
  if (!targetsValid || !cashValid) {
    setSaveStatus("error", "Fix values to save");
    return;
  }
  setSaveStatus("saving", "Unsaved changes");
  state.saveTimer = window.setTimeout(() => flushWorkingState().catch(() => {}), 500);
}

async function flushWorkingState() {
  clearTimeout(state.saveTimer);
  if (state.saveInFlight) await state.saveInFlight;
  if (!state.dirty || !state.current?.is_active) return;
  const { targetsValid, cashValid } = allocationStatus();
  if (!targetsValid || !cashValid) return;
  const snapshotId = state.current.snapshot_id;
  const payload = workingStatePayload();
  state.dirty = false;
  setSaveStatus("saving", "Saving…");
  state.saveInFlight = apiFetch(`/api/portfolio/snapshots/${snapshotId}/working-state`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  try {
    const saved = await state.saveInFlight;
    state.active = saved;
    setSaveStatus(state.dirty ? "saving" : "", state.dirty ? "Unsaved changes" : "Saved locally");
  } catch (error) {
    state.dirty = true;
    setSaveStatus("error", "Save failed");
    showToast(error.message, "error");
    throw error;
  } finally {
    state.saveInFlight = null;
  }
  if (state.dirty) return flushWorkingState();
}

function applyTargets(targets) {
  state.targetWeights = { ...targets };
  clearRebalance();
  renderHoldings();
  renderAllocationTotal();
  scheduleSave();
}

function apportionToHundred(values) {
  if (!values.length) return [];
  const total = values.reduce((sum, value) => sum + Math.max(0, value), 0);
  if (total <= 0) return values.map(() => 0);
  const raw = values.map((value) => Math.max(0, value) / total * 10000);
  const basisPoints = raw.map(Math.floor);
  let remainder = 10000 - basisPoints.reduce((sum, value) => sum + value, 0);
  const order = raw.map((value, index) => ({ index, fraction: value - Math.floor(value) }))
    .sort((a, b) => b.fraction - a.fraction || a.index - b.index);
  for (let index = 0; index < remainder; index += 1) basisPoints[order[index].index] += 1;
  return basisPoints.map((value) => value / 100);
}

function useCurrentAllocation() {
  const holdings = state.current?.holdings || [];
  const weights = apportionToHundred(holdings.map((holding) => holding.current_weight_pct));
  applyTargets(Object.fromEntries(holdings.map((holding, index) => [holding.symbol, weights[index]])));
  showToast("Targets matched to the current allocation.");
}

function distributeEqually() {
  const holdings = state.current?.holdings || [];
  const weights = apportionToHundred(holdings.map(() => 1));
  applyTargets(Object.fromEntries(holdings.map((holding, index) => [holding.symbol, weights[index]])));
  showToast("Targets distributed equally.");
}

async function uploadPortfolio() {
  const file = elements.portfolioFile.files[0];
  if (!file) return setDialogMessage("Choose a CSV, XLSX, or XLS file first.");
  try {
    await flushWorkingState();
    setBusy(true);
    setDialogMessage();
    const formData = new FormData();
    formData.append("file", file);
    const result = await apiFetch("/api/portfolio/upload", { method: "POST", body: formData });
    applyWorkspace(result.workspace);
    elements.updateDialog.close();
    showToast(result.status === "no_op" ? "This portfolio is already active." : "Portfolio imported and saved locally.");
  } catch (error) {
    setDialogMessage(error.message);
  } finally {
    setBusy(false);
  }
}

function renderKiteStatus() {
  const kite = state.kite;
  const activeKite = state.active?.source?.source_type === "kite";
  elements.kiteStatus.classList.toggle("ok", kite.authenticated);
  elements.kiteDisconnectButton.hidden = !kite.authenticated;
  if (!kite.configured) {
    elements.kiteStatus.textContent = "Not configured";
    elements.kitePrimaryButton.textContent = "Kite unavailable";
    elements.kitePrimaryButton.disabled = true;
  } else if (!kite.authenticated) {
    elements.kiteStatus.textContent = "Disconnected";
    elements.kitePrimaryButton.textContent = "Connect Kite";
    elements.kitePrimaryButton.disabled = state.busy;
  } else {
    elements.kiteStatus.textContent = "Connected";
    elements.kitePrimaryButton.textContent = activeKite ? "Refresh from Kite" : "Import from Kite";
    elements.kitePrimaryButton.disabled = state.busy;
  }
}

async function connectKite() {
  try {
    setBusy(true);
    const payload = await apiFetch("/api/kite/session", { method: "POST" });
    window.top.location.assign(payload.login_url);
  } catch (error) {
    setDialogMessage(error.message);
    setBusy(false);
  }
}

async function syncKite() {
  try {
    await flushWorkingState();
    setBusy(true);
    const result = await apiFetch("/api/kite/holdings/sync", { method: "POST" });
    applyWorkspace(result.workspace);
    elements.updateDialog.close();
    showToast(result.status === "snapshot_created" ? "Kite holdings imported as a new snapshot." : "Kite prices refreshed.");
  } catch (error) {
    if (/session|connect/i.test(error.message)) state.kite.authenticated = false;
    setDialogMessage(error.message);
    renderKiteStatus();
  } finally {
    setBusy(false);
  }
}

function handleKitePrimary() { return state.kite.authenticated ? syncKite() : connectKite(); }

async function disconnectKite() {
  try {
    setBusy(true);
    await apiFetch("/api/kite/session", { method: "DELETE" });
    state.kite = await apiFetch("/api/kite/status");
    renderKiteStatus();
    showToast("Kite disconnected for this backend session.");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function viewSnapshot(snapshotId) {
  if (snapshotId === state.current?.snapshot_id) return elements.historyDialog.close();
  try {
    await flushWorkingState();
    setBusy(true);
    const portfolio = await apiFetch(`/api/portfolio/snapshots/${snapshotId}`);
    setPortfolio(portfolio);
    elements.historyDialog.close();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function returnToActive() {
  try {
    setBusy(true);
    applyWorkspace(await apiFetch("/api/portfolio"));
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function restoreSnapshot() {
  if (!state.current || !window.confirm("Restore this historical snapshot as a new active portfolio?")) return;
  try {
    setBusy(true);
    applyWorkspace(await apiFetch(`/api/portfolio/snapshots/${state.current.snapshot_id}/restore`, { method: "POST" }));
    showToast("Historical snapshot restored as a new active portfolio.");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function refreshPrices() {
  closeActionMenu();
  if (state.current?.source?.source_type === "kite") {
    if (!state.kite.authenticated) return openDialog(elements.updateDialog);
    return syncKite();
  }
  try {
    await flushWorkingState();
    setBusy(true);
    const payload = await apiFetch(`/api/portfolio/snapshots/${state.current.snapshot_id}/refresh-prices`, { method: "POST" });
    setPortfolio(payload.portfolio);
    const failures = payload.prices.filter((item) => !item.success);
    showToast(failures.length
      ? `Prices refreshed with ${failures.length} warning${failures.length === 1 ? "" : "s"}.`
      : "Prices refreshed and saved.", failures.length ? "error" : "info");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function calculateRebalance() {
  const { targetsValid, cashValid } = allocationStatus();
  if (!targetsValid || !cashValid) return;
  try {
    setBusy(true);
    state.rebalanceResult = await apiFetch(`/api/portfolio/snapshots/${state.current.snapshot_id}/rebalance`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(workingStatePayload()),
    });
    state.dirty = false;
    state.active = { ...state.current, ...workingStatePayload() };
    setSaveStatus("", "Saved locally");
    renderRebalance();
    elements.previewPanel.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    showToast("Trade preview calculated from current prices.");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function exportBackup() {
  closeActionMenu();
  try {
    const response = await fetch(`${API_BASE}/api/portfolio/bundles/export`);
    if (!response.ok) throw new Error("Could not export portfolio backup");
    downloadBlob(await response.blob(), "portfolio-manager-backup.zip");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function importBackup() {
  const file = elements.backupFile.files[0];
  if (!file || state.active) return;
  try {
    setBusy(true);
    const formData = new FormData();
    formData.append("file", file);
    applyWorkspace(await apiFetch("/api/portfolio/bundles/import", { method: "POST", body: formData }));
    showToast("Portfolio backup imported successfully.");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements.backupFile.value = "";
    setBusy(false);
  }
}

function exportTradesCsv() {
  if (!state.rebalanceResult?.rows?.length) return;
  const columns = ["symbol", "action", "trade_qty", "trade_value", "current_weight_pct", "target_weight_pct", "final_weight_pct", "weight_drift_pct"];
  const lines = [columns.join(","), ...state.rebalanceResult.rows.map((row) => columns.map((column) => csvCell(row[column] ?? "")).join(","))];
  downloadBlob(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" }), "rebalance-trades.csv");
}

function csvCell(value) {
  const text = String(value);
  return JSON.stringify(/^[=+\-@]/.test(text) ? `'${text}` : text);
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function updateControls() {
  const editable = Boolean(state.current?.is_active);
  const hasHoldings = Boolean(state.current?.holdings?.length);
  const status = state.current ? allocationStatus() : { targetsValid: false, cashValid: false };
  const disabled = state.busy || !editable;
  elements.rebalanceButton.disabled = disabled || !hasHoldings || !status.targetsValid || !status.cashValid;
  elements.freshCash.disabled = disabled;
  elements.roundingMode.disabled = disabled;
  elements.useCurrentButton.disabled = disabled || !hasHoldings;
  elements.distributeButton.disabled = disabled || !hasHoldings;
  elements.clearTargetsButton.disabled = disabled || !hasHoldings;
  elements.refreshPricesButton.disabled = disabled;
  elements.restoreButton.disabled = state.busy;
  elements.exportButton.disabled = state.busy || !state.rebalanceResult?.rows?.length;
  elements.holdingsBody.querySelectorAll(".target-input").forEach((input) => {
    input.disabled = disabled;
  });
}

function setBusy(busy) {
  state.busy = busy;
  document.body.setAttribute("aria-busy", String(busy));
  elements.uploadButton.disabled = busy;
  elements.updateHoldingsButton.disabled = busy;
  elements.emptyUpdateButton.disabled = busy;
  renderKiteStatus();
  updateControls();
}

function openDialog(dialog) {
  setDialogMessage();
  if (!dialog.open) dialog.showModal();
}

function closeActionMenu() {
  elements.actionMenu.hidden = true;
  elements.overflowButton.setAttribute("aria-expanded", "false");
}

elements.retryConnection.addEventListener("click", initialize);
elements.updateHoldingsButton.addEventListener("click", () => openDialog(elements.updateDialog));
elements.emptyUpdateButton.addEventListener("click", () => openDialog(elements.updateDialog));
elements.historyButton.addEventListener("click", () => { renderHistory(); openDialog(elements.historyDialog); });
elements.overflowButton.addEventListener("click", () => {
  const willOpen = elements.actionMenu.hidden;
  elements.actionMenu.hidden = !willOpen;
  elements.overflowButton.setAttribute("aria-expanded", String(willOpen));
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".overflow-wrap")) closeActionMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeActionMenu();
});
document.querySelectorAll(".dialog-close").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog").close());
});
document.querySelectorAll("dialog.sheet-dialog").forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

elements.portfolioFile.addEventListener("change", () => {
  elements.selectedFileName.textContent = elements.portfolioFile.files[0]?.name || "Choose portfolio file";
  setDialogMessage();
});
elements.uploadButton.addEventListener("click", uploadPortfolio);
elements.kitePrimaryButton.addEventListener("click", handleKitePrimary);
elements.kiteDisconnectButton.addEventListener("click", disconnectKite);
elements.refreshPricesButton.addEventListener("click", refreshPrices);
elements.exportBackupButton.addEventListener("click", exportBackup);
elements.backupFile.addEventListener("change", importBackup);
elements.returnActiveButton.addEventListener("click", returnToActive);
elements.restoreButton.addEventListener("click", restoreSnapshot);
elements.rebalanceButton.addEventListener("click", calculateRebalance);
elements.exportButton.addEventListener("click", exportTradesCsv);
elements.useCurrentButton.addEventListener("click", useCurrentAllocation);
elements.distributeButton.addEventListener("click", distributeEqually);
elements.clearTargetsButton.addEventListener("click", () => openDialog(elements.confirmDialog));
elements.cancelClearButton.addEventListener("click", () => elements.confirmDialog.close());
elements.confirmClearButton.addEventListener("click", () => {
  const targets = Object.fromEntries((state.current?.holdings || []).map((holding) => [holding.symbol, 0]));
  elements.confirmDialog.close();
  applyTargets(targets);
  showToast("All targets cleared. The target portfolio is now 100% cash.");
});

elements.allocationSearch.addEventListener("input", (event) => {
  state.allocationSearch = event.target.value;
  state.allocationPage = 1;
  renderHoldings();
});
elements.holdingsBody.addEventListener("input", (event) => {
  if (!event.target.matches(".target-input")) return;
  const value = event.target.value === "" ? 0 : Number(event.target.value);
  state.targetWeights[event.target.dataset.symbol] = value;
  event.target.setAttribute("aria-invalid", String(!Number.isFinite(value) || value < 0 || value > 100));
  clearRebalance();
  renderAllocationTotal();
  scheduleSave();
});
elements.holdingsBody.addEventListener("click", (event) => {
  const button = event.target.closest('[data-action="toggle-details"]');
  if (!button) return;
  const symbol = button.dataset.symbol;
  const isExpanded = !state.expandedSymbols.has(symbol);
  if (isExpanded) state.expandedSymbols.add(symbol); else state.expandedSymbols.delete(symbol);
  button.setAttribute("aria-expanded", String(isExpanded));
  button.setAttribute("aria-label", `${isExpanded ? "Hide" : "Show"} details for ${symbol}`);
  const detail = elements.holdingsBody.querySelector(`[data-detail-for="${CSS.escape(symbol)}"]`);
  if (detail) detail.hidden = !isExpanded;
});

function handlePagination(event) {
  const button = event.target.closest("button[data-page-kind]");
  if (!button) return;
  const page = Number(button.dataset.page);
  if (button.dataset.pageKind === "allocation") {
    state.allocationPage = page;
    renderHoldings();
    elements.holdingsBody.scrollIntoView({ block: "start" });
  } else {
    state.tradePage = page;
    renderRebalance();
    elements.rebalanceBody.scrollIntoView({ block: "start" });
  }
}
elements.allocationPagination.addEventListener("click", handlePagination);
elements.tradePagination.addEventListener("click", handlePagination);
elements.historyList.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-snapshot-id]");
  if (button) viewSnapshot(button.dataset.snapshotId);
});
elements.freshCash.addEventListener("input", () => {
  clearRebalance();
  renderAllocationTotal();
  scheduleSave();
});
elements.roundingMode.addEventListener("change", () => {
  clearRebalance();
  scheduleSave();
});
window.addEventListener("beforeunload", () => {
  const status = state.current ? allocationStatus() : null;
  if (state.dirty && state.current?.is_active && status?.targetsValid && status.cashValid) {
    fetch(`${API_BASE}/api/portfolio/snapshots/${state.current.snapshot_id}/working-state`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(workingStatePayload()), keepalive: true,
    });
  }
});

initialize();
