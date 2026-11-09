// Форма чаевых: 4 экрана макета. Без сборки и сторонних библиотек.
// В хранилище браузера лежит только {payment_id, confirm_token} (sessionStorage): ни номер карты, ни JWT, ни ключи.
// Данные сервера выводятся только как текст (textContent), не как разметка.

const API = "/api/v2";
const SAVED = "tips.payment";
const $ = (id) => document.getElementById(id);
const screens = ["loading", "welcome", "tip", "confirm", "result"];

const state = { qr: null, view: null, percent: 5, custom: null, key: null, payment: null };

// ---------- деньги: целые копейки, без чисел с плавающей точкой ----------
function toKopecks(text) {
  const m = /^(\d{1,7})(?:[.,](\d{1,2}))?$/.exec(String(text).trim());
  return m ? Number(m[1]) * 100 + Number((m[2] || "").padEnd(2, "0")) : null;
}
function money(kop) {
  const rub = Math.floor(kop / 100), c = String(kop % 100).padStart(2, "0");
  return `${rub}.${c}`;
}
function show(name) {
  for (const s of screens) $(`s-${s}`).hidden = s !== name;
  window.scrollTo(0, 0);
}
function pretty(kop) { return `${money(kop).replace(".", ",")} ₽`; }

function tipKopecks() {
  const base = toKopecks(state.view.base_amount);
  if (state.custom !== null) return state.custom;
  return Math.floor((base * state.percent + 50) / 100); // ROUND_HALF_UP, как на сервере
}

