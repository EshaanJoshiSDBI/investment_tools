const API_BASE = "http://127.0.0.1:8000";

const state = {
  active: null,
  current: null,
  snapshots: [],
  targetWeights: {},
  rebalanceResult: null,
  saveTimer: null,
  saveInFlight: null,
  dirty: false,
};

const elements = Object.fromEntries([
  "apiStatus", "saveStatus", "portfolioFile", "selectedFileName", "uploadButton",
  "exportBackupButton", "importBackupLabel", "backupFile", "errorArea", "infoArea",
  "historyPanel", "snapshotSelect", "snapshotMetadata", "restoreButton", "holdingsBody",
  "refreshPricesButton", "priceMetadata", "rebalanceButton", "exportButton", "freshCash",
  "roundingMode", "totalMarketValue", "totalCost", "unrealizedPnl", "holdingCount",
  "rebalanceBody", "totalBuy", "totalSell", "netCash", "cashDelta",
].map((id) => [id, document.querySelector(`#${id}`)]));

const money = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const number = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 4 });

function formatMoney(value) { return money.format(Number(value || 0)); }
function formatPct(value) { return `${number.format(Number(value || 0))}%`; }

function setMessage(type, message) {
  const target = type === "error" ? elements.errorArea : elements.infoArea;
  const other = type === "error" ? elements.infoArea : elements.errorArea;
  other.classList.remove("visible");
  other.textContent = "";
  target.textContent = message;
  target.classList.toggle("visible", Boolean(message));
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
  try {
    const health = await apiFetch("/api/health");
    elements.apiStatus.textContent = `Backend online · DB v${health.schema_version}`;
    elements.apiStatus.classList.add("ok");
    applyWorkspace(await apiFetch("/api/portfolio"));
  } catch (error) {
    elements.apiStatus.textContent = "Start FastAPI on :8000";
    elements.apiStatus.classList.remove("ok");
    setMessage("error", error.message);
  }
}

function applyWorkspace(workspace) {
  state.active = workspace.active;
  state.snapshots = workspace.snapshots;
  setPortfolio(workspace.active);
  renderHistory();
  elements.exportBackupButton.disabled = !workspace.active;
  elements.importBackupLabel.classList.toggle("disabled", Boolean(workspace.active));
}

function setPortfolio(portfolio) {
  state.current = portfolio;
  state.targetWeights = Object.fromEntries(
    (portfolio?.target_weights || []).map((item) => [item.symbol, item.target_weight_pct]),
  );
  elements.freshCash.value = portfolio?.fresh_cash ?? 0;
  elements.roundingMode.value = portfolio?.rounding_mode ?? "nearest";
  state.dirty = false;
  clearTimeout(state.saveTimer);
  elements.saveStatus.textContent = "";
  clearRebalance();
  renderSummary();
  renderHoldings();
  renderHistory();
  updateControls();
}

function renderSummary() {
  const summary = state.current?.summary;
  elements.totalMarketValue.textContent = summary ? formatMoney(summary.total_market_value) : "-";
  elements.totalCost.textContent = summary ? formatMoney(summary.total_cost) : "-";
  elements.unrealizedPnl.textContent = summary
    ? `${formatMoney(summary.unrealized_pnl)} (${formatPct(summary.unrealized_pnl_pct)})` : "-";
  elements.holdingCount.textContent = summary?.holding_count ?? "-";
}

function renderHoldings() {
  const holdings = state.current?.holdings || [];
  if (!holdings.length) {
    elements.holdingsBody.innerHTML = '<tr><td colspan="9" class="empty-cell">Upload a portfolio to begin.</td></tr>';
    elements.priceMetadata.textContent = "Current allocation from backend calculations.";
    return;
  }
  elements.holdingsBody.replaceChildren();
  holdings.forEach((holding) => {
    const row = document.createElement("tr");
    [
      holding.symbol, number.format(holding.quantity), formatMoney(holding.avg_price),
      formatMoney(holding.ltp), formatMoney(holding.market_value), formatPct(holding.current_weight_pct),
      formatMoney(holding.unrealized_pnl), formatPct(holding.unrealized_pnl_pct),
    ].forEach((value) => row.appendChild(createCell(value)));
    const targetCell = document.createElement("td");
    const input = document.createElement("input");
    input.className = "target-input";
    input.type = "number";
    input.min = "0";
    input.max = "100";
    input.step = "0.01";
    input.value = Number(state.targetWeights[holding.symbol] ?? 0).toFixed(2);
    input.dataset.symbol = holding.symbol;
    input.disabled = !state.current.is_active;
    input.addEventListener("input", (event) => {
      state.targetWeights[event.target.dataset.symbol] = Number(event.target.value || 0);
      clearRebalance();
      scheduleSave();
    });
    targetCell.appendChild(input);
    row.appendChild(targetCell);
    elements.holdingsBody.appendChild(row);
  });
  elements.priceMetadata.textContent = state.current.latest_price_at
    ? `Latest successful prices observed ${new Date(state.current.latest_price_at).toLocaleString()}.`
    : "Using prices from the uploaded portfolio file.";
}

