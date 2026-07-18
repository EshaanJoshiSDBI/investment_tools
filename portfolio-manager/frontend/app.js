const API_BASE = "http://127.0.0.1:8000";

const state = {
  holdings: [],
  summary: null,
  targetWeights: {},
  rebalanceResult: null,
};

const elements = {
  apiStatus: document.querySelector("#apiStatus"),
  portfolioFile: document.querySelector("#portfolioFile"),
  selectedFileName: document.querySelector("#selectedFileName"),
  uploadButton: document.querySelector("#uploadButton"),
  refreshPricesButton: document.querySelector("#refreshPricesButton"),
  rebalanceButton: document.querySelector("#rebalanceButton"),
  exportButton: document.querySelector("#exportButton"),
  errorArea: document.querySelector("#errorArea"),
  infoArea: document.querySelector("#infoArea"),
  holdingsBody: document.querySelector("#holdingsBody"),
  rebalanceBody: document.querySelector("#rebalanceBody"),
  freshCash: document.querySelector("#freshCash"),
  roundingMode: document.querySelector("#roundingMode"),
  totalMarketValue: document.querySelector("#totalMarketValue"),
  totalCost: document.querySelector("#totalCost"),
  unrealizedPnl: document.querySelector("#unrealizedPnl"),
  holdingCount: document.querySelector("#holdingCount"),
  totalBuy: document.querySelector("#totalBuy"),
  totalSell: document.querySelector("#totalSell"),
  netCash: document.querySelector("#netCash"),
  cashDelta: document.querySelector("#cashDelta"),
};

const money = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 2,
  minimumFractionDigits: 2,
});

const number = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 4,
});

function formatMoney(value) {
  return money.format(Number(value || 0));
}

function formatPct(value) {
  return `${number.format(Number(value || 0))}%`;
}

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
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).join("; ")
      : payload.detail;
    throw new Error(detail || `Request failed with ${response.status}`);
  }
  return payload;
}

async function checkHealth() {
  try {
    await apiFetch("/api/health");
    elements.apiStatus.textContent = "Backend online";
    elements.apiStatus.classList.add("ok");
  } catch {
    elements.apiStatus.textContent = "Start FastAPI on :8000";
    elements.apiStatus.classList.remove("ok");
  }
}

function renderSummary() {
  if (!state.summary) {
    return;
  }

  elements.totalMarketValue.textContent = formatMoney(state.summary.total_market_value);
  elements.totalCost.textContent = formatMoney(state.summary.total_cost);
  elements.unrealizedPnl.textContent = `${formatMoney(state.summary.unrealized_pnl)} (${formatPct(state.summary.unrealized_pnl_pct)})`;
  elements.holdingCount.textContent = state.summary.holding_count;
}

function renderHoldings() {
  if (!state.holdings.length) {
    elements.holdingsBody.innerHTML = '<tr><td colspan="9" class="empty-cell">Upload a portfolio to begin.</td></tr>';
    return;
  }

  elements.holdingsBody.replaceChildren();
  state.holdings.forEach((holding) => {
    const target = state.targetWeights[holding.symbol] ?? holding.current_weight_pct;
    const row = document.createElement("tr");
    [
      holding.symbol,
      number.format(holding.quantity),
      formatMoney(holding.avg_price),
      formatMoney(holding.ltp),
      formatMoney(holding.market_value),
      formatPct(holding.current_weight_pct),
      formatMoney(holding.unrealized_pnl),
      formatPct(holding.unrealized_pnl_pct),
    ].forEach((value) => row.appendChild(createCell(value)));

    const targetCell = document.createElement("td");
    const input = document.createElement("input");
    input.className = "target-input";
    input.type = "number";
    input.min = "0";
    input.max = "100";
    input.step = "0.01";
    input.value = Number(target).toFixed(2);
    input.dataset.symbol = holding.symbol;
    input.addEventListener("input", (event) => {
      state.targetWeights[event.target.dataset.symbol] = Number(event.target.value || 0);
      clearRebalance();
    });
    targetCell.appendChild(input);
    row.appendChild(targetCell);
    elements.holdingsBody.appendChild(row);
  });
}

function createCell(value) {
  const cell = document.createElement("td");
  cell.textContent = value;
  return cell;
}

function renderCashImpact() {
  const cash = state.rebalanceResult?.cash_impact;
  if (!cash) {
    return;
  }

  elements.totalBuy.textContent = formatMoney(cash.total_buy_value);
  elements.totalSell.textContent = formatMoney(cash.total_sell_value);
  elements.netCash.textContent = formatMoney(cash.net_cash_required);
  elements.cashDelta.textContent = formatMoney(cash.cash_surplus_or_shortfall);
  elements.cashDelta.classList.toggle("negative", cash.cash_surplus_or_shortfall < 0);
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
    [
      number.format(result.trade_qty),
      formatMoney(result.trade_value),
      formatPct(result.current_weight_pct),
      formatPct(result.target_weight_pct),
      formatPct(result.final_weight_pct),
      formatPct(result.weight_drift_pct),
    ].forEach((value) => row.appendChild(createCell(value)));
    elements.rebalanceBody.appendChild(row);
  });
}