// ---------- API ----------
async function call(method, path, { headers = {}, body } = {}) {
  const res = await fetch(API + path, {
    method, cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer",
    headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* пустое тело */ }
  return { status: res.status, data, retryAfter: Number(res.headers.get("Retry-After")) || 0 };
}
const SANDBOX = document.body.dataset.sandbox === "1";   // поля карты есть только в песочнице; живой провайдер даёт свои
const uuid = () => crypto.randomUUID();

// ---------- результат (экран 4) ----------
const OUTCOMES = {
  SUCCESS: { icon: "✓", cls: "ok", title: "Спасибо!", text: "Чаевые отправлены." },
  DECLINED: { icon: "!", cls: "bad", title: "Оплата не прошла", text: "Попробуйте другой способ.", retry: true },
  CANCELED: { icon: "–", cls: "bad", title: "Оплата отменена", text: "Деньги не списаны." },
  PENDING: { icon: "…", cls: "", title: "Обрабатываем", text: "Проверьте через минуту: эта страница обновится сама." },
  REFUND_PENDING: { icon: "↺", cls: "", title: "Возвращаем деньги", text: "Чек уже оплачен другим платежом, возврат в пути." },
  REFUNDED: { icon: "↺", cls: "", title: "Деньги возвращены", text: "Чек уже был оплачен другим платежом." },
};
function result(outcome, textOverride) {
  $("r-icon").textContent = outcome.icon;
  $("r-icon").className = `icon ${outcome.cls}`;
  $("r-title").textContent = outcome.title;
  $("r-text").textContent = textOverride || outcome.text;
  $("b-retry").hidden = !outcome.retry;
  show("result");
}
function failure(data) {
  // тексты ошибок приходят с сервера (каталог ADR-006); наружу не уходят подробности
  const expired = data && data.error_code === "QR_EXPIRED";
  result({ icon: "!", cls: "bad", title: expired ? "QR устарел" : "Не удалось открыть оплату",
           text: (data && data.message) || "Повторите позже.", retry: Boolean(data && data.retryable && state.view) });
}

function save(p) { try { sessionStorage.setItem(SAVED, JSON.stringify({ payment_id: p.payment_id, confirm_token: p.confirm_token })); } catch (_) {} }
function load() { try { return JSON.parse(sessionStorage.getItem(SAVED) || "null"); } catch (_) { return null; } }

async function poll(saved, attempts = 40) {
  for (let i = 0; i < attempts; i++) {
    let wait = 3000;
    try {
      const r = await call("GET", `/payments/${saved.payment_id}`, { headers: { "X-Payment-Token": saved.confirm_token } });
      if (r.status === 404) return failure(r.data);          // платёж не найден: дальше ждать нечего
      if (r.status === 200) {
        if (r.data.status !== "PENDING") return result(OUTCOMES[r.data.status] || OUTCOMES.PENDING);
      } else if (r.retryAfter) {
        wait = Math.min(r.retryAfter, 30) * 1000;            // 429 и 5xx не итог: платёж мог пройти, ждём и спрашиваем снова
      }
    } catch (_) { /* нет связи: повторим */ }
    result(OUTCOMES.PENDING);
    await new Promise((ok) => setTimeout(ok, wait));
  }
}

// ---------- экран 2 ----------
function renderTip() {
  const base = toKopecks(state.view.base_amount), tip = tipKopecks();
  $("t-base").textContent = pretty(base);
  $("t-tip").textContent = pretty(tip);
  $("t-total").textContent = pretty(base + tip);
  for (const b of $("tip-options").children) b.setAttribute("aria-pressed", String(state.custom === null && Number(b.dataset.p) === state.percent));
}
function validateCustom() {
  const raw = $("tip-custom").value.trim();
  const err = $("tip-error");
  err.hidden = true;
  if (raw === "") { state.custom = null; return true; }
  const kop = toKopecks(raw), base = toKopecks(state.view.base_amount);
  if (kop === null || kop * 100 > base * state.view.max_tip_percent) {
    err.textContent = `Чаевые — не больше ${state.view.max_tip_percent} % от чека`; err.hidden = false; return false;
  }
  state.custom = kop;
  return true;
}
function setupTip() {
  const box = $("tip-options");
  box.replaceChildren();
  for (const p of state.view.tip_options) {
    const b = document.createElement("button");
    b.type = "button"; b.dataset.p = String(p); b.textContent = `${p}%`;
    b.addEventListener("click", () => { state.percent = p; state.custom = null; $("tip-custom").value = ""; $("tip-error").hidden = true; renderTip(); });
    box.append(b);
  }
  $("tip-custom").addEventListener("input", () => { if (validateCustom()) renderTip(); });
  renderTip();
}

// ---------- экран 3 и оплата ----------
function renderConfirm() {
  const base = toKopecks(state.view.base_amount), tip = tipKopecks();
  $("c-base").textContent = pretty(base); $("c-tip").textContent = pretty(tip); $("c-total").textContent = pretty(base + tip);
  $("confirm-error").hidden = true;
}
// платёж создан, но до банка не дошёл: отменяем его, иначе он держит QR до автоотмены (30 минут), а ключ остаётся «занят»
async function abandon() {
  const p = state.payment;
  state.payment = null; state.key = null;
  if (!p) return;
  try { await call("POST", `/payments/${p.payment_id}/cancel`, { headers: { "X-Payment-Token": p.confirm_token } }); } catch (_) { /* сверка отменит позже */ }
}
function cardError(text) { $("confirm-error").textContent = text; $("confirm-error").hidden = false; }

async function pay() {
  const number = $("card-number").value.replace(/\s/g, ""), exp = $("card-exp").value.trim(), cvc = $("card-cvc").value.trim();
  if (!/^\d{12,19}$/.test(number) || !/^(0[1-9]|1[0-2])\/\d{2}$/.test(exp) || !/^\d{3,4}$/.test(cvc)) return cardError("Проверьте данные карты");
  if (!SANDBOX) return cardError("Оплата картой пока не подключена");   // номер карты на наш сервер не отправляем
  const button = $("b-pay");
  let redirecting = false;
  button.disabled = true;                       // двойное нажатие не создаёт второй платёж
  try {
    state.key = state.key || uuid();            // ключ идемпотентности один на попытку, повтор после сбоя использует тот же
    const tip = tipKopecks();
    const body = { qr_id: state.qr.q, sig: state.qr.s, base_amount: state.view.base_amount, receipt_id: state.view.receipt_id };
    if (state.custom !== null) body.tip_amount = money(tip); else body.tip_percent = state.percent;
    const created = await call("POST", "/payments", { headers: { "Idempotency-Key": state.key }, body });
    if (created.status !== 201 && created.status !== 200) return failure(created.data);
    state.payment = created.data; save(created.data);

    const tokenRes = await fetch("/sandbox/tokens", {   // в production вместо этого поля провайдера отдают токен сами
      method: "POST", cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ number, exp, cvc }),
    });
    $("card-number").value = ""; $("card-cvc").value = "";   // номер и CVC не остаются на странице
    if (!tokenRes.ok) { await abandon(); return cardError("Оплата картой сейчас недоступна"); }
    const { payment_method_token: token } = await tokenRes.json();

    const confirmed = await call("POST", `/payments/${state.payment.payment_id}/confirm`,
      { headers: { "X-Payment-Token": state.payment.confirm_token }, body: { payment_method_token: token } });
    const d = confirmed.data;
    if (confirmed.status === 202 && d && d.next_action && d.next_action.type === "redirect") {
      const url = new URL(d.next_action.url, location.href);
      if (url.protocol === "https:" || url.origin === location.origin) { redirecting = true; return void location.assign(url.href); }
      return failure(null);                     // сервер сам проверяет адрес; чужой узел не открываем и не ждём вхолостую
    }
    if (confirmed.status === 200 || confirmed.status === 202 || confirmed.status === 402) {
      return d.status === "PENDING" ? poll(load()) : result(OUTCOMES[d.status] || OUTCOMES.PENDING);
    }
    await abandon();                           // платёж не подтверждён: отменяем, следующая попытка с новым ключом
    failure(d);
  } catch (_) {
    cardError("Нет связи. Нажмите ещё раз: повторный запрос не спишет деньги дважды.");
  } finally {
    if (!redirecting) button.disabled = false;   // при переходе на 3-D Secure кнопка остаётся выключенной
  }
}

