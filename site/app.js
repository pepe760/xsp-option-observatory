"use strict";

const SYMBOLS = {
  XSP: [15, 20, 40, 50],
  SPY: [5, 10, 15, 20, 40, 50],
  QQQ: [5, 10, 15, 20, 40, 50],
  TLT: [1, 2, 5, 10],
  SOXX: [5, 10, 15, 20],
};

const TARGET_DELTAS = [0.03, 0.05, 0.07, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40];
const QUOTE_FIELDS = {
  short_strike: "short-strike",
  long_strike: "long-strike",
  natural_credit: "natural-credit",
  natural_debit: "natural-debit",
  max_loss_per_spread_before_fees: "max-loss",
  actual_short_delta: "actual-short-delta",
};
const PUBLIC_ROW_FIELDS = new Set([
  "symbol", "target_delta", "wing", "status", "session", "expiry",
  "observed_at", "reason", "source", "dte",
]);
const STATUS = {
  WAITING_FOR_SOURCE: ["等候資料", "neutral", "仍未收到呢個組合的觀測狀態。"],
  CAPTURED: ["合資格觀測", "good", "呢個組合有一筆通過資格檢查的觀測紀錄。"],
  NO_NEXT_SESSION_EXPIRY: ["下一交易日無到期", "warn", "已核對合約，下一個交易日冇對應到期日。"],
  EXPIRY_UNVERIFIED: ["到期日待核實", "warn", "暫時未能核實下一個交易日有冇對應的期權合約。"],
  DELTA_UNREACHABLE: ["目標 Delta 未達", "warn", "已核對的短腿合約入面，冇符合目標 Delta 條件的一腿。"],
  MISSING_EXACT_WING: ["缺指定翼寬", "warn", "搵到短腿，但合約列表入面冇完全符合所選翼寬的長腿。"],
  QUOTE_UNQUALIFIED: ["報價未合資格", "bad", "兩腿的報價未通過觀測資格檢查。"],
  NONPOSITIVE_CREDIT: ["自然價淨收入不為正", "bad", "短腿 bid 減長腿 ask 得出的自然價淨權利金小於或等於零。"],
  OHLC_ONLY: ["只有 OHLC", "warn", "目前只得 OHLC 資料，未能重建同一時點兩腿的 bid／ask 同 Delta。"],
  INDICATIVE_ONLY: ["只有參考資料", "warn", "目前的資料只可作參考，未符合觀測所需的資料條件。"],
  OUTSIDE_SESSION: ["交易時段外", "warn", "資料時間喺觀測所用的交易時段以外。"],
  CALENDAR_UNKNOWN: ["交易日曆待核實", "warn", "暫時未能確認美國市場的下一個交易日。"],
};
const SOURCE_KINDS = {
  LIVE_BBO: "即時買賣報價",
  HISTORICAL_BBO: "歷史買賣報價",
  DELAYED_BBO: "延遲買賣報價",
  INDICATIVE: "參考資料",
  OHLC: "OHLC",
};
const GREEKS_SOURCES = { VENDOR: "資料供應商", MODEL: "模型估算" };
const LOAD_FAILED = ["讀取失敗", "bad", "暫時讀唔到公開狀態檔。"];
const DATE_FORMAT = {
  et: new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }),
  hk: new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Hong_Kong", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }),
};
const NUMBER_FORMAT = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });
const MAX_FILE_BYTES = 4 * 1024 * 1024;
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const TIME_WITH_ZONE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const STATUS_CODE = /^[A-Z][A-Z0-9_]{0,79}$/;
const EMPTY = "—";
const elements = Object.fromEntries(
  Array.from(document.querySelectorAll("[id]"), (element) => [element.id, element]),
);
const coverageRows = new Map();
const state = { public: null, private: null, loading: true };
const symbolSelect = elements.symbol;
const deltaSelect = elements["target-delta"];
const wingSelect = elements.wing;
const historySelect = elements.history;

function validDate(value) {
  if (typeof value !== "string" || !DATE_ONLY.test(value)) return false;
  const time = Date.parse(value);
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value;
}

