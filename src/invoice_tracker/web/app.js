const state = {
  filters: {
    search: "",
    kind: "all",
    currency: "",
    company: "",
    sort: "newest",
  },
  pagination: {
    limit: 25,
    offset: 0,
    total: 0,
  },
  options: {
    currencies: [],
    companies: [],
  },
  summary: null,
  records: [],
  selectedEmailId: null,
};

const elements = {
  filterForm: document.getElementById("filter-form"),
  searchInput: document.getElementById("search-input"),
  kindSelect: document.getElementById("kind-select"),
  currencySelect: document.getElementById("currency-select"),
  companySelect: document.getElementById("company-select"),
  sortSelect: document.getElementById("sort-select"),
  resetFiltersButton: document.getElementById("reset-filters"),
  statusBanner: document.getElementById("status-banner"),
  spendStrip: document.getElementById("spend-strip"),
  summaryCards: document.getElementById("summary-cards"),
  companyList: document.getElementById("company-list"),
  trendChart: document.getElementById("trend-chart"),
  trendCaption: document.getElementById("trend-caption"),
  resultCount: document.getElementById("result-count"),
  recordsBody: document.getElementById("records-body"),
  prevPageButton: document.getElementById("prev-page"),
  nextPageButton: document.getElementById("next-page"),
  pageLabel: document.getElementById("page-label"),
  detailEmpty: document.getElementById("detail-empty"),
  detailContent: document.getElementById("detail-content"),
  detailTitle: document.getElementById("detail-title"),
  detailBadges: document.getElementById("detail-badges"),
  detailGrid: document.getElementById("detail-grid"),
  detailDocuments: document.getElementById("detail-documents"),
  detailLinks: document.getElementById("detail-links"),
  detailJson: document.getElementById("detail-json"),
  rawEmailLink: document.getElementById("raw-email-link"),
};

let searchTimerId = null;
let detailRequestCounter = 0;
let resizeTimerId = null;

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function showStatus(message, { isError = false } = {}) {
  if (!message) {
    elements.statusBanner.hidden = true;
    elements.statusBanner.textContent = "";
    elements.statusBanner.style.color = "";
    elements.statusBanner.style.background = "";
    elements.statusBanner.style.borderColor = "";
    return;
  }

  elements.statusBanner.hidden = false;
  elements.statusBanner.textContent = message;
  if (isError) {
    elements.statusBanner.style.color = "#8f3e18";
    elements.statusBanner.style.background = "#fff0ea";
    elements.statusBanner.style.borderColor = "#f2bba1";
    return;
  }
  elements.statusBanner.style.color = "#1f5940";
  elements.statusBanner.style.background = "#e9f8ef";
  elements.statusBanner.style.borderColor = "#a8d6b8";
}

function formatCount(value) {
  const number = Number(value || 0);
  return new Intl.NumberFormat().format(number);
}

function formatPrice(value, currency) {
  if (value === null || value === undefined) {
    return "-";
  }
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "-";
  }
  if (currency) {
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency,
        maximumFractionDigits: 2,
      }).format(number);
    } catch (_error) {
      // Ignore invalid/unknown currency codes and fall through.
    }
  }
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(number);
}