function initialTargetWeights(holdings) {
  const entries = holdings.map((holding) => [
    holding.symbol,
    Math.round(holding.current_weight_pct * 100) / 100,
  ]);
  const currentTotal = holdings.reduce((sum, holding) => sum + holding.current_weight_pct, 0);
  if (entries.length && currentTotal > 0) {
    const roundedTotal = entries.reduce((sum, entry) => sum + entry[1], 0);
    const largestIndex = holdings.reduce(
      (best, holding, index) => (
        holding.current_weight_pct > holdings[best].current_weight_pct ? index : best
      ),
      0,
    );
    entries[largestIndex][1] = Math.round((entries[largestIndex][1] + 100 - roundedTotal) * 100) / 100;
  }
  return Object.fromEntries(entries);
}

function clearRebalance() {
  state.rebalanceResult = null;
  elements.exportButton.disabled = true;
  [elements.totalBuy, elements.totalSell, elements.netCash, elements.cashDelta]
    .forEach((element) => {
      element.textContent = "-";
      element.classList.remove("negative");
    });
  renderRebalance();
}

function setRequestBusy(isBusy) {
  elements.uploadButton.disabled = isBusy;
  elements.refreshPricesButton.disabled = isBusy || !state.holdings.length;
  elements.rebalanceButton.disabled = isBusy || !state.holdings.length;
  if (isBusy) {
    elements.exportButton.disabled = true;
  } else if (state.rebalanceResult?.rows?.length) {
    elements.exportButton.disabled = false;
  }
}

function setPortfolio(payload) {
  state.holdings = payload.holdings;
  state.summary = payload.summary;
  state.targetWeights = initialTargetWeights(payload.holdings);
  elements.refreshPricesButton.disabled = false;
  elements.rebalanceButton.disabled = false;
  clearRebalance();
  renderSummary();
  renderHoldings();
  renderRebalance();
}

async function uploadPortfolio() {
  const file = elements.portfolioFile.files[0];
  if (!file) {
    setMessage("error", "Choose a CSV or XLSX file first.");
    return;
  }

  const formData = new FormData();
  formData.append("file", file);

  try {
    setRequestBusy(true);
    setMessage("info", "Uploading and validating portfolio...");
    const payload = await apiFetch("/api/portfolio/upload", {
      method: "POST",
      body: formData,
    });
    setPortfolio(payload);
    setMessage("info", "Portfolio loaded successfully.");
  } catch (error) {
    setMessage("error", error.message);
  } finally {
    setRequestBusy(false);
  }
}

async function recalculatePortfolio() {
  const payload = await apiFetch("/api/portfolio/summary", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(state.holdings),
  });
  state.holdings = payload.holdings;
  state.summary = payload.summary;
  renderSummary();
  renderHoldings();
}

async function refreshPrices() {
  if (!state.holdings.length) {
    return;
  }

  try {
    clearRebalance();
    setRequestBusy(true);
    setMessage("info", "Refreshing prices from yfinance...");
    const payload = await apiFetch("/api/portfolio/refresh-prices", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols: state.holdings.map((holding) => holding.symbol) }),
    });

    const prices = Object.fromEntries(
      payload.prices.filter((item) => item.success).map((item) => [item.symbol, item.price]),
    );
    state.holdings = state.holdings.map((holding) => ({
      ...holding,
      ltp: prices[holding.symbol] ?? holding.ltp,
    }));
    await recalculatePortfolio();

    const failures = payload.prices.filter((item) => !item.success);
    const message = failures.length
      ? `Prices refreshed with warnings: ${failures.map((item) => `${item.symbol} (${item.error})`).join(", ")}`
      : "Prices refreshed.";
    setMessage("info", message);
  } catch (error) {
    setMessage("error", error.message);
  } finally {
    setRequestBusy(false);
  }
}

async function calculateRebalance() {
  try {
    clearRebalance();
    setRequestBusy(true);
    const targetWeights = Object.entries(state.targetWeights).map(([symbol, target_weight_pct]) => ({
      symbol,
      target_weight_pct: Number(target_weight_pct || 0),
    }));
    const payload = await apiFetch("/api/portfolio/rebalance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        holdings: state.holdings,
        target_weights: targetWeights,
        fresh_cash: Number(elements.freshCash.value || 0),
        rounding_mode: elements.roundingMode.value,
      }),
    });
    state.rebalanceResult = payload;
    renderCashImpact();
    renderRebalance();
    setMessage("info", "Rebalance calculated.");
  } catch (error) {
    setMessage("error", error.message);
  } finally {
    setRequestBusy(false);
  }
}

function exportTradesCsv() {
  if (!state.rebalanceResult?.rows?.length) {
    return;
  }

  const columns = [
    "symbol",
    "action",
    "trade_qty",
    "trade_value",
    "current_weight_pct",
    "target_weight_pct",
    "final_weight_pct",
    "weight_drift_pct",
  ];
  const lines = [
    columns.join(","),
    ...state.rebalanceResult.rows.map((row) => (
      columns.map((column) => csvCell(row[column] ?? "")).join(",")
    )),
  ];
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "rebalance-trades.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function csvCell(value) {
  const text = String(value);
  const safeText = /^[=+\-@]/.test(text) ? `'${text}` : text;
  return JSON.stringify(safeText);
}

elements.uploadButton.addEventListener("click", uploadPortfolio);
elements.portfolioFile.addEventListener("change", () => {
  const file = elements.portfolioFile.files[0];
  elements.selectedFileName.textContent = file?.name || "Choose portfolio file";
});
elements.refreshPricesButton.addEventListener("click", refreshPrices);
elements.rebalanceButton.addEventListener("click", calculateRebalance);
elements.exportButton.addEventListener("click", exportTradesCsv);
elements.freshCash.addEventListener("input", clearRebalance);
elements.roundingMode.addEventListener("change", clearRebalance);

checkHealth();
