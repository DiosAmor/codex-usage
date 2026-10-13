"use strict";

// Keep date keys as the server supplied them. Only the range's "today" uses KST.
(() => {
  const INTEGER = /^\d{1,64}$/;
  const DATE = /^\d{4}-\d{2}-\d{2}$/;
  const KST_PARTS = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" });
  const EXACT = new Intl.NumberFormat("ko-KR");
  function tokens(value) { return typeof value === "string" && INTEGER.test(value) ? BigInt(value) : null; }
  function validDate(key) {
    if (typeof key !== "string" || !DATE.test(key) || key.slice(0, 4) === "0000") return false;
    const date = new Date(`${key}T00:00:00Z`);
    return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === key;
  }
  function todayKey(now = Date.now()) {
    const parts = Object.fromEntries(KST_PARTS.formatToParts(new Date(now)).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  function shiftDay(key, amount) {
    if (!validDate(key) || !Number.isInteger(amount)) return null;
    const date = new Date(`${key}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + amount);
    return date.toISOString().slice(0, 10);
  }
  function records(account) {
    const rows = new Map();
    for (const row of Array.isArray(account?.tokenUsage?.daily) ? account.tokenUsage.daily : []) {
      if (row && validDate(row.date)) rows.set(row.date, tokens(row.tokens));
    }
    return rows;
  }
  function periodDates(accounts, period, today = todayKey()) {
    if (!validDate(today)) return [];
    if (period === "7" || period === "30") {
      const count = Number(period), dates = [];
      for (let offset = count - 1; offset >= 0; offset--) dates.push(shiftDay(today, -offset));
      return dates;
    }
    if (period !== "all") return [];
    const dates = new Set();
    for (const account of accounts) for (const date of records(account).keys()) if (date <= today) dates.add(date);
    return [...dates].sort();
  }
  function cell(rows, date) {
    if (!rows.has(date)) return { kind: "missing", value: null };
    const value = rows.get(date);
    return value === null ? { kind: "unknown", value: null } : { kind: "known", value };
  }
  function summary(account, dates) {
    const rows = records(account);
    let total = 0n, known = 0, unknown = 0, missing = 0, peak = null;
    for (const date of dates) {
      const entry = cell(rows, date);
      if (entry.kind === "missing") { missing++; continue; }
      if (entry.kind === "unknown") { unknown++; continue; }
      known++; total += entry.value;
      // On ties, the most recent date is the representative peak day.
      if (!peak || entry.value >= peak.value) peak = { date, value: entry.value };
    }
    const healthy = account?.tokenUsage?.status === "ok" && !account.tokenUsage.error;
    return { total: known ? total : null, peak, known, unknown, missing, complete: healthy && dates.length > 0 && unknown === 0 && missing === 0 };
  }
  function exact(value) { return value === null ? "확인 불가" : EXACT.format(value); }
  function compact(value) {
    if (value === null) return "확인 불가";
    const units = [[1000000000000n, "조"], [100000000n, "억"], [10000n, "만"]];
    for (const [unit, suffix] of units) if (value >= unit) {
      const tenth = (value * 10n + unit / 2n) / unit;
      return `${EXACT.format(tenth / 10n)}${tenth % 10n ? `.${tenth % 10n}` : ""}${suffix}`;
    }
    return exact(value);
  }
  const helpers = { tokens, validDate, todayKey, shiftDay, records, periodDates, cell, summary, exact, compact };
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (typeof document === "undefined" || typeof window === "undefined") return;
  const el = id => document.getElementById(id);
  if (!el("daily-title")) return;
  const str = (value, fallback = "") => typeof value === "string" ? value : fallback;
  const state = { accounts: [], selectedIds: new Set(), seenIds: new Set(), tones: new Map(), period: "30", selectedDate: null, hasKnownSelection: false, loaded: false, signature: null, filterSignature: null, demo: false };
  function node(tag, props = {}, ...children) {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === "className") element.className = value;
      else if (key.startsWith("on") && typeof value === "function") element.addEventListener(key.slice(2).toLowerCase(), value);
      else if (key in element && !key.startsWith("aria")) element[key] = value;
      else element.setAttribute(key, String(value));
    }
    for (const child of children.flat()) if (child !== null && child !== undefined) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    return element;
  }
  function svgNode(tag, attrs = {}, text = null) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
    if (text !== null) element.textContent = text;
    return element;
  }
  function tone(account) { return `tone-${state.tones.get(account.id) % 3}`; }
  function label(account) { return str(account.label, "계정"); }
  function email(account) { return str(account.email).trim() || "이메일 확인 불가"; }
  function plan(account) {
    const value = str(account.plan).trim();
    const names = { free: "Free", plus: "Plus", pro: "Pro", prolite: "Pro Lite", team: "Team", business: "Business", enterprise: "Enterprise", self_serve_business_prolite: "Business · Pro Lite", self_serve_business_pro: "Business · Pro" };
    return names[value.toLowerCase()] || (value && value !== "unknown" ? value.replaceAll("_", " ") : "요금제 확인 불가");
  }
  function identityText(account) { return `${label(account)} · ${email(account)} · ${plan(account)}`; }
  function identity(account) {
    return node("span", { className: "daily-identity" },
      node("span", { className: "daily-identity-name" }, label(account)),
      node("span", { className: "daily-identity-details" }, node("span", { className: "daily-identity-email" }, email(account)), node("span", { className: "daily-identity-plan" }, plan(account))));
  }
  function dot(account) { return node("i", { className: `daily-dot ${tone(account)}`, "aria-hidden": "true" }); }
  function dateText(date, short = false) { return short ? `${Number(date.slice(5, 7))}/${Number(date.slice(8))}` : `${date.slice(0, 4)}.${date.slice(5, 7)}.${date.slice(8)}`; }
  function timestamp(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return null;
    const date = new Date(value * 1000);
    if (Number.isNaN(date.getTime())) return null;
    return new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }
  function cellText(entry) { return entry.kind === "known" ? exact(entry.value) : entry.kind === "unknown" ? "확인 불가" : "기록 없음"; }
  function selectedAccounts() { return state.accounts.filter(account => state.selectedIds.has(account.id)); }
  function revealChartDay(group) {
    const viewport = el("daily-chart-scroll"), box = viewport.getBoundingClientRect(), day = group.getBoundingClientRect();
    const left = box.left + viewport.clientLeft + 10, right = box.left + viewport.clientLeft + viewport.clientWidth - 10;
    // Move this horizontal viewport only; scrolling the page would interrupt
    // keyboard navigation or a date selection made in the table below.
    if (day.left < left) viewport.scrollLeft -= left - day.left;
    else if (day.right > right) viewport.scrollLeft += day.right - right;
  }
  function chooseDate(date, focus = false) {
    state.selectedDate = date; state.hasKnownSelection = true;
    for (const item of el("daily-chart").querySelectorAll("[data-daily-date]")) {
      const chosen = item.dataset.dailyDate === date;
      item.classList.toggle("is-selected", chosen); item.setAttribute("aria-pressed", String(chosen)); item.setAttribute("tabindex", chosen ? "0" : "-1");
      const dateLabel = item.querySelector(".daily-date-label");
      dateLabel?.setAttribute("visibility", chosen || dateLabel.dataset.dailyTick === "true" ? "visible" : "hidden");
      if (chosen) {
        if (focus) item.focus({ preventScroll: true });
        revealChartDay(item);
      }
    }
    for (const row of el("daily-table").querySelectorAll("tbody tr[data-daily-row]")) {
      const chosen = row.dataset.dailyRow === date;
      row.classList.toggle("is-selected", chosen); row.querySelector("button")?.setAttribute("aria-pressed", String(chosen));
    }
    renderDay(selectedAccounts());
  }
  function renderFilters() {
    const signature = JSON.stringify(state.accounts.map(account => [account.id, label(account), account.email, account.plan]));
    if (signature === state.filterSignature) return;
    const focusedId = document.activeElement?.dataset.dailyAccount;
    const filters = state.accounts.map(account => node("label", { className: "daily-account-filter" }, node("input", { type: "checkbox", checked: state.selectedIds.has(account.id), "data-daily-account": account.id, "aria-label": identityText(account), onchange: event => {
      if (event.target.checked) state.selectedIds.add(account.id); else state.selectedIds.delete(account.id);
      render();
    } }), dot(account), identity(account)));
    el("daily-account-filters").replaceChildren(...filters); state.filterSignature = signature;
    if (focusedId) [...el("daily-account-filters").querySelectorAll("input")].find(input => input.dataset.dailyAccount === focusedId)?.focus({ preventScroll: true });
  }
  function renderNotice(accounts) {
    const notices = [];
    if (state.demo) notices.push("데모 · 표시된 일별 토큰 수는 실제 사용량이 아닌 예시입니다.");
    if (!state.loaded) notices.push("일별 사용 기록을 불러오고 있어요.");
    else if (!state.accounts.length) notices.push("계정이 연결되면 기존 일별 사용 기록을 불러옵니다.");
    else if (!accounts.length) notices.push("비교할 계정을 하나 이상 선택해 주세요.");
    for (const account of accounts) {
      const usage = account.tokenUsage || {}, hasRows = records(account).size > 0, fetched = timestamp(usage.fetchedAt);
      if (usage.status === "error" || usage.error) notices.push(`${label(account)}: 일별 기록을 갱신하지 못했어요.${hasRows ? ` 이전 조회 기록을 표시합니다${fetched ? ` (${fetched}, KST)` : ""}.` : ""} ${str(usage.error)}`.trim());
      else if (usage.status === "unavailable") notices.push(`${label(account)}: 일별 토큰 기록이 제공되지 않았어요.${hasRows ? " 이전 조회 기록을 표시합니다." : ""}`);
      else if (!usage.status || usage.status === "pending") notices.push(`${label(account)}: 일별 기록을 ${hasRows ? "갱신" : "불러오는"} 중이에요.`);
    }
    const notice = el("daily-notice"); notice.textContent = notices.join(" "); notice.hidden = !notices.length; notice.classList.toggle("is-demo", state.demo);
  }
  function renderSummaries(accounts, dates) {
    const cards = accounts.map(account => {
      const value = summary(account, dates), fetched = timestamp(account.tokenUsage?.fetchedAt);
      const total = node("div", { className: `daily-summary-value${value.total === null ? " is-unknown" : value.total < 10000n ? " is-exact" : ""}`, title: value.total === null ? "확인된 사용 기록이 없습니다." : `${exact(value.total)} 토큰` }, value.total === null ? "기록 확인 필요" : compact(value.total), value.total === null ? null : node("small", {}, "토큰"));
      const peakText = value.peak ? `${value.complete ? "가장 많이 사용한 날" : "확인된 최고 사용일"} ${dateText(value.peak.date, true)} · ${compact(value.peak.value)} 토큰` : "최고 사용일 확인 불가";
      const coverage = [`${dates.length}일 중 ${value.known}일 기록 확인`, ...(value.missing ? [`${value.missing}일 기록 없음`] : []), ...(value.unknown ? [`${value.unknown}일 값 확인 불가`] : [])];
      return node("article", { className: `daily-summary ${tone(account)}` }, node("h3", {}, dot(account), identity(account)), node("div", { className: "daily-summary-label" }, value.complete ? "선택한 기간 합계" : "확인된 합계 · 제공된 기록 기준"), total, node("p", { className: "daily-summary-peak", title: value.peak ? `${dateText(value.peak.date)} · ${exact(value.peak.value)} 토큰. 제공된 기록 기준입니다.` : "확인된 사용 기록이 없습니다." }, peakText), node("p", { className: "daily-summary-coverage" }, coverage.join(" · "), fetched ? node("br") : null, fetched ? `일별 기록 확인 ${fetched} (KST)` : null));
    });
    el("daily-summaries").replaceChildren(...cards);
  }
  function axisLabel(value) {
    if (value === 0) return "0";
    if (value < 1) return value.toFixed(1);
    if (value >= 1e16) return value.toExponential(1).replace("e+", "e").replace(".0e", "e");
    return compact(BigInt(Math.round(value)));
  }
  function renderChart(accounts, dates) {
    const holder = el("daily-chart");
    if (!accounts.length || !dates.length) {
      holder.replaceChildren(node("div", { className: "daily-chart-empty" }, accounts.length ? "아직 제공된 일별 기록이 없어요." : "계정을 선택하면 일별 사용량이 표시됩니다.", node("p", {}, accounts.length ? "조회가 끝나면 사용 기록이 있는 날짜를 비교할 수 있어요." : "위의 계정 이름을 선택해 주세요."))); return;
    }
    const dayWidth = Math.max(window.matchMedia("(max-width: 540px)").matches ? 42 : 31, accounts.length * 7 + 10);
    const maps = accounts.map(records), width = Math.max(el("daily-chart-scroll").clientWidth, dates.length * dayWidth + 70), plotLeft = 57, plotRight = width - 12, plotBottom = 237, plotTop = 23, plotHeight = plotBottom - plotTop, groupWidth = (plotRight - plotLeft) / dates.length;
    let maximum = 0;
    for (const rows of maps) for (const date of dates) { const entry = cell(rows, date); if (entry.kind === "known") maximum = Math.max(maximum, Number(entry.value)); }
    // Approximation is confined to geometry; displayed amounts remain BigInt.
    const ceiling = maximum > 0 ? maximum * 1.12 : 1, svg = svgNode("svg", { class: "daily-svg", width, height: 285, viewBox: `0 0 ${width} 285`, "aria-labelledby": "daily-svg-title", "aria-describedby": "daily-chart-note" });
    svg.append(svgNode("title", { id: "daily-svg-title" }, "계정별 일별 토큰 사용량 막대그래프. 화살표 키로 날짜를 선택할 수 있습니다. 정확한 값은 날짜 상세와 아래 표에 표시됩니다."));
    for (let tick = 0; tick <= 4; tick++) {
      const y = plotBottom - plotHeight * tick / 4;
      svg.append(svgNode("line", { class: tick ? "daily-axis-line" : "daily-baseline", x1: plotLeft, x2: plotRight, y1: y, y2: y }), svgNode("text", { class: "daily-axis-text", x: plotLeft - 10, y: y + 3, "text-anchor": "end" }, maximum > 0 || tick === 0 ? axisLabel(ceiling * tick / 4) : ""));
    }
    const stride = Math.max(1, Math.ceil(dates.length / 20));
    dates.forEach((date, index) => {
      const left = plotLeft + index * groupWidth, chosen = date === state.selectedDate, description = accounts.map((account, ai) => `${identityText(account)}: ${cellText(cell(maps[ai], date))}${cell(maps[ai], date).kind === "known" ? " 토큰" : ""}`).join(", ");
      const group = svgNode("g", { class: `daily-day-group${chosen ? " is-selected" : ""}`, "data-daily-date": date, role: "button", tabindex: chosen ? "0" : "-1", "aria-pressed": chosen, "aria-label": `${dateText(date)}. ${description}` });
      group.append(svgNode("title", {}, `${dateText(date)}\n${description}`), svgNode("rect", { class: "daily-day-hit", x: left + 1, y: plotTop - 10, width: groupWidth - 2, height: plotHeight + 39, rx: 4 }));
      const barWidth = Math.min(13, Math.max(3, (groupWidth - 11) / accounts.length - 2)), step = barWidth + 2, groupOffset = (groupWidth - step * accounts.length + 2) / 2;
      accounts.forEach((account, ai) => {
        const entry = cell(maps[ai], date), x = left + groupOffset + ai * step;
        if (entry.kind === "known") {
          const height = entry.value === 0n ? 1 : Math.max(1.8, Number(entry.value) / ceiling * plotHeight);
          const bar = svgNode("rect", { class: `daily-bar ${tone(account)}`, x, y: plotBottom - height, width: barWidth, height, rx: Math.min(2, height / 2) });
          bar.append(svgNode("title", {}, `${identityText(account)} · ${dateText(date)} · ${exact(entry.value)} 토큰`)); group.append(bar);
        } else if (entry.kind === "unknown") group.append(svgNode("circle", { class: "daily-unavailable", cx: x + barWidth / 2, cy: plotBottom - 5, r: Math.min(2.6, barWidth / 2) }));
        else group.append(svgNode("text", { class: "daily-no-data", x: x + barWidth / 2, y: plotBottom - 2, "text-anchor": "middle" }, "–"));
      });
      const tick = index % stride === 0 || index === dates.length - 1;
      group.append(svgNode("text", { class: "daily-date-label", "data-daily-tick": tick, visibility: tick || chosen ? "visible" : "hidden", x: left + groupWidth / 2, y: plotBottom + 20, "text-anchor": "middle" }, dateText(date, true)));
      group.addEventListener("click", () => chooseDate(date));
      group.addEventListener("keydown", event => {
        let next = null;
        if (event.key === "ArrowRight") next = dates[Math.min(dates.length - 1, index + 1)];
        else if (event.key === "ArrowLeft") next = dates[Math.max(0, index - 1)];
        else if (event.key === "Home") next = dates[0];
        else if (event.key === "End") next = dates[dates.length - 1];
        else if (event.key === "Enter" || event.key === " ") next = date;
        if (next) { event.preventDefault(); chooseDate(next, true); }
      });
      svg.append(group);
    });
    holder.replaceChildren(svg);
  }
  function renderDay(accounts) {
    const date = state.selectedDate, holder = el("daily-day-detail");
    holder.hidden = !date || !accounts.length;
    if (!date || !accounts.length) { holder.replaceChildren(); return; }
    const title = node("div", { className: "daily-day-title" }, node("span", {}, "선택한 날짜 · 서버 집계"), node("h3", { id: "daily-selected-title" }, dateText(date)));
    const values = accounts.map(account => {
      const entry = cell(records(account), date);
      return node("div", { className: `daily-day-value${entry.kind !== "known" ? " is-unknown" : ""}` }, node("span", { className: "daily-day-value-label" }, dot(account), identity(account)), node("strong", {}, cellText(entry), entry.kind === "known" ? node("small", {}, "토큰") : null));
    });
    holder.replaceChildren(title, node("div", { className: "daily-day-values" }, ...values));
  }
  function renderTable(accounts, dates) {
    const table = el("daily-table"), head = node("thead", {}, node("tr", {}, node("th", { scope: "col" }, "집계 날짜"), ...accounts.map(account => node("th", { scope: "col" }, node("span", {}, dot(account), identity(account))))));
    const maps = accounts.map(records), body = node("tbody");
    if (!accounts.length || !dates.length) body.append(node("tr", {}, node("td", { colSpan: Math.max(1, accounts.length + 1), className: "daily-table-empty" }, accounts.length ? "제공된 기록이 없습니다." : "표시할 계정을 선택해 주세요.")));
    else [...dates].reverse().forEach(date => {
      const chosen = date === state.selectedDate, cells = maps.map(rows => { const entry = cell(rows, date); return node("td", { className: entry.kind !== "known" ? "is-unknown" : "" }, cellText(entry)); });
      body.append(node("tr", { className: chosen ? "is-selected" : "", "data-daily-row": date }, node("td", {}, node("button", { type: "button", className: "daily-table-date", "data-daily-table-date": date, "aria-pressed": chosen, "aria-label": `${dateText(date)} 상세 보기`, onclick: () => chooseDate(date) }, dateText(date))), ...cells));
    });
    table.replaceChildren(node("caption", { className: "sr-only" }, "서버 집계 날짜별 계정 토큰 수. 기록 없음은 제공된 기록이 없는 날짜, 확인 불가는 기록의 숫자를 확인할 수 없는 날짜입니다."), head, body);
  }
  function render() {
    renderFilters();
    const accounts = selectedAccounts(), dates = periodDates(accounts, state.period), active = document.activeElement, focusedChart = active?.dataset.dailyDate, focusedTable = active?.dataset.dailyTableDate, chartScroll = el("daily-chart-scroll").scrollLeft, tableScroll = [el("daily-table-scroll").scrollTop, el("daily-table-scroll").scrollLeft];
    if (!dates.includes(state.selectedDate) || !state.hasKnownSelection) {
      const latestKnown = [...dates].reverse().find(date => accounts.some(account => cell(records(account), date).kind === "known"));
      state.selectedDate = latestKnown || dates.at(-1) || null;
      if (latestKnown) state.hasKnownSelection = true;
    }
    document.querySelectorAll("[data-daily-period]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.dailyPeriod === state.period)));
    el("daily-range").textContent = dates.length ? `${dateText(dates[0])} — ${dateText(dates.at(-1))}${state.period === "all" ? " · 제공된 날짜" : " · 한국의 오늘 기준"}` : "조회된 집계 날짜 없음";
    renderNotice(accounts); renderSummaries(accounts, dates); renderChart(accounts, dates); renderDay(accounts); renderTable(accounts, dates);
    const known = accounts.some(account => dates.some(date => cell(records(account), date).kind === "known"));
    el("daily-chart-note").textContent = `${known ? "– 기록 없음 · ○ 값 확인 불가 · 0은 기준선의 작은 막대로 표시합니다." : "확인된 숫자가 아직 없어 막대를 표시하지 않습니다."} 날짜는 서버에서 제공한 집계 날짜이며, 한국 시간으로 다시 변환하지 않습니다.`;
    el("daily-chart-scroll").scrollLeft = chartScroll; el("daily-table-scroll").scrollTop = tableScroll[0]; el("daily-table-scroll").scrollLeft = tableScroll[1];
    if (focusedChart) [...el("daily-chart").querySelectorAll("[data-daily-date]")].find(item => item.dataset.dailyDate === focusedChart)?.focus({ preventScroll: true });
    if (focusedTable) [...el("daily-table").querySelectorAll("[data-daily-table-date]")].find(item => item.dataset.dailyTableDate === focusedTable)?.focus({ preventScroll: true });
  }
  function receive(snapshot) {
    const seen = new Set(), accounts = (Array.isArray(snapshot?.accounts) ? snapshot.accounts : []).filter(account => {
      if (!account || typeof account.id !== "string" || seen.has(account.id)) return false;
      seen.add(account.id); return true;
    });
    const signature = JSON.stringify({ loaded: !!snapshot, demo: !!snapshot?.demo, accounts: accounts.map(account => ({ id: account.id, label: account.label, email: account.email, plan: account.plan, tokenUsage: account.tokenUsage })) });
    if (signature === state.signature) return;
    for (const account of accounts) if (!state.seenIds.has(account.id)) { state.seenIds.add(account.id); state.selectedIds.add(account.id); state.tones.set(account.id, state.tones.size); }
    state.accounts = accounts; state.loaded = !!snapshot; state.demo = !!snapshot?.demo; state.signature = signature; render();
  }
  document.querySelectorAll("[data-daily-period]").forEach(button => button.addEventListener("click", () => { state.period = button.dataset.dailyPeriod; render(); }));
  window.addEventListener("codex-account-state", event => receive(event.detail));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) render(); });
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 100); });
  receive(window.codexAccountState || null);
})();