function formatDate(value) {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return parsed.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function formatMonthLabel(value) {
  if (!value || !value.includes("-")) {
    return value || "-";
  }
  const [yearRaw, monthRaw] = value.split("-");
  const year = Number(yearRaw);
  const month = Number(monthRaw);
  if (!Number.isFinite(year) || !Number.isFinite(month)) {
    return value;
  }
  return new Date(year, month - 1, 1).toLocaleDateString(undefined, {
    month: "short",
    year: "2-digit",
  });
}

function getTypeTag(record) {
  if (record.is_invoice && record.is_receipt) {
    return '<span class="badge badge-both">Invoice + Receipt</span>';
  }
  if (record.is_invoice) {
    return '<span class="badge badge-invoice">Invoice</span>';
  }
  if (record.is_receipt) {
    return '<span class="badge badge-receipt">Receipt</span>';
  }
  return '<span class="badge badge-unknown">Unknown</span>';
}

function renderSummary() {
  const summary = state.summary;
  if (!summary) {
    elements.summaryCards.innerHTML = "";
    elements.spendStrip.innerHTML = "";
    elements.companyList.innerHTML = "";
    elements.trendCaption.textContent = "";
    drawTrend([]);
    return;
  }

  const totals = summary.totals || {};
  const stats = [
    { label: "Emails", value: formatCount(totals.emails) },
    { label: "Classified", value: formatCount(totals.classified) },
    { label: "Invoices", value: formatCount(totals.invoices) },
    { label: "Receipts", value: formatCount(totals.receipts) },
    { label: "Documents", value: formatCount(totals.documents) },
    { label: "Links", value: formatCount(totals.links) },
  ];
  elements.summaryCards.innerHTML = stats
    .map(
      (entry) => `
      <article class="stat-card">
        <div class="stat-label">${escapeHtml(entry.label)}</div>
        <div class="stat-value">${escapeHtml(entry.value)}</div>
      </article>
    `
    )
    .join("");

  const spendByCurrency = summary.spend_by_currency || [];
  if (!spendByCurrency.length) {
    elements.spendStrip.innerHTML = '<span class="pill">No spend totals available yet.</span>';
  } else {
    elements.spendStrip.innerHTML = spendByCurrency
      .slice(0, 6)
      .map((item) => {
        const currency = item.currency || "(none)";
        const total = formatPrice(item.total, currency === "(none)" ? "" : currency);
        return `<span class="pill"><span>${escapeHtml(currency)}</span><b>${escapeHtml(total)}</b></span>`;
      })
      .join("");
  }

  const companies = summary.top_companies || [];
  if (!companies.length) {
    elements.companyList.innerHTML = "<li>No company data yet.</li>";
  } else {
    elements.companyList.innerHTML = companies
      .slice(0, 10)
      .map((item) => {
        return `
          <li>
            <span>${escapeHtml(item.company || "(unknown)")}</span>
            <span>${escapeHtml(formatCount(item.count))}</span>
          </li>
        `;
      })
      .join("");
  }

  const monthly = summary.monthly || [];
  if (monthly.length) {
    const firstMonth = monthly[0].month;
    const lastMonth = monthly[monthly.length - 1].month;
    elements.trendCaption.textContent = `${formatMonthLabel(firstMonth)} to ${formatMonthLabel(lastMonth)}`;
  } else {
    elements.trendCaption.textContent = "No dated records yet";
  }

  drawTrend(monthly);
}

function drawTrend(monthlyData) {
  const canvas = elements.trendChart;
  const context = canvas.getContext("2d");
  if (!context) {
    return;
  }

  const width = Math.max(320, canvas.parentElement.clientWidth - 4);
  const height = 230;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;

  context.setTransform(1, 0, 0, 1, 0, 0);
  context.scale(dpr, dpr);
  context.clearRect(0, 0, width, height);

  if (!monthlyData || !monthlyData.length) {
    context.fillStyle = "#5e6e74";
    context.font = '15px "Avenir Next", "Trebuchet MS", sans-serif';
    context.fillText("No monthly totals available yet.", 16, 32);
    return;
  }

  const padding = { top: 16, right: 12, bottom: 34, left: 48 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const values = monthlyData.map((item) => Number(item.total || 0));
  const maxValue = Math.max(...values, 1);
  const ySteps = 4;

  context.strokeStyle = "rgba(18, 32, 39, 0.18)";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(padding.left, padding.top);
  context.lineTo(padding.left, padding.top + chartHeight);
  context.lineTo(padding.left + chartWidth, padding.top + chartHeight);
  context.stroke();

  context.font = '11px "Avenir Next", "Trebuchet MS", sans-serif';
  context.fillStyle = "#5e6e74";
  for (let step = 0; step <= ySteps; step += 1) {
    const ratio = step / ySteps;
    const y = padding.top + chartHeight - ratio * chartHeight;
    context.strokeStyle = "rgba(18, 32, 39, 0.08)";
    context.beginPath();
    context.moveTo(padding.left, y);
    context.lineTo(padding.left + chartWidth, y);
    context.stroke();
    const labelValue = (maxValue * ratio).toFixed(0);
    context.fillText(labelValue, 8, y + 4);
  }

  const bucketWidth = chartWidth / monthlyData.length;
  const barWidth = Math.max(8, Math.min(38, bucketWidth * 0.68));
  const labelStep = Math.max(1, Math.ceil(monthlyData.length / 10));

  monthlyData.forEach((item, index) => {
    const value = Number(item.total || 0);
    const barHeight = (value / maxValue) * chartHeight;
    const barX = padding.left + index * bucketWidth + (bucketWidth - barWidth) / 2;
    const barY = padding.top + chartHeight - barHeight;

    const gradient = context.createLinearGradient(0, barY, 0, padding.top + chartHeight);
    gradient.addColorStop(0, "#108576");
    gradient.addColorStop(1, "#79c5b8");
    context.fillStyle = gradient;
    context.fillRect(barX, barY, barWidth, barHeight);

    if (index % labelStep === 0 || index === monthlyData.length - 1) {
      context.fillStyle = "#41555c";
      context.fillText(formatMonthLabel(item.month), barX - 4, padding.top + chartHeight + 14);
    }
  });
}

function renderOptions() {
  const currentCurrency = state.filters.currency;
  const currentCompany = state.filters.company;

  const currencyOptions = [
    '<option value="">All currencies</option>',
    ...state.options.currencies.map(
      (currency) =>
        `<option value="${escapeHtml(currency)}"${currency === currentCurrency ? " selected" : ""}>${escapeHtml(currency)}</option>`
    ),
  ];
  elements.currencySelect.innerHTML = currencyOptions.join("");

  const companyOptions = [
    '<option value="">All companies</option>',
    ...state.options.companies.map(
      (company) =>
        `<option value="${escapeHtml(company)}"${company === currentCompany ? " selected" : ""}>${escapeHtml(company)}</option>`
    ),
  ];
  elements.companySelect.innerHTML = companyOptions.join("");
}

function renderRecords() {
  const records = state.records;
  elements.recordsBody.innerHTML = "";

  if (!records.length) {
    const row = document.createElement("tr");
    row.innerHTML = '<td class="empty-row" colspan="7">No matching records found.</td>';
    elements.recordsBody.appendChild(row);
  } else {
    records.forEach((record) => {
      const row = document.createElement("tr");
      row.dataset.emailId = String(record.email_id);
      if (state.selectedEmailId === record.email_id) {
        row.classList.add("is-selected");
      }
      row.innerHTML = `
        <td>${escapeHtml(record.imap_uid || "-")}</td>
        <td>
          <div class="message-subject">${escapeHtml(record.subject || "(no subject)")}</div>
          <div class="message-sender">${escapeHtml(record.sender || "-")}</div>
        </td>
        <td>${getTypeTag(record)}</td>
        <td>${escapeHtml(record.company || "-")}</td>
        <td>${escapeHtml(formatPrice(record.price, record.currency))}</td>
        <td>${escapeHtml(formatDate(record.invoice_date || record.sent_at))}</td>
        <td>${escapeHtml(`${Math.round(Number(record.confidence || 0) * 100)}%`)}</td>
      `;
      row.addEventListener("click", () => selectRecord(record.email_id));
      elements.recordsBody.appendChild(row);
    });
  }

  const { limit, offset, total } = state.pagination;
  if (!total) {
    elements.resultCount.textContent = "0 records";
    elements.pageLabel.textContent = "Page 0 of 0";
    elements.prevPageButton.disabled = true;
    elements.nextPageButton.disabled = true;
    return;
  }

  const start = offset + 1;
  const end = Math.min(offset + records.length, total);
  const page = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));
  elements.resultCount.textContent = `${formatCount(start)}-${formatCount(end)} of ${formatCount(total)} records`;
  elements.pageLabel.textContent = `Page ${page} of ${totalPages}`;
  elements.prevPageButton.disabled = offset <= 0;
  elements.nextPageButton.disabled = offset + limit >= total;
}