function renderHistory() {
  elements.historyPanel.hidden = state.snapshots.length === 0;
  if (!state.snapshots.length) return;
  elements.snapshotSelect.replaceChildren();
  state.snapshots.forEach((snapshot) => {
    const option = document.createElement("option");
    option.value = snapshot.snapshot_id;
    option.textContent = `${new Date(snapshot.created_at).toLocaleString()} · ${snapshot.filename}${snapshot.lifecycle_status === "active" ? " · Active" : ""}`;
    option.selected = snapshot.snapshot_id === state.current?.snapshot_id;
    elements.snapshotSelect.appendChild(option);
  });
  const current = state.current;
  elements.snapshotMetadata.textContent = current
    ? `${current.source.filename} · imported ${new Date(current.source.imported_at).toLocaleString()} · ${current.lifecycle_status}`
    : "";
  elements.restoreButton.hidden = !current || current.is_active;
}

function renderRebalance() {
  if (!state.rebalanceResult?.rows?.length) {
    elements.rebalanceBody.innerHTML = '<tr><td colspan="8" class="empty-cell">Run a rebalance to see trades.</td></tr>';
    return;
  }
  elements.rebalanceBody.replaceChildren();
  state.rebalanceResult.rows.forEach((result) => {
    const row = document.createElement("tr");
    row.appendChild(createCell(result.symbol));
    const actionCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${result.action.toLowerCase()}`;
    badge.textContent = result.action;
    actionCell.appendChild(badge);
    row.appendChild(actionCell);
    [result.trade_qty, result.trade_value, result.current_weight_pct, result.target_weight_pct,
      result.final_weight_pct, result.weight_drift_pct].forEach((value, index) => (
      row.appendChild(createCell(index === 1 ? formatMoney(value) : index === 0 ? number.format(value) : formatPct(value)))
    ));
    elements.rebalanceBody.appendChild(row);
  });
}

function createCell(value) {
  const cell = document.createElement("td");
  cell.textContent = value;
  return cell;
}

function clearRebalance() {
  state.rebalanceResult = null;
  [elements.totalBuy, elements.totalSell, elements.netCash, elements.cashDelta].forEach((element) => {
    element.textContent = "-";
    element.classList.remove("negative");
  });
  renderRebalance();
  updateControls();
}

function workingStatePayload() {
  return {
    target_weights: Object.entries(state.targetWeights).map(([symbol, target_weight_pct]) => ({ symbol, target_weight_pct: Number(target_weight_pct || 0) })),
    fresh_cash: Number(elements.freshCash.value || 0),
    rounding_mode: elements.roundingMode.value,
  };
}

function scheduleSave() {
  if (!state.current?.is_active) return;
  state.dirty = true;
  elements.saveStatus.textContent = "Unsaved changes";
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => flushWorkingState().catch(() => {}), 500);
}

async function flushWorkingState() {
  clearTimeout(state.saveTimer);
  if (state.saveInFlight) await state.saveInFlight;
  if (!state.dirty || !state.current?.is_active) return;
  const snapshotId = state.current.snapshot_id;
  const payload = workingStatePayload();
  state.dirty = false;
  elements.saveStatus.textContent = "Saving…";
  state.saveInFlight = apiFetch(`/api/portfolio/snapshots/${snapshotId}/working-state`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  try {
    await state.saveInFlight;
    elements.saveStatus.textContent = state.dirty ? "Unsaved changes" : "Saved";
  } catch (error) {
    state.dirty = true;
    elements.saveStatus.textContent = "Save failed";
    setMessage("error", error.message);
    throw error;
  } finally {
    state.saveInFlight = null;
  }
  if (state.dirty) return flushWorkingState();
}

async function uploadPortfolio() {
  const file = elements.portfolioFile.files[0];
  if (!file) return setMessage("error", "Choose a CSV or XLSX file first.");
  try {
    await flushWorkingState();
    setBusy(true);
    const formData = new FormData();
    formData.append("file", file);
    const result = await apiFetch("/api/portfolio/upload", { method: "POST", body: formData });
    applyWorkspace(result.workspace);
    setMessage("info", result.status === "no_op" ? "This portfolio is already active." : "Portfolio saved successfully.");
  } catch (error) { setMessage("error", error.message); }
  finally { setBusy(false); }
}

async function selectSnapshot() {
  try {
    await flushWorkingState();
    setBusy(true);
    setPortfolio(await apiFetch(`/api/portfolio/snapshots/${elements.snapshotSelect.value}`));
  } catch (error) { setMessage("error", error.message); }
  finally { setBusy(false); }
}

async function restoreSnapshot() {
  if (!state.current || !window.confirm("Restore this historical snapshot as a new active snapshot?")) return;
  try {
    setBusy(true);
    applyWorkspace(await apiFetch(`/api/portfolio/snapshots/${state.current.snapshot_id}/restore`, { method: "POST" }));
    setMessage("info", "Historical snapshot restored as a new active snapshot.");
  } catch (error) { setMessage("error", error.message); }
  finally { setBusy(false); }
}

async function refreshPrices() {
  try {
    await flushWorkingState();
    setBusy(true);
    clearRebalance();
    const payload = await apiFetch(`/api/portfolio/snapshots/${state.current.snapshot_id}/refresh-prices`, { method: "POST" });
    setPortfolio(payload.portfolio);
    const failures = payload.prices.filter((item) => !item.success);
    setMessage("info", failures.length
      ? `Prices refreshed with warnings: ${failures.map((item) => `${item.symbol} (${item.error})`).join(", ")}`
      : "Prices refreshed and saved.");
  } catch (error) { setMessage("error", error.message); }
  finally { setBusy(false); }
}

async function calculateRebalance() {
  try {
    setBusy(true);
    const payload = workingStatePayload();
    state.rebalanceResult = await apiFetch(`/api/portfolio/snapshots/${state.current.snapshot_id}/rebalance`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    state.dirty = false;
    elements.saveStatus.textContent = "Saved";
    const cash = state.rebalanceResult.cash_impact;
    elements.totalBuy.textContent = formatMoney(cash.total_buy_value);
    elements.totalSell.textContent = formatMoney(cash.total_sell_value);
    elements.netCash.textContent = formatMoney(cash.net_cash_required);
    elements.cashDelta.textContent = formatMoney(cash.cash_surplus_or_shortfall);
    elements.cashDelta.classList.toggle("negative", cash.cash_surplus_or_shortfall < 0);
    renderRebalance();
    setMessage("info", "Rebalance calculated.");
  } catch (error) { setMessage("error", error.message); }
  finally { setBusy(false); }
}

async function exportBackup() {
  try {
    const response = await fetch(`${API_BASE}/api/portfolio/bundles/export`);
    if (!response.ok) throw new Error("Could not export portfolio backup");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = "portfolio-manager-backup.zip";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (error) { setMessage("error", error.message); }
}

async function importBackup() {
  const file = elements.backupFile.files[0];
  if (!file || state.active) return;
  try {
    setBusy(true);
    const formData = new FormData();
    formData.append("file", file);
    applyWorkspace(await apiFetch("/api/portfolio/bundles/import", { method: "POST", body: formData }));
    setMessage("info", "Portfolio backup imported successfully.");
  } catch (error) { setMessage("error", error.message); }
  finally { elements.backupFile.value = ""; setBusy(false); }
}

function exportTradesCsv() {
  if (!state.rebalanceResult?.rows?.length) return;
  const columns = ["symbol", "action", "trade_qty", "trade_value", "current_weight_pct", "target_weight_pct", "final_weight_pct", "weight_drift_pct"];
  const lines = [columns.join(","), ...state.rebalanceResult.rows.map((row) => columns.map((column) => csvCell(row[column] ?? "")).join(","))];
  const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "rebalance-trades.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function csvCell(value) {
  const text = String(value);
  return JSON.stringify(/^[=+\-@]/.test(text) ? `'${text}` : text);
}

function updateControls() {
  const editable = Boolean(state.current?.is_active);
  elements.refreshPricesButton.disabled = !editable;
  elements.rebalanceButton.disabled = !editable;
  elements.freshCash.disabled = !editable;
  elements.roundingMode.disabled = !editable;
  elements.exportButton.disabled = !state.rebalanceResult?.rows?.length;
}

function setBusy(busy) {
  elements.uploadButton.disabled = busy;
  elements.snapshotSelect.disabled = busy;
  elements.restoreButton.disabled = busy;
  elements.exportBackupButton.disabled = busy || !state.active;
  if (busy) {
    elements.refreshPricesButton.disabled = true;
    elements.rebalanceButton.disabled = true;
  } else updateControls();
}

elements.uploadButton.addEventListener("click", uploadPortfolio);
elements.portfolioFile.addEventListener("change", () => {
  elements.selectedFileName.textContent = elements.portfolioFile.files[0]?.name || "Choose portfolio file";
});
elements.snapshotSelect.addEventListener("change", selectSnapshot);
elements.restoreButton.addEventListener("click", restoreSnapshot);
elements.refreshPricesButton.addEventListener("click", refreshPrices);
elements.rebalanceButton.addEventListener("click", calculateRebalance);
elements.exportButton.addEventListener("click", exportTradesCsv);
elements.exportBackupButton.addEventListener("click", exportBackup);
elements.backupFile.addEventListener("change", importBackup);
elements.freshCash.addEventListener("input", () => { clearRebalance(); scheduleSave(); });
elements.roundingMode.addEventListener("change", () => { clearRebalance(); scheduleSave(); });
window.addEventListener("beforeunload", () => {
  if (state.dirty && state.current?.is_active) {
    fetch(`${API_BASE}/api/portfolio/snapshots/${state.current.snapshot_id}/working-state`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(workingStatePayload()), keepalive: true,
    });
  }
});

initialize();