// ---------- запуск ----------
async function start() {
  if (location.pathname.endsWith("/result")) {          // возврат с 3-D Secure
    const saved = load();
    if (!saved) return failure(null);
    return poll(saved);
  }
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const q = params.get("q"), s = params.get("s");
  if (!q || !s) return failure(null);
  state.qr = { q, s };
  const r = await call("GET", `/qr/${encodeURIComponent(q)}?s=${encodeURIComponent(s)}`);
  if (r.status !== 200) return failure(r.data);
  state.view = r.data;
  $("brand").textContent = r.data.merchant_name;
  $("w-merchant").textContent = r.data.merchant_name;
  $("w-employee").textContent = r.data.employee_name;
  $("w-receipt").textContent = r.data.receipt_id;
  $("w-base").textContent = pretty(toKopecks(r.data.base_amount));
  if (!SANDBOX) { $("card-box").hidden = true; $("b-pay").disabled = true; cardError("Оплата картой пока не подключена"); }
  show("welcome");
}

// ввод карты: номер группами по 4, срок как ММ/ГГ
$("card-number").addEventListener("input", (e) => {
  e.target.value = e.target.value.replace(/\D/g, "").slice(0, 19).replace(/(\d{4})(?=\d)/g, "$1 ");
});
$("card-exp").addEventListener("input", (e) => {
  const d = e.target.value.replace(/\D/g, "").slice(0, 4);
  e.target.value = d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d;
});
$("card-cvc").addEventListener("input", (e) => { e.target.value = e.target.value.replace(/\D/g, "").slice(0, 4); });

$("b-continue").addEventListener("click", () => { setupTip(); show("tip"); });
$("b-to-confirm").addEventListener("click", () => { if (validateCustom()) { renderConfirm(); show("confirm"); } });
$("b-back").addEventListener("click", () => { abandon(); show("tip"); });
$("b-pay").addEventListener("click", pay);
$("b-retry").addEventListener("click", () => { state.key = null; state.payment = null; renderConfirm(); show("confirm"); });
start().catch(() => failure(null));
