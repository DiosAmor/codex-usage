"use strict";

const $ = (id) => document.getElementById(id);
const ui = { state: null, fetching: false, manualRefresh: false, timer: null, toastTimer: null, deleteId: null, returnFocus: null, lastRender: null };
const OFFICIAL_USAGE = "https://chatgpt.com/codex/settings/usage";
const ACTIVE_LOGINS = new Set(["pending", "starting", "waiting", "running", "awaiting_authorization", "awaiting_login", "in_progress"]);
const number = (value) => typeof value === "number" && Number.isFinite(value) ? value : null;
const percent = (value) => number(value) === null ? null : Math.max(0, Math.min(100, value));
const asString = (value, fallback = "") => typeof value === "string" ? value : fallback;
const formatNumber = (value) => new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 }).format(value);
const epochDate = (value) => number(value) !== null && value > 0 ? new Date(value * 1000) : null;
const absoluteTime = (value, full = false) => {
  const date = epochDate(value);
  return date ? new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", ...(full ? { year: "numeric" } : {}), month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(date) : "확인 불가";
};
const h = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (name === "className") node.className = value;
    else if (name === "text") node.textContent = value;
    else if (name.startsWith("on") && typeof value === "function") node.addEventListener(name.slice(2).toLowerCase(), value);
    else if (name in node && !name.startsWith("aria")) node[name] = value;
    else node.setAttribute(name, String(value));
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
};
const icon = (name) => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
  const paths = { trash: "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7", copy: "M9 8V3h12v14h-5M3 8h13v13H3z", reset: "M20 7v5h-5M20 11a8 8 0 1 0-2 6", link: "M14 4h6v6M20 4l-9 9M10 4H4v16h16v-6" };
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", paths[name] || paths.link); svg.append(path); return svg;
};
function officialUrl(value) {
  try { const url = new URL(value); return url.protocol === "https:" && ["auth.openai.com", "login.openai.com", "chatgpt.com", "openai.com"].includes(url.hostname) && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}
function relativeTime(value, now = Date.now() / 1000, future = false) {
  if (number(value) === null) return "시간 확인 불가";
  const difference = future ? value - now : now - value;
  if (difference <= 0) return future ? "초기화 시점 도달 · 갱신 대기" : "방금 업데이트";
  if (difference < 60) return future ? "1분 이내 초기화" : "방금 업데이트";
  if (difference < 3600) return `${Math.ceil(difference / 60)}분 ${future ? "후 초기화" : "전 업데이트"}`;
  if (difference < 86400) {
    const hours = Math.floor(difference / 3600), minutes = Math.floor(difference % 3600 / 60);
    return `${hours}시간${minutes ? ` ${minutes}분` : ""} ${future ? "후 초기화" : "전 업데이트"}`;
  }
  const days = Math.floor(difference / 86400), hours = Math.floor(difference % 86400 / 3600);
  return `${days}일${hours ? ` ${hours}시간` : ""} ${future ? "후 초기화" : "전 업데이트"}`;
}
async function request(path, method = "GET", body) {
  const response = await fetch(path, { method, credentials: "same-origin", cache: "no-store", headers: { ...(method !== "GET" ? { "X-Dashboard-Request": "1", "Content-Type": "application/json" } : {}) }, ...(body !== undefined ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(20000) });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(asString(payload.error, `요청을 처리하지 못했습니다 (${response.status}).`));
  return payload;
}
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(ui.toastTimer); ui.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4200); }
function windowName(window, index) {
  const mins = number(window?.windowDurationMins);
  if (mins === 300) return "5시간 사용 한도";
  if (mins === 10080) return "주간 사용 한도";
  if (mins === null) return index === 0 ? "사용 한도" : "추가 사용 한도";
  if (mins % 1440 === 0) return `${formatNumber(mins / 1440)}일 사용 한도`;
  if (mins % 60 === 0) return `${formatNumber(mins / 60)}시간 사용 한도`;
  return `${formatNumber(mins)}분 사용 한도`;
}
function remaining(window) { return percent(window?.remainingPercent); }
function renderWindow(window, index) {
  const value = remaining(window), used = percent(window.usedPercent), resetsAt = number(window.resetsAt);
  const bar = value === null ? h("div", { className: "usage-bar unknown", role: "img", "aria-label": "잔여량 확인 불가" }) : h("progress", { className: `usage-bar${value <= 10 ? " critical" : value <= 20 ? " low" : ""}`, max: 100, value, "aria-label": `잔여량 ${formatNumber(value)}%` });
  const detail = h("div", { className: "window-detail" }, h("span", {}, used === null ? "사용률 확인 불가" : `${formatNumber(used)}% 사용`), h("time", { title: resetsAt === null ? "초기화 시각 확인 불가" : `${absoluteTime(resetsAt, true)} (한국 시간)`, ...(resetsAt !== null ? { dateTime: epochDate(resetsAt).toISOString(), "data-reset-at": resetsAt } : {}) }, resetsAt === null ? "초기화 시각 확인 불가" : relativeTime(resetsAt, Date.now() / 1000, true)));
  return h("div", { className: "usage-window" }, h("div", { className: "window-heading" }, h("span", { className: "window-name" }, windowName(window, index)), h("div", { className: `window-value${value === null ? " unknown" : ""}` }, value === null ? "확인 불가" : `${formatNumber(value)}%`, value === null ? null : h("span", {}, "남음"))), bar, detail);
}
function renderReset(account) {
  const reset = account.resetCredits, count = number(reset?.availableCount), credits = Array.isArray(reset?.credits) ? reset.credits : [];
  const dates = credits.map(c => number(c.expiresAt)).filter(v => v !== null && v > Date.now() / 1000).sort((a, b) => a - b);
  let detail = count === 0 ? "현재 사용 가능한 초기화권이 없습니다." : count === null ? "이 계정에서 사용 가능 개수를 확인하지 못했어요." : "필요할 때 공식 사용량 페이지에서 사용할 수 있어요.";
  if (dates.length && count !== 0) {
    detail = `확인된 만료일 ${absoluteTime(dates[0])}`;
    if (count === null || dates.length !== credits.length || credits.length < count) detail += " · 일부 초기화권의 만료일은 확인 불가";
  }
  const nodes = [h("div", { className: "reset-panel" }, h("div", {}, h("div", { className: "reset-title" }, icon("reset"), "초기화권"), h("div", { className: "reset-detail" }, detail)), h("div", { className: `reset-number${count === null ? " unknown" : ""}` }, count === null ? "확인 불가" : formatNumber(count), count === null ? null : h("span", {}, "개")))];
  if (credits.length) nodes.push(h("details", { className: "reset-details" }, h("summary", {}, "초기화권 상세"), credits.map((credit, index) => h("div", { className: "reset-credit" }, h("div", {}, asString(credit.title, `초기화권 ${index + 1}`)), h("div", {}, `만료: ${number(credit.expiresAt) === null ? "확인 불가" : absoluteTime(credit.expiresAt, true)}${asString(credit.status) ? ` · ${asString(credit.status)}` : ""}`)))));
  return nodes;
}
function openDelete(account, trigger) {
  ui.deleteId = account.id; ui.returnFocus = trigger;
  $("delete-description").textContent = `‘${asString(account.label, "이 계정")}’을 대시보드에서 제거합니다. 다시 연결하면 사용량을 확인할 수 있습니다.`;
  $("delete-error").hidden = true; $("delete-dialog").showModal(); $("delete-cancel").focus();
}
function renderAccount(account, index) {
  const label = asString(account.label, `계정 ${index + 1}`), error = asString(account.error), status = asString(account.status), buckets = Array.isArray(account.buckets) ? account.buckets : [], lastSuccess = number(account.lastSuccessAt);
  const failed = !!error || ["error", "failed", "expired", "unauthorized", "reauth_required"].includes(status);
  const article = h("article", { className: "account-card", "aria-label": `${label} 사용량`, "data-account-id": account.id });
  const deleteButton = h("button", { className: "icon-button delete-account", type: "button", "aria-label": `${label} 연결 해제`, title: "계정 연결 해제", onclick: (event) => openDelete(account, event.currentTarget) }, icon("trash"));
  article.append(h("div", { className: "account-header" }, h("div", { className: `avatar tone-${index % 4}`, "aria-hidden": "true" }, label.trim().slice(0, 1).toUpperCase()), h("div", { className: "account-identity" }, h("h3", {}, label), h("div", { className: "account-email" }, asString(account.email, "계정 정보 확인 중"))), account.removable === false ? null : deleteButton));
  article.append(h("div", { className: "account-meta" }, h("span", { className: "plan-badge" }, asString(account.plan, "플랜 확인 불가")), account.source === "existing" ? h("span", { className: "plan-badge" }, "기존 로그인") : null, h("span", { className: `account-status${failed ? " error" : ""}` }, failed ? (lastSuccess !== null ? "이전 조회값 표시" : "조회 실패") : status === "refreshing" ? "갱신 중" : lastSuccess === null ? "조회 대기" : "연결됨")));
  if (failed) article.append(h("div", { className: "account-alert", role: "status" }, error || "계정 조회에 실패했습니다. 연결 상태를 확인해 주세요.", lastSuccess !== null ? " 마지막으로 확인한 값을 표시하고 있어요." : ""));
  if (!buckets.length) article.append(h("div", { className: "bucket" }, h("div", { className: "usage-absent" }, lastSuccess === null ? "아직 사용량을 확인하지 못했습니다." : "이 계정에서 사용 한도 정보가 제공되지 않았습니다.")));
  buckets.forEach((bucket, bucketIndex) => {
    const section = h("div", { className: "bucket" });
    if (buckets.length > 1 || (asString(bucket.name) && !["codex", "Codex", "기본"].includes(bucket.name))) section.append(h("div", { className: "bucket-heading" }, asString(bucket.name, `사용 한도 ${bucketIndex + 1}`), bucket.plan ? h("small", {}, asString(bucket.plan)) : null));
    const windows = [bucket.primary, bucket.secondary];
    if (!windows.some(Boolean)) section.append(h("div", { className: "usage-absent" }, "사용 한도 정보가 제공되지 않았습니다."));
    windows.forEach((window, windowIndex) => { if (window && typeof window === "object") section.append(renderWindow(window, windowIndex)); else if (windows.some(Boolean)) section.append(h("div", { className: "usage-absent" }, `${windowIndex === 0 ? "기본" : "추가"} 사용 한도: 정보 미제공`)); });
    if (bucket.credits && typeof bucket.credits === "object") {
      const credit = bucket.credits, balance = number(credit.balance);
      if (balance !== null || credit.unlimited === true) section.append(h("div", { className: "credits-detail" }, credit.unlimited === true ? "추가 크레딧: 무제한으로 제공됨" : `추가 크레딧 ${formatNumber(balance)}${credit.hasCredits === false ? " · 현재 사용 불가" : ""}`));
    }
    article.append(section);
  });
  article.append(...renderReset(account));
  article.append(h("div", { className: "account-footer" }, h("time", { title: lastSuccess === null ? "성공한 조회가 없습니다" : `${absoluteTime(lastSuccess, true)} (한국 시간)`, ...(lastSuccess !== null ? { dateTime: epochDate(lastSuccess).toISOString(), "data-updated-at": lastSuccess } : {}) }, lastSuccess === null ? "아직 업데이트되지 않음" : relativeTime(lastSuccess)), h("a", { href: OFFICIAL_USAGE, target: "_blank", rel: "noopener noreferrer", title: "브라우저에 현재 로그인된 계정의 사용량 페이지가 열립니다" }, "공식 페이지 ↗")));
  return article;
}
function renderLogins(logins) {
  const visible = logins.filter(login => !["cancelled", "complete", "completed", "success"].includes(asString(login.status)));
  $("login-area").replaceChildren(...visible.map(login => {
    const active = ACTIVE_LOGINS.has(asString(login.status)) || !login.status, code = asString(login.userCode), url = officialUrl(login.verificationUrl), error = asString(login.error), label = asString(login.label, "새 계정");
    const node = h("article", { className: `login-card${active ? "" : " failed"}` }, h("div", { className: "login-copy" }, h("h3", {}, active ? `${label} · 로그인 대기 중` : `${label} · 연결하지 못했어요`), h("p", {}, error || (code ? "아래 코드를 복사해 로그인 페이지에 입력하세요." : "로그인 정보를 준비하고 있어요. 잠시만 기다려 주세요."))));
    if (active && code) node.append(h("div", { className: "login-code" }, h("code", {}, code), h("button", { className: "copy-code", type: "button", title: "로그인 코드 복사", "aria-label": "로그인 코드 복사", onclick: async () => { try { await navigator.clipboard.writeText(code); toast("로그인 코드를 복사했습니다."); } catch { toast("코드를 직접 선택해 복사해 주세요."); } } }, icon("copy"))));
    if (active && !code) node.append(h("div", { className: "login-no-code" }, h("span", { className: "spinner" }), "로그인 준비 중"));
    const actions = h("div", { className: "login-actions" });
    if (active && url) actions.append(h("a", { className: "button primary small", href: url, target: "_blank", rel: "noopener noreferrer" }, "로그인 페이지 열기", icon("link")));
    actions.append(h("button", { className: "button text-button", type: "button", onclick: async (event) => { event.currentTarget.disabled = true; try { await request(`/api/logins/${encodeURIComponent(login.id)}/cancel`, "POST", {}); await fetchState(); } catch (e) { toast(e.message); event.currentTarget.disabled = false; } } }, active ? "취소" : "닫기"));
    node.append(actions);
    if (active && code) node.append(h("div", { className: "login-micro" }, "다른 계정을 추가할 때는 로그인 페이지에서 연결할 계정을 확인해 주세요. 로그인 완료 후 자동으로 표시됩니다."));
    if (active && login.verificationUrl && !url) node.append(h("div", { className: "login-micro" }, "로그인 주소를 검증하지 못했습니다. 계정 연결을 취소하고 다시 시도해 주세요."));
    return node;
  }));
  $("login-area").hidden = !visible.length;
}
function updateClock() {
  const now = Date.now() / 1000;
  document.querySelectorAll("[data-reset-at]").forEach(node => { node.textContent = relativeTime(Number(node.dataset.resetAt), now, true); });
  document.querySelectorAll("[data-updated-at]").forEach(node => { node.textContent = relativeTime(Number(node.dataset.updatedAt), now); });
  if (!ui.state) return;
  const accounts = ui.state.accounts || [], interval = number(ui.state.refreshInterval) ?? 300;
  const attempts = accounts.map(account => number(account.lastAttemptAt)).filter(value => value !== null);
  const next = number(ui.state.nextRefreshAt) ?? (attempts.length ? Math.max(...attempts) + interval : null);
  $("refresh-status").classList.toggle("busy", !!ui.state.refreshing || ui.manualRefresh);
  if (!$("connection-alert").hidden) { $("refresh-status-text").textContent = "연결 확인 필요"; return; }
  $("refresh-status-text").textContent = ui.state.refreshing || ui.manualRefresh ? "사용량 갱신 중" : !accounts.length ? `자동 갱신 · ${Math.max(1, Math.round(interval / 60))}분 간격` : next === null ? `자동 갱신 · ${Math.max(1, Math.round(interval / 60))}분 간격` : `자동 갱신 · ${Math.max(1, Math.ceil((next - now) / 60))}분 후`;
}
function renderState(state) {
  ui.state = state;
  const accounts = Array.isArray(state.accounts) ? state.accounts : [], logins = Array.isArray(state.logins) ? state.logins : [];
  $("account-total").textContent = String(accounts.length); $("account-count").textContent = String(accounts.length);
  const successful = accounts.filter(account => number(account.lastSuccessAt) !== null && !account.error).length;
  $("account-summary").textContent = accounts.length ? `${successful}개 계정 정상 조회${successful < accounts.length ? ` · ${accounts.length - successful}개 확인 필요` : ""}` : "계정을 추가해 사용량을 확인하세요";
  const knownCounts = accounts.map(account => number(account.resetCredits?.availableCount)).filter(value => value !== null);
  const unknownCount = accounts.length - knownCounts.length, resetTotal = knownCounts.reduce((sum, count) => sum + count, 0);
  $("reset-total").textContent = !accounts.length ? "—" : !knownCounts.length ? "확인 불가" : formatNumber(resetTotal);
  $("reset-total").parentElement.classList.toggle("summary-unknown", !!accounts.length && !knownCounts.length);
  $("reset-unit").hidden = !accounts.length || !knownCounts.length;
  $("reset-summary").textContent = !accounts.length ? "계정 연결 후 확인할 수 있어요" : unknownCount ? `${knownCounts.length ? `${resetTotal}개 확인됨 · ` : ""}${unknownCount}개 계정 확인 불가` : "모든 계정의 사용 가능 개수 합계";
  const low = accounts.filter(account => (account.buckets || []).some(bucket => [bucket.primary, bucket.secondary].some(window => remaining(window) !== null && remaining(window) <= 20))).length;
  const unknownUsage = accounts.filter(account => !(account.buckets || []).some(bucket => [bucket.primary, bucket.secondary].some(window => remaining(window) !== null))).length;
  $("low-total").textContent = String(low);
  $("low-summary").textContent = unknownUsage ? `잔여량 20% 이하 · ${unknownUsage}개 확인 불가` : "잔여량 20% 이하인 계정";
  const openedDetails = new Set([...$("accounts").querySelectorAll(".account-card")].filter(card => card.querySelector("details[open]")).map(card => card.dataset.accountId));
  const focusedAccount = document.activeElement?.closest(".account-card")?.dataset.accountId;
  const accountCards = accounts.map(renderAccount);
  accountCards.forEach(card => { if (openedDetails.has(card.dataset.accountId) && card.querySelector("details")) card.querySelector("details").open = true; });
  $("accounts").replaceChildren(...accountCards); $("accounts").setAttribute("aria-busy", "false"); $("accounts").hidden = !accounts.length; $("empty-state").hidden = !!accounts.length;
  if (focusedAccount && !document.querySelector("dialog[open]")) accountCards.find(card => card.dataset.accountId === focusedAccount)?.querySelector("button")?.focus({ preventScroll: true });
  if (asString(state.credentialSource)) $("source-note").textContent = state.credentialSource;
  $("demo-notice").hidden = !state.demo;
  renderLogins(logins);
  $("refresh-button").disabled = !!state.refreshing || ui.manualRefresh;
  $("refresh-button").querySelector("svg").classList.toggle("rotate", !!state.refreshing || ui.manualRefresh);
  updateClock();
}
async function fetchState() {
  if (ui.fetching || document.hidden) return;
  ui.fetching = true;
  try { const state = await request("/api/state"); $("connection-alert").hidden = true; $("refresh-status").classList.remove("offline"); const signature = JSON.stringify({ ...state, now: undefined }); if (signature !== ui.lastRender) { renderState(state); ui.lastRender = signature; } else { ui.state = state; updateClock(); } }
  catch (error) { $("connection-alert").textContent = `대시보드에 연결하지 못했습니다. 프로그램 실행 상태를 확인해 주세요.${ui.state ? " 마지막으로 불러온 정보를 표시하고 있어요." : ""} (${asString(error.message, "연결 오류")})`; $("connection-alert").hidden = false; $("refresh-status").classList.add("offline"); $("refresh-status-text").textContent = "대시보드 연결 끊김"; $("accounts").setAttribute("aria-busy", "false"); if (!ui.state) $("accounts").replaceChildren(h("div", { className: "initial-loading" }, "연결되면 계정 정보가 자동으로 표시됩니다.")); }
  finally { ui.fetching = false; }
}
function openAdd(event) { ui.returnFocus = event.currentTarget; $("account-label").value = ""; $("form-error").hidden = true; $("account-dialog").showModal(); $("account-label").focus(); }
$("add-button").addEventListener("click", openAdd); $("empty-add-button").addEventListener("click", openAdd);
$("dialog-close").addEventListener("click", () => $("account-dialog").close());
$("delete-close").addEventListener("click", () => $("delete-dialog").close()); $("delete-cancel").addEventListener("click", () => $("delete-dialog").close());
[$("account-dialog"), $("delete-dialog")].forEach(dialog => { dialog.addEventListener("close", () => { if (ui.returnFocus?.isConnected) ui.returnFocus.focus(); }); dialog.addEventListener("click", event => { const bounds = dialog.getBoundingClientRect(); if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close(); }); });
$("account-form").addEventListener("submit", async event => {
  event.preventDefault(); const label = $("account-label").value.trim(); if (!label) { $("account-label").focus(); return; }
  $("submit-account").disabled = true; $("submit-account").textContent = "로그인 준비 중…"; $("form-error").hidden = true;
  try { await request("/api/accounts", "POST", { label }); $("account-dialog").close(); toast("로그인을 시작했습니다. 아래 안내를 따라 계정을 연결해 주세요."); await fetchState(); }
  catch (error) { $("form-error").textContent = error.message; $("form-error").hidden = false; }
  finally { $("submit-account").disabled = false; $("submit-account").textContent = "로그인 시작 ↗"; }
});
$("delete-confirm").addEventListener("click", async () => {
  if (!ui.deleteId) return; $("delete-confirm").disabled = true; $("delete-cancel").disabled = true; $("delete-error").hidden = true;
  try { await request(`/api/accounts/${encodeURIComponent(ui.deleteId)}`, "DELETE"); $("delete-dialog").close(); toast("계정 연결을 해제했습니다."); await fetchState(); }
  catch (error) { $("delete-error").textContent = error.message; $("delete-error").hidden = false; }
  finally { $("delete-confirm").disabled = false; $("delete-cancel").disabled = false; }
});
$("refresh-button").addEventListener("click", async () => {
  ui.manualRefresh = true; $("refresh-button").disabled = true; $("refresh-button").querySelector("svg").classList.add("rotate"); updateClock();
  try { await request("/api/refresh", "POST", {}); await fetchState(); }
  catch (error) { toast(error.message); }
  finally { ui.manualRefresh = false; $("refresh-button").disabled = !!ui.state?.refreshing; $("refresh-button").querySelector("svg").classList.toggle("rotate", !!ui.state?.refreshing); updateClock(); }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) { fetchState(); updateClock(); } });
fetchState(); ui.timer = setInterval(fetchState, 5000); setInterval(() => { if (!document.hidden) updateClock(); }, 10000);
