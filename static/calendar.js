"use strict";

(() => {
  const el = (id) => document.getElementById(id);
  const finite = (value) => typeof value === "number" && Number.isFinite(value) ? value : null;
  const str = (value, fallback = "") => typeof value === "string" ? value : fallback;
  const KST = "Asia/Seoul";
  const DATE_PARTS = new Intl.DateTimeFormat("en-CA", { timeZone: KST, year: "numeric", month: "2-digit", day: "2-digit" });
  const KIND_NAMES = { regular: "사용량 초기화", banked: "초기화권 지급", scheduled: "예정 공지", prediction: "예측", account: "내 한도 갱신", expiry: "초기화권 만료" };
  const state = { feed: null, accounts: window.codexAccountState || null, fetching: false, signature: "", events: [], undated: [], month: null, selected: null, expiredWatch: false, connectionError: null };
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
  function dateKey(epoch) {
    if (finite(epoch) === null || epoch <= 0) return null;
    const date = new Date(epoch * 1000);
    if (Number.isNaN(date.getTime())) return null;
    const parts = Object.fromEntries(DATE_PARTS.formatToParts(date).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  function keyDate(key) { return new Date(`${key}T00:00:00Z`); }
  function isoKey(date) { return date.toISOString().slice(0, 10); }
  function shiftDay(key, amount) { const date = keyDate(key); date.setUTCDate(date.getUTCDate() + amount); return isoKey(date); }
  function shiftMonth(key, amount) {
    const date = keyDate(key), day = date.getUTCDate();
    date.setUTCDate(1); date.setUTCMonth(date.getUTCMonth() + amount);
    const last = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
    date.setUTCDate(Math.min(day, last)); return isoKey(date);
  }
  function prettyTime(epoch, full = false) {
    if (!dateKey(epoch)) return "시각 확인 불가";
    return new Intl.DateTimeFormat("ko-KR", { timeZone: KST, ...(full ? { year: "numeric", month: "numeric", day: "numeric" } : {}), hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(epoch * 1000));
  }
  function sourceLink(url) {
    try {
      const parsed = new URL(url);
      return parsed.protocol === "https:" && ["x.com", "twitter.com", "codex-resets.com"].includes(parsed.hostname) && !parsed.username && !parsed.password ? parsed.href : null;
    } catch { return null; }
  }
  function accountWindowName(window) {
    const duration = finite(window.windowDurationMins);
    if (duration === 10080) return "주간 한도";
    if (duration === 300) return "5시간 한도";
    if (duration === null) return "사용 한도";
    return duration % 1440 === 0 ? `${duration / 1440}일 한도` : duration % 60 === 0 ? `${duration / 60}시간 한도` : `${duration}분 한도`;
  }
  function buildEvents() {
    const now = Date.now() / 1000, events = [], undated = [], seen = new Set();
    state.expiredWatch = false;
    const feed = state.feed;
    const feedEvents = [...(Array.isArray(feed?.events) ? feed.events : []), ...(Array.isArray(feed?.history) ? feed.history : []), feed?.activeWatch, feed?.scheduledReset].filter(Boolean);
    for (const original of feedEvents) {
      if (!Object.hasOwn(KIND_NAMES, original.kind) || ["account", "expiry"].includes(original.kind)) continue;
      const key = `${original.kind}:${str(original.id)}`;
      if (seen.has(key)) continue; seen.add(key);
      if (original.kind === "prediction" && finite(original.expiresAt) !== null && original.expiresAt <= now) { state.expiredWatch = true; continue; }
      // Only use forecast dates normalized by the backend: structured dates or
      // recognized relative forecast windows anchored to their observation time.
      // An observation/expiry pair alone never becomes a predicted reset window.
      const event = { ...original, title: str(original.title, KIND_NAMES[original.kind]), startsAt: finite(original.startsAt) ?? finite(original.forecastStartsAt), endsAt: finite(original.endsAt) ?? finite(original.forecastEndsAt), sourceUrl: sourceLink(original.sourceUrl), accountLabel: null };
      if (dateKey(event.startsAt)) events.push(event); else if (["prediction", "scheduled"].includes(event.kind)) undated.push(event);
    }
    const accounts = Array.isArray(state.accounts?.accounts) ? state.accounts.accounts : [];
    accounts.forEach((account) => {
      const label = str(account.label, "계정"), stale = !!account.error || account.status === "error";
      (Array.isArray(account.buckets) ? account.buckets : []).forEach((bucket, bucketIndex) => {
        [bucket.primary, bucket.secondary].forEach((window, windowIndex) => {
          if (!window || !dateKey(window.resetsAt)) return;
          events.push({ id: `account-${account.id}-${bucket.id || bucketIndex}-${windowIndex}`, kind: "account", title: `${label} · ${accountWindowName(window)}`, accountLabel: label, startsAt: window.resetsAt, endsAt: null, remainingPercent: finite(window.remainingPercent), text: str(bucket.name), stale, lastSuccessAt: finite(account.lastSuccessAt), sourceUrl: null });
        });
      });
      (Array.isArray(account.resetCredits?.credits) ? account.resetCredits.credits : []).forEach((credit, index) => {
        if (["used", "consumed", "redeemed", "expired", "cancelled", "unavailable"].includes(str(credit.status).toLowerCase())) return;
        if (dateKey(credit.expiresAt)) events.push({ id: `expiry-${account.id}-${index}`, kind: "expiry", title: `${label} · 초기화권 만료`, startsAt: credit.expiresAt, endsAt: null, text: str(credit.title, "초기화권"), accountLabel: label, stale, sourceUrl: null });
      });
    });
    state.events = events.sort((a, b) => a.startsAt - b.startsAt || a.kind.localeCompare(b.kind));
    state.undated = undated;
  }
  function eventsOn(key) {
    return state.events.filter(event => {
      const start = dateKey(event.startsAt);
      const end = event.kind === "prediction" && finite(event.endsAt) !== null && event.endsAt >= event.startsAt ? dateKey(event.endsAt) : start;
      return start <= key && key <= end;
    });
  }
  function phaseLabel(event) {
    if (event.kind === "scheduled") return finite(event.startsAt) !== null && event.startsAt <= Date.now() / 1000 ? "진행 확인 대기" : "예정 공지";
    if (event.kind === "prediction") return finite(event.endsAt) !== null && event.endsAt <= Date.now() / 1000 ? "예측 구간 지남" : "예측 · 미확정";
    return KIND_NAMES[event.kind];
  }
  function eventDetail(event, undated = false) {
    const wrapper = node("article", { className: `calendar-event-detail ${event.kind}` });
    wrapper.append(node("div", { className: "calendar-event-meta" }, node("span", { className: `calendar-event-badge ${event.kind}` }, phaseLabel(event)), undated ? node("span", { className: "calendar-event-time" }, "날짜 미제공") : node("time", { className: "calendar-event-time", dateTime: new Date(event.startsAt * 1000).toISOString() }, prettyTime(event.startsAt))));
    wrapper.append(node("h4", {}, event.title));
    if (event.kind === "prediction") {
      const chance = finite(event.chancePercent);
      wrapper.append(node("p", { className: "calendar-prediction-chance" }, chance === null ? "확률 확인 불가" : `예측 확률 ${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 }).format(chance)}%`));
      if (str(event.forecastWindow)) wrapper.append(node("p", {}, event.forecastWindow));
      if (finite(event.endsAt) !== null && !undated) wrapper.append(node("p", { className: "calendar-event-secondary" }, `예측 구간: ${prettyTime(event.startsAt, true)} ~ ${prettyTime(event.endsAt, true)} (KST)`));
      if (event.derivedWindow && !undated) wrapper.append(node("p", { className: "calendar-event-secondary" }, "상대 시간 표현으로 계산한 참고 구간이에요."));
      if (finite(event.expiresAt) !== null) wrapper.append(node("p", { className: "calendar-event-secondary" }, `예측 유효 시각: ${prettyTime(event.expiresAt, true)} (KST)`));
      if (finite(event.endsAt) !== null && event.endsAt <= Date.now() / 1000) wrapper.append(node("p", { className: "calendar-event-secondary" }, "예측 구간이 지났어요. 실제 적용 여부는 계정 사용량에서 확인하세요."));
      if (undated) wrapper.append(node("p", { className: "calendar-event-secondary" }, "정확한 예측 날짜가 제공되지 않아 달력에 배치하지 않았어요."));
    }
    if (event.kind === "account") {
      const remaining = finite(event.remainingPercent);
      wrapper.append(node("p", {}, remaining === null ? "현재 잔여량 확인 불가" : `현재 ${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 }).format(remaining)}% 남음`));
      if (event.startsAt <= Date.now() / 1000) wrapper.append(node("p", { className: "calendar-event-secondary" }, "갱신 시각에 도달했어요. 다음 계정 조회에서 반영 여부를 확인합니다."));
    }
    if (str(event.text)) wrapper.append(node("p", { className: "calendar-event-text" }, event.text));
    if (event.stale) wrapper.append(node("p", { className: "calendar-event-secondary" }, "계정 조회 실패 · 이전 조회 일정입니다."));
    if (event.sourceUrl) wrapper.append(node("a", { className: "calendar-event-link", href: event.sourceUrl, target: "_blank", rel: "noopener noreferrer" }, "원문 확인 ↗"));
    return wrapper;
  }
  function renderDetails() {
    const date = keyDate(state.selected), events = eventsOn(state.selected);
    el("calendar-selected-weekday").textContent = new Intl.DateTimeFormat("ko-KR", { timeZone: "UTC", weekday: "long" }).format(date).toUpperCase();
    el("calendar-selected-title").textContent = `${date.getUTCMonth() + 1}월 ${date.getUTCDate()}일`;
    el("calendar-selected-subtitle").textContent = events.length ? `${events.length}개 일정 · 한국 시간` : "등록된 일정이 없는 날이에요.";
    el("calendar-day-events").replaceChildren(...(events.length ? events.map(event => eventDetail(event)) : [node("div", { className: "calendar-no-events" }, node("span", { "aria-hidden": "true" }, "—"), node("p", {}, "날짜를 선택하면 초기화 공지와 계정별 갱신 시각을 확인할 수 있어요."))]));
  }
  function chooseDay(key, focus = false) {
    state.selected = key; state.month = `${key.slice(0, 7)}-01`; renderCalendar();
    if (focus) [...el("calendar-days").querySelectorAll("button[data-date]")].find(button => button.dataset.date === key)?.focus({ preventScroll: true });
  }
  function keyNavigation(event, key) {
    const moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
    let next = null;
    if (Object.hasOwn(moves, event.key)) next = shiftDay(key, moves[event.key]);
    else if (event.key === "Home") next = shiftDay(key, -keyDate(key).getUTCDay());
    else if (event.key === "End") next = shiftDay(key, 6 - keyDate(key).getUTCDay());
    else if (event.key === "PageUp") next = shiftMonth(key, -1);
    else if (event.key === "PageDown") next = shiftMonth(key, 1);
    if (next) { event.preventDefault(); chooseDay(next, true); }
  }
  function renderCalendar() {
    const focused = document.activeElement?.dataset.date, first = keyDate(state.month), today = dateKey(Date.now() / 1000);
    const start = shiftDay(state.month, -first.getUTCDay()), last = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0));
    const rows = Math.ceil((first.getUTCDay() + last.getUTCDate()) / 7);
    el("calendar-month").textContent = `${first.getUTCFullYear()}년 ${first.getUTCMonth() + 1}월`;
    const body = [];
    for (let row = 0; row < rows; row++) {
      const tr = node("tr");
      for (let col = 0; col < 7; col++) {
        const key = shiftDay(start, row * 7 + col), date = keyDate(key), events = eventsOn(key), selected = state.selected === key, isToday = key === today;
        const button = node("button", { type: "button", className: `calendar-date${key.slice(0, 7) !== state.month.slice(0, 7) ? " outside" : ""}${events.some(item => item.kind === "prediction") ? " has-prediction" : ""}${selected ? " selected" : ""}${isToday ? " is-today" : ""}`, "data-date": key, tabIndex: selected ? 0 : -1, "aria-label": `${date.getUTCFullYear()}년 ${date.getUTCMonth() + 1}월 ${date.getUTCDate()}일${isToday ? ", 오늘" : ""}, 일정 ${events.length}개`, "aria-pressed": selected, ...(isToday ? { "aria-current": "date" } : {}), onclick: () => chooseDay(key), onkeydown: (event) => keyNavigation(event, key) }, node("span", { className: "calendar-date-number" }, date.getUTCDate(), isToday ? node("span", { className: "calendar-today-label" }, "오늘") : null));
        const eventStack = node("span", { className: "calendar-cell-events", "aria-hidden": "true" });
        events.slice(0, 2).forEach(item => eventStack.append(node("span", { className: `calendar-event-chip ${item.kind}`, title: item.title }, node("i", { className: `event-dot ${item.kind}` }), node("span", { className: "calendar-chip-text" }, item.kind === "account" ? `${item.accountLabel} 갱신` : phaseLabel(item)))));
        if (events.length > 2) eventStack.append(node("span", { className: "calendar-more" }, `+${events.length - 2}개`));
        button.append(eventStack); tr.append(node("td", { "aria-selected": selected }, button));
      }
      body.push(tr);
    }
    el("calendar-days").replaceChildren(...body);
    if (focused) [...el("calendar-days").querySelectorAll("button[data-date]")].find(button => button.dataset.date === focused)?.focus({ preventScroll: true });
    renderDetails();
  }
  function renderFeedStatus() {
    const feed = state.feed, fetched = finite(feed?.fetchedAt), generated = finite(feed?.generatedAt), error = state.connectionError || str(feed?.error), notice = el("calendar-notice");
    const notices = [];
    if (feed?.demo) notices.push("캘린더 데모 · 표시된 공지와 예측은 실제 일정이 아닌 예시입니다.");
    if (error) notices.push(`초기화 공지를 갱신하지 못했어요.${fetched !== null ? " 이전에 확인한 일정을 표시합니다." : " 내 계정의 갱신 일정은 계속 확인할 수 있어요."} ${error}`);
    else if (feed?.status === "pending" || !feed) notices.push("초기화 공지를 불러오고 있어요. 내 계정의 갱신 일정은 먼저 표시됩니다.");
    notice.textContent = notices.join(" "); notice.hidden = !notices.length; notice.classList.toggle("is-demo", !!feed?.demo && !error);
    const activePrediction = event => event.kind === "prediction" && (finite(event.endsAt) === null || event.endsAt > Date.now() / 1000);
    const noWatch = feed?.status === "ok" && !state.events.some(activePrediction) && !state.undated.some(activePrediction);
    el("calendar-feed-time").textContent = feed?.refreshing ? "공지 갱신 중" : fetched !== null ? `공지 확인 ${prettyTime(fetched, true)} (KST)${error ? " · 이전 조회" : noWatch ? " · 활성 예측 없음" : ""}` : "공지 조회 시각 확인 불가";
    el("calendar-feed-time").title = generated !== null ? `API 응답 생성: ${prettyTime(generated, true)} (KST). 실제 X 수집 시각과 다를 수 있습니다.` : "공지는 Codex Resets가 수집하고 분류한 정보입니다.";
    const undated = state.undated.map(event => eventDetail(event, true));
    if (!undated.length && state.expiredWatch) undated.push(node("div", { className: "calendar-undated-note" }, "최근 예측의 유효 시간이 끝났습니다. 새로운 예측이 제공되면 표시됩니다."));
    el("calendar-undated").replaceChildren(...undated); el("calendar-undated").hidden = !undated.length;
  }
  function render() { buildEvents(); renderFeedStatus(); renderCalendar(); }
  async function fetchFeed() {
    if (state.fetching || document.hidden) return;
    state.fetching = true;
    try {
      const response = await fetch("/api/reset-calendar", { credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(15000) });
      if (!response.ok) throw new Error(`조회 오류 (${response.status})`);
      const feed = await response.json(), signature = JSON.stringify({ ...feed, now: undefined });
      const recovered = !!state.connectionError;
      state.connectionError = null; state.feed = feed;
      if (signature !== state.signature || recovered) { state.signature = signature; render(); }
    } catch {
      state.connectionError = "로컬 대시보드의 연결 상태를 확인해 주세요."; renderFeedStatus();
    } finally { state.fetching = false; }
  }
  state.selected = dateKey(Date.now() / 1000); state.month = `${state.selected.slice(0, 7)}-01`;
  el("calendar-prev").addEventListener("click", () => chooseDay(shiftMonth(state.selected, -1)));
  el("calendar-next").addEventListener("click", () => chooseDay(shiftMonth(state.selected, 1)));
  el("calendar-today").addEventListener("click", () => chooseDay(dateKey(Date.now() / 1000)));
  window.addEventListener("codex-account-state", (event) => { state.accounts = event.detail; render(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { fetchFeed(); render(); } });
  render(); fetchFeed(); setInterval(fetchFeed, 5000);
  setInterval(() => { if (!document.hidden) render(); }, 60000);
})();