function renderDetail(payload) {
  if (!payload || !payload.record) {
    elements.detailContent.hidden = true;
    elements.detailEmpty.hidden = false;
    elements.rawEmailLink.hidden = true;
    return;
  }

  const record = payload.record;
  elements.detailContent.hidden = false;
  elements.detailEmpty.hidden = true;
  elements.detailTitle.textContent = record.subject || "(no subject)";

  elements.detailBadges.innerHTML = `
    ${getTypeTag(record)}
    <span class="badge badge-unknown">${escapeHtml(`Source: ${record.source_used || "-"}`)}</span>
    <span class="badge badge-unknown">${escapeHtml(`Confidence: ${Math.round(Number(record.confidence || 0) * 100)}%`)}</span>
  `;

  const detailFields = [
    ["UID", record.imap_uid],
    ["Sender", record.sender],
    ["Recipients", record.recipients],
    ["Sent At", formatDate(record.sent_at)],
    ["Invoice Date", formatDate(record.invoice_date)],
    ["Company", record.company || "-"],
    ["Product", record.product || "-"],
    ["Amount", formatPrice(record.price, record.currency)],
    ["VAT", record.vat === null || record.vat === undefined ? "-" : String(record.vat)],
    ["Currency", record.currency || "-"],
    ["Documents", String(payload.documents?.length || 0)],
    ["Links", String(payload.links?.length || 0)],
    ["Extracted At", formatDate(record.extracted_at)],
  ];
  elements.detailGrid.innerHTML = detailFields
    .map(
      ([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value || "-")}</dd>`
    )
    .join("");

  if (record.raw_email_url) {
    elements.rawEmailLink.hidden = false;
    elements.rawEmailLink.href = record.raw_email_url;
  } else {
    elements.rawEmailLink.hidden = true;
  }

  const documents = payload.documents || [];
  if (!documents.length) {
    elements.detailDocuments.innerHTML = "<li>No documents saved.</li>";
  } else {
    elements.detailDocuments.innerHTML = documents
      .map(
        (doc) => `
          <li>
            <a href="${escapeHtml(doc.download_url)}" target="_blank" rel="noopener noreferrer">
              ${escapeHtml(doc.filename || "document")}
            </a>
            <span>(${escapeHtml(doc.kind || "file")})</span>
          </li>
        `
      )
      .join("");
  }

  const links = payload.links || [];
  if (!links.length) {
    elements.detailLinks.innerHTML = "<li>No links stored.</li>";
  } else {
    elements.detailLinks.innerHTML = links
      .map(
        (link) => `
          <li>
            <a href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer">
              ${escapeHtml(link.url)}
            </a>
            <span>(${escapeHtml(link.kind || "other")})</span>
          </li>
        `
      )
      .join("");
  }

  elements.detailJson.textContent = JSON.stringify(record.raw_json || {}, null, 2);
}

async function fetchJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const body = await response.json();
      if (body.error) {
        message = body.error;
      }
    } catch (_error) {
      // Keep default fallback message.
    }
    throw new Error(message);
  }
  return response.json();
}

async function loadSummary() {
  state.summary = await fetchJson("/api/summary");
  renderSummary();
}

async function loadOptions() {
  state.options = await fetchJson("/api/options");
  renderOptions();
}

function buildRecordQuery() {
  const params = new URLSearchParams();
  params.set("limit", String(state.pagination.limit));
  params.set("offset", String(state.pagination.offset));
  params.set("kind", state.filters.kind);
  params.set("sort", state.filters.sort);
  if (state.filters.search) {
    params.set("search", state.filters.search);
  }
  if (state.filters.currency) {
    params.set("currency", state.filters.currency);
  }
  if (state.filters.company) {
    params.set("company", state.filters.company);
  }
  return params.toString();
}

async function loadRecords() {
  const query = buildRecordQuery();
  const payload = await fetchJson(`/api/records?${query}`);
  state.records = payload.records || [];
  state.pagination = payload.pagination || state.pagination;
  renderRecords();

  if (!state.records.length) {
    state.selectedEmailId = null;
    renderDetail(null);
    return;
  }

  const stillVisible = state.records.some((record) => record.email_id === state.selectedEmailId);
  if (!stillVisible) {
    state.selectedEmailId = state.records[0].email_id;
  }
  await loadRecordDetail(state.selectedEmailId);
  renderRecords();
}

async function loadRecordDetail(emailId) {
  if (!emailId) {
    renderDetail(null);
    return;
  }

  const requestId = detailRequestCounter + 1;
  detailRequestCounter = requestId;
  try {
    const payload = await fetchJson(`/api/records/${emailId}`);
    if (requestId !== detailRequestCounter) {
      return;
    }
    renderDetail(payload);
  } catch (error) {
    if (requestId !== detailRequestCounter) {
      return;
    }
    showStatus(error.message, { isError: true });
  }
}

async function selectRecord(emailId) {
  state.selectedEmailId = emailId;
  renderRecords();
  await loadRecordDetail(emailId);
}

function syncFilterStateFromUi() {
  state.filters.search = elements.searchInput.value.trim();
  state.filters.kind = elements.kindSelect.value;
  state.filters.currency = elements.currencySelect.value;
  state.filters.company = elements.companySelect.value;
  state.filters.sort = elements.sortSelect.value;
}

async function applyFilters({ resetOffset }) {
  syncFilterStateFromUi();
  if (resetOffset) {
    state.pagination.offset = 0;
  }

  showStatus("Loading records...");
  try {
    await loadRecords();
    showStatus("");
  } catch (error) {
    showStatus(error.message, { isError: true });
  }
}

function resetFilters() {
  elements.searchInput.value = "";
  elements.kindSelect.value = "all";
  elements.currencySelect.value = "";
  elements.companySelect.value = "";
  elements.sortSelect.value = "newest";
  applyFilters({ resetOffset: true });
}

function bindEvents() {
  elements.filterForm.addEventListener("submit", (event) => event.preventDefault());

  elements.searchInput.addEventListener("input", () => {
    if (searchTimerId !== null) {
      window.clearTimeout(searchTimerId);
    }
    searchTimerId = window.setTimeout(() => {
      applyFilters({ resetOffset: true });
    }, 260);
  });

  elements.kindSelect.addEventListener("change", () => applyFilters({ resetOffset: true }));
  elements.currencySelect.addEventListener("change", () => applyFilters({ resetOffset: true }));
  elements.companySelect.addEventListener("change", () => applyFilters({ resetOffset: true }));
  elements.sortSelect.addEventListener("change", () => applyFilters({ resetOffset: true }));

  elements.resetFiltersButton.addEventListener("click", resetFilters);

  elements.prevPageButton.addEventListener("click", async () => {
    if (state.pagination.offset <= 0) {
      return;
    }
    state.pagination.offset = Math.max(0, state.pagination.offset - state.pagination.limit);
    await applyFilters({ resetOffset: false });
  });

  elements.nextPageButton.addEventListener("click", async () => {
    if (state.pagination.offset + state.pagination.limit >= state.pagination.total) {
      return;
    }
    state.pagination.offset += state.pagination.limit;
    await applyFilters({ resetOffset: false });
  });

  window.addEventListener("resize", () => {
    if (resizeTimerId !== null) {
      window.clearTimeout(resizeTimerId);
    }
    resizeTimerId = window.setTimeout(() => {
      drawTrend(state.summary?.monthly || []);
    }, 140);
  });
}

async function initialize() {
  bindEvents();
  showStatus("Loading dashboard...");
  try {
    await Promise.all([loadSummary(), loadOptions()]);
    await applyFilters({ resetOffset: true });
    showStatus("");
  } catch (error) {
    showStatus(error.message, { isError: true });
  }
}

initialize();