function validTime(value) {
  return typeof value === "string" && TIME_WITH_ZONE.test(value)
    && validDate(value.slice(0, 10)) && Number.isFinite(Date.parse(value));
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validRow(row, mode) {
  if (!isRecord(row) || !Object.hasOwn(SYMBOLS, row.symbol) || !Object.hasOwn(STATUS, row.status)) return false;
  if (!Number.isFinite(row.target_delta) || row.target_delta <= 0 || row.target_delta > 1
    || !Number.isFinite(row.wing) || row.wing <= 0) return false;
  if (row.session != null && !validDate(row.session)) return false;
  if (row.expiry != null && !validDate(row.expiry)) return false;
  if (row.observed_at != null && !validTime(row.observed_at)) return false;
  if (row.reason != null && (typeof row.reason !== "string" || !STATUS_CODE.test(row.reason))) return false;
  if (row.source != null && (typeof row.source !== "string" || row.source.length > 100)) return false;
  if (row.dte != null && (!Number.isInteger(row.dte) || row.dte < 0)) return false;
  if (mode === "STATUS_ONLY") return Object.keys(row).every((key) => PUBLIC_ROW_FIELDS.has(key));
  if (row.source_kind != null && !Object.hasOwn(SOURCE_KINDS, row.source_kind)) return false;
  if (row.greeks_source != null && !Object.hasOwn(GREEKS_SOURCES, row.greeks_source)) return false;
  return Object.keys(QUOTE_FIELDS).every((key) => row[key] == null || Number.isFinite(row[key]));
}

function validateDataset(data, mode) {
  if (!isRecord(data) || data.schema_version !== 1 || data.mode !== mode
    || (data.generated_at != null && !validTime(data.generated_at))
    || !Array.isArray(data.rows) || typeof data.note !== "string" || data.note.length > 400
    || !data.rows.every((row) => validRow(row, mode))) {
    throw new Error("Invalid observation schema");
  }
  return data;
}

function text(id, value) {
  elements[id].textContent = value;
}

function formatTime(value, zone) {
  const parts = Object.fromEntries(
    DATE_FORMAT[zone].formatToParts(new Date(value)).map(({ type, value: part }) => [type, part]),
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

function renderTime(container, value) {
  if (!value) {
    container.textContent = EMPTY;
    return;
  }
  const times = [["et", "ET"], ["hk", "HK"]].map(([zone, label]) => {
    const time = document.createElement("time");
    time.dateTime = value;
    time.textContent = `${formatTime(value, zone)} ${label}`;
    return time;
  });
  container.replaceChildren(...times);
}

function setBadge(element, info) {
  element.textContent = info[0];
  element.dataset.tone = info[1];
}

function dte(row) {
  if (row?.session && row.expiry) {
    const days = (Date.parse(row.expiry) - Date.parse(row.session)) / 86400000;
    return days >= 0 ? days : EMPTY;
  }
  return row?.dte ?? EMPTY;
}

function deltaLabel(value) {
  return NUMBER_FORMAT.format(value * 100) + "Δ";
}

function replaceOptions(select, values, previous, fallback, label) {
  const options = values.map((value) => new Option(label(value), String(value)));
  select.replaceChildren(...options);
  select.value = String(values.includes(previous) ? previous : fallback);
}

function updateFilters(rows) {
  const symbol = symbolSelect.value;
  const matchingRows = rows.filter((row) => row.symbol === symbol);
  const deltas = Array.from(new Set([...TARGET_DELTAS, ...matchingRows.map((row) => row.target_delta)]));
  const wings = Array.from(new Set([...SYMBOLS[symbol], ...matchingRows.map((row) => row.wing)]));
  replaceOptions(deltaSelect, deltas.sort((a, b) => a - b), Number(deltaSelect.value), 0.10, deltaLabel);
  replaceOptions(wingSelect, wings.sort((a, b) => a - b), Number(wingSelect.value), symbol === "XSP" ? 50 : wings[0],
    (value) => `${NUMBER_FORMAT.format(value)} 點`);
}

function keyFor(symbol, delta, wing) {
  return `${symbol}/${delta}/${wing}`;
}

function latestCombinations(rows) {
  const latest = new Map();
  for (const row of rows) {
    const key = keyFor(row.symbol, row.target_delta, row.wing);
    const previous = latest.get(key);
    if (!previous || isMoreRecent(row, previous)) latest.set(key, row);
  }
  return latest;
}

function isMoreRecent(row, previous) {
  const session = row.session || "";
  const priorSession = previous.session || "";
  if (session !== priorSession) return session > priorSession;
  const timestamp = row.observed_at ? Date.parse(row.observed_at) : -Infinity;
  const priorTimestamp = previous.observed_at ? Date.parse(previous.observed_at) : -Infinity;
  return timestamp > priorTimestamp;
}

function selectedObservation(rows, latest, key) {
  const matching = state.private ? rows.filter((row) => keyFor(row.symbol, row.target_delta, row.wing) === key) : [];
  elements["history-field"].hidden = matching.length < 2;
  if (matching.length < 2) {
    historySelect.replaceChildren();
    delete historySelect.dataset.key;
    return latest;
  }

  matching.sort((a, b) => isMoreRecent(a, b) ? -1 : isMoreRecent(b, a) ? 1 : 0);
  const previous = historySelect.dataset.key === key ? historySelect.value : "0";
  const options = matching.map((row, index) => {
    const date = row.observed_at ? formatTime(row.observed_at, "et") + " ET" : row.session ?? "日期未記錄";
    return new Option([date, STATUS[row.status][0], row.source].filter(Boolean).join(" · "), String(index));
  });
  historySelect.replaceChildren(...options);
  historySelect.dataset.key = key;
  historySelect.value = Number(previous) < matching.length ? previous : "0";
  return matching[Number(historySelect.value)];
}

function initCoverage() {
  for (const symbol of Object.keys(SYMBOLS)) {
    const tr = document.createElement("tr");
    const heading = document.createElement("th");
    const button = document.createElement("button");
    heading.scope = "row";
    button.className = "symbol-button";
    button.type = "button";
    button.textContent = symbol;
    button.addEventListener("click", () => {
      symbolSelect.value = symbol;
      render(true);
    });
    heading.append(button);
    tr.append(heading);

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge";
    statusCell.append(badge);
    const session = document.createElement("td");
    const expiry = document.createElement("td");
    const observed = document.createElement("td");
    tr.append(statusCell, session, expiry, observed);
    elements["coverage-rows"].append(tr);
    coverageRows.set(symbol, { tr, button, badge, session, expiry, observed });
  }
}

function renderCoverage(latest, selectedSymbol, delta, wing, datasetStatus) {
  for (const [symbol, cells] of coverageRows) {
    const row = latest.get(keyFor(symbol, delta, wing));
    const status = datasetStatus || (row ? STATUS[row.status] : SYMBOLS[symbol].includes(wing)
      ? STATUS.WAITING_FOR_SOURCE : ["翼寬未列入", "neutral"]);
    const selected = symbol === selectedSymbol;
    cells.tr.dataset.selected = String(selected);
    cells.button.setAttribute("aria-pressed", String(selected));
    setBadge(cells.badge, status);
    cells.session.textContent = row?.session ?? EMPTY;
    cells.expiry.textContent = row?.expiry ?? EMPTY;
    renderTime(cells.observed, row?.observed_at);
  }
}

function render(refreshFilters = false) {
  const data = state.private || state.public;
  const rows = data?.rows ?? [];
  if (refreshFilters) updateFilters(rows);
  const symbol = symbolSelect.value;
  const delta = Number(deltaSelect.value);
  const wing = Number(wingSelect.value);
  const latest = latestCombinations(rows);
  const key = keyFor(symbol, delta, wing);
  const row = selectedObservation(rows, latest.get(key), key);
  const pending = state.loading && !state.private;
  const datasetStatus = pending ? ["讀取中", "neutral", "正等候資料檔。"] : data ? null : LOAD_FAILED;
  const status = datasetStatus || (row ? STATUS[row.status] : STATUS.WAITING_FOR_SOURCE);
  const qualified = Array.from(latest.values()).filter((record) => record.status === "CAPTURED").length;

  text("mode-label", state.private ? "本機私有資料" : "公開狀態");
  text("generated-at", data?.generated_at ? `${formatTime(data.generated_at, "hk")} HK` : EMPTY);
  text("coverage-summary", data ? `${qualified} 個` : EMPTY);
  text("data-notice", pending ? "讀取資料狀態中。" : data?.note || (data ? "已讀取觀測狀態。" : "暫時讀唔到公開狀態檔。"));
  text("observation-title", symbol);
  text("selection-summary", `目標 ${deltaLabel(delta)} · 翼寬 ${NUMBER_FORMAT.format(wing)} 點`);
  setBadge(elements["observation-status"], status);
  text("observation-reason", status[2]);
  const code = row?.reason;
  const reason = elements["observation-reason"];
  if (code && code !== row.status) {
    const detail = document.createElement("small");
    detail.textContent = `狀態原因 ${code}`;
    reason.append(detail);
  }
  text("session", row?.session ?? EMPTY);
  text("expiry", row?.expiry ?? EMPTY);
  renderTime(elements["observed-at"], row?.observed_at);
  text("dte", dte(row));
  const hasQuote = state.private && row?.status === "CAPTURED" && Object.keys(QUOTE_FIELDS).some((key) => row[key] != null);
  elements["quote-details"].hidden = !hasQuote;
  for (const [key, id] of Object.entries(QUOTE_FIELDS)) {
    text(id, hasQuote && row[key] != null ? NUMBER_FORMAT.format(row[key]) : EMPTY);
  }
  text("greeks-source", hasQuote ? GREEKS_SOURCES[row.greeks_source] ?? EMPTY : EMPTY);
  const sourceDescription = state.private ? "瀏覽器已讀取你揀的本機觀測檔。" : "公開狀態檔載有資料覆蓋同資格結果。";
  const sourceKind = row?.source_kind ? `（${SOURCE_KINDS[row.source_kind]}）` : "";
  text("source-description", row?.source ? `${sourceDescription} 所選紀錄來源為 ${row.source}${sourceKind}。` : sourceDescription);
  elements["clear-private"].hidden = !state.private;
  renderCoverage(latest, symbol, delta, wing, datasetStatus);
}

async function loadPublicData() {
  try {
    const response = await fetch("./data/status.json", { cache: "no-store" });
    if (!response.ok) throw new Error("Public data request failed");
    state.public = validateDataset(await response.json(), "STATUS_ONLY");
  } catch {
    state.public = null;
  } finally {
    state.loading = false;
    if (!state.private) render(true);
  }
}

async function readPrivateFile() {
  const input = elements["private-file"];
  const file = input.files[0];
  input.value = "";
  if (!file) return;
  if (file.size > MAX_FILE_BYTES) {
    text("import-feedback", "檔案大過 4 MB，請揀較細的觀測檔。");
    return;
  }

  const chooser = elements["choose-private"];
  const clearButton = elements["clear-private"];
  chooser.disabled = true;
  clearButton.disabled = true;
  text("import-feedback", "讀取本機檔案中。");
  try {
    state.private = validateDataset(JSON.parse(await file.text()), "PRIVATE_LOCAL");
    delete historySelect.dataset.key;
    text("import-feedback", `已開啟本機觀測檔，共 ${state.private.rows.length} 筆狀態。`);
    render(true);
  } catch {
    text("import-feedback", "讀取失敗。請揀符合版本 1 格式的私有觀測 JSON 檔。");
  } finally {
    chooser.disabled = false;
    clearButton.disabled = false;
  }
}

symbolSelect.addEventListener("change", () => render(true));
deltaSelect.addEventListener("change", () => render());
wingSelect.addEventListener("change", () => render());
historySelect.addEventListener("change", () => render());
elements["choose-private"].addEventListener("click", () => elements["private-file"].click());
elements["private-file"].addEventListener("change", readPrivateFile);
elements["clear-private"].addEventListener("click", () => {
  state.private = null;
  text("import-feedback", "已清除本機觀測資料。");
  render(true);
  elements["choose-private"].focus();
});

initCoverage();
render(true);
loadPublicData();
