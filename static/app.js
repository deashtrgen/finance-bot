// ═══════════════════════════════════════════════════════════════
// Finance Web App — frontend
// ═══════════════════════════════════════════════════════════════

const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}

const initData = tg?.initData || "";
const fmt = (n) => "₸" + Number(n || 0).toLocaleString("en-US").replace(/,/g, " ");

// Show logout button only in browser (not inside Telegram Mini App)
if (!initData) {
  document.getElementById("logout-btn")?.classList.remove("hidden");
}

// ── API wrapper ────────────────────────────────────────────────
async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    "X-Telegram-Init-Data": initData,
    ...(options.headers || {}),
  };
  const res = await fetch(path, {
    credentials: "include",   // include session cookie for browser auth
    ...options,
    headers,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  const ct = res.headers.get("Content-Type") || "";
  if (ct.includes("application/json")) return res.json();
  return res;
}

// ── Toast ──────────────────────────────────────────────────────
const toast = document.getElementById("toast");
let toastTimer;
function showToast(msg, ok = true) {
  toast.textContent = (ok ? "✅ " : "⚠️ ") + msg;
  toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("hidden"), 2500);
}

function showMsg(id, text, ok = true) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "err");
}

// ── Tabs ───────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const target = "panel-" + btn.dataset.tab;
    document.getElementById(target).classList.add("active");
    if (btn.dataset.tab === "home") loadSummary();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
});

// ── Initial load ───────────────────────────────────────────────
let EF_TARGET = 2988000;

(async function boot() {
  try {
    const me = await api("/api/me");
    EF_TARGET = me.ef_target;
    document.getElementById("userName").textContent = me.user?.first_name || "";
    document.getElementById("ef-target").textContent = fmt(EF_TARGET);
    await loadSummary();
  } catch (e) {
    // If this is a browser (no Telegram WebApp context), redirect to login page.
    // If inside Telegram, show the error directly — re-login via /login won't help there.
    if (!initData) {
      window.location.href = "/login";
      return;
    }
    document.getElementById("summary-loading").innerHTML =
      '<div style="color:#E74C3C">Auth failed. Try closing and reopening the app.</div>' +
      `<div class="muted" style="margin-top:10px">${e.message}</div>`;
  }
})();

// Logout handler (browser only)
async function logout() {
  try {
    await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
  } catch {}
  window.location.href = "/login";
}

// ═══════════════════════════════════════════════════════════════
// SUMMARY / HOME
// ═══════════════════════════════════════════════════════════════
async function loadSummary() {
  const loading = document.getElementById("summary-loading");
  const content = document.getElementById("summary-content");
  loading.classList.remove("hidden");
  content.classList.add("hidden");

  try {
    const s = await api("/api/summary");

    document.getElementById("s-month-label").textContent = s.month;
    document.getElementById("s-networth").textContent = fmt(s.net_worth);
    document.getElementById("s-income").textContent = fmt(s.income_month);
    document.getElementById("s-expense").textContent = fmt(s.expense_month);

    const netEl = document.getElementById("s-net");
    netEl.textContent = fmt(s.net_month);
    netEl.classList.toggle("positive", s.net_month >= 0);
    netEl.classList.toggle("negative", s.net_month < 0);

    document.getElementById("s-ef-total").textContent = fmt(s.ef_total);
    document.getElementById("s-ef-pct").textContent = s.ef_pct + "%";
    document.getElementById("s-ef-bar").style.width = s.ef_pct + "%";
    document.getElementById("s-ef-target-inline").textContent = fmt(s.ef_target);

    document.getElementById("s-accounts-total").textContent = fmt(s.accounts_total);
    const accUl = document.getElementById("s-accounts");
    accUl.innerHTML = Object.entries(s.accounts)
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `<li><span class="name">${esc(k)}</span><span class="num">${fmt(v)}</span></li>`)
      .join("") || '<li class="muted">No snapshots yet</li>';

    document.getElementById("s-inv-total").textContent = fmt(s.investments_total);
    const invUl = document.getElementById("s-inv");
    invUl.innerHTML = Object.entries(s.investments)
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `<li><span class="name">${esc(k)}</span><span class="num">${fmt(v)}</span></li>`)
      .join("") || '<li class="muted">No snapshots yet</li>';

    const expUl = document.getElementById("s-exp-cats");
    const expEntries = Object.entries(s.expenses_by_category).slice(0, 5);
    expUl.innerHTML = expEntries.length
      ? expEntries.map(([k, v]) => `<li><span class="name">${esc(k)}</span><span class="num">${fmt(v)}</span></li>`).join("")
      : '<li class="muted">No expenses yet</li>';

    // Subscriptions breakdown
    const subsWrap = document.getElementById("s-subs-wrap");
    if (s.subs_total > 0) {
      subsWrap.classList.remove("hidden");
      document.getElementById("s-subs-total").textContent = fmt(s.subs_total);
      document.getElementById("s-subs-groups").innerHTML =
        Object.entries(s.subs_by_group)
          .map(([k, v]) => `<li><span class="name">${esc(k)}</span><span class="num">${fmt(v)}</span></li>`)
          .join("");
      document.getElementById("s-subs-names").innerHTML =
        Object.entries(s.subs_by_name)
          .map(([k, v]) => `<li><span class="name">${esc(k)}</span><span class="num">${fmt(v)}</span></li>`)
          .join("");
    } else {
      subsWrap.classList.add("hidden");
    }

    loading.classList.add("hidden");
    content.classList.remove("hidden");
  } catch (e) {
    loading.innerHTML = `<div style="color: var(--danger)">Error loading summary: ${e.message}</div>`;
  }
}

function esc(s) {
  return String(s).replace(/[<>&"]/g, (c) => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;"}[c]));
}

// ═══════════════════════════════════════════════════════════════
// INCOME
// ═══════════════════════════════════════════════════════════════
async function submitIncome() {
  const source = document.getElementById("inc-source").value;
  const amount = parseInt(document.getElementById("inc-amount").value, 10);
  if (!amount || amount <= 0) return showMsg("inc-msg", "Enter a valid amount", false);
  try {
    await api("/api/income/add", { method: "POST", body: JSON.stringify({ source, amount }) });
    document.getElementById("inc-amount").value = "";
    showMsg("inc-msg", `Logged ${fmt(amount)} from ${source}`, true);
    showToast("Income saved");
  } catch (e) { showMsg("inc-msg", e.message, false); }
}

// ═══════════════════════════════════════════════════════════════
// EXPENSE
// ═══════════════════════════════════════════════════════════════
function toggleSubFields() {
  const isSub = document.getElementById("exp-cat").value === "Subscriptions";
  document.getElementById("sub-fields").classList.toggle("hidden", !isSub);
}

async function submitExpense() {
  const category = document.getElementById("exp-cat").value;
  const amount = parseInt(document.getElementById("exp-amount").value, 10);
  if (!amount || amount <= 0) return showMsg("exp-msg", "Enter a valid amount", false);

  const body = { category, amount };
  if (category === "Subscriptions") {
    const sub_group = document.getElementById("sub-group").value;
    const sub_name  = document.getElementById("sub-name").value.trim();
    if (!sub_name) return showMsg("exp-msg", "Please enter the subscription name", false);
    body.sub_group = sub_group;
    body.sub_name  = sub_name;
  }

  try {
    await api("/api/expense/add", { method: "POST", body: JSON.stringify(body) });
    document.getElementById("exp-amount").value = "";
    if (category === "Subscriptions") {
      document.getElementById("sub-name").value = "";
      showMsg("exp-msg", `Logged ${fmt(amount)} for ${body.sub_name} (${body.sub_group})`, true);
    } else {
      showMsg("exp-msg", `Logged ${fmt(amount)} under ${category}`, true);
    }
    showToast("Expense saved");
  } catch (e) { showMsg("exp-msg", e.message, false); }
}

// ═══════════════════════════════════════════════════════════════
// EMERGENCY FUND
// ═══════════════════════════════════════════════════════════════
async function submitEF() {
  const amount = parseInt(document.getElementById("ef-amount").value, 10);
  if (!amount || amount <= 0) return showMsg("ef-msg", "Enter a valid amount", false);
  try {
    const res = await api("/api/ef/add", { method: "POST", body: JSON.stringify({ amount }) });
    document.getElementById("ef-amount").value = "";
    const pct = Math.min(100, Math.round((res.running / res.target) * 1000) / 10);
    showMsg("ef-msg", `Saved ${fmt(amount)} · Running: ${fmt(res.running)} (${pct}%)`, true);
    showToast("Saved");
  } catch (e) { showMsg("ef-msg", e.message, false); }
}

// ═══════════════════════════════════════════════════════════════
// ACCOUNT
// ═══════════════════════════════════════════════════════════════
async function submitAccount() {
  const account = document.getElementById("acc-name").value.trim();
  const balance = parseInt(document.getElementById("acc-balance").value, 10);
  if (!account)           return showMsg("acc-msg", "Enter an account name", false);
  if (!balance || balance <= 0) return showMsg("acc-msg", "Enter a valid balance", false);

  try {
    const res = await api("/api/account/add", {
      method: "POST",
      body: JSON.stringify({ account, balance }),
    });
    document.getElementById("acc-balance").value = "";
    let msg = `Balance saved: ${fmt(balance)}`;
    if (res.prev > 0) {
      const diff = balance - res.prev;
      const sign = diff >= 0 ? "+" : "";
      const pct = Math.round((diff / res.prev) * 1000) / 10;
      msg += ` (${sign}${fmt(diff)}, ${sign}${pct}%)`;
    }
    showMsg("acc-msg", msg, true);
    showToast("Balance saved");
  } catch (e) { showMsg("acc-msg", e.message, false); }
}

// ═══════════════════════════════════════════════════════════════
// INVESTMENT
// ═══════════════════════════════════════════════════════════════
async function submitInvestment() {
  const wallet = document.getElementById("inv-wallet").value.trim();
  const asset  = document.getElementById("inv-asset").value.trim();
  const value  = parseInt(document.getElementById("inv-value").value, 10);
  if (!wallet)          return showMsg("inv-msg", "Enter a wallet", false);
  if (!asset)           return showMsg("inv-msg", "Enter an asset", false);
  if (!value || value <= 0) return showMsg("inv-msg", "Enter a valid value", false);

  try {
    const res = await api("/api/investment/add", {
      method: "POST",
      body: JSON.stringify({ wallet, asset, value }),
    });
    document.getElementById("inv-value").value = "";
    let msg = `Snapshot saved: ${fmt(value)}`;
    if (res.prev > 0) {
      const diff = value - res.prev;
      const sign = diff >= 0 ? "+" : "";
      const pct = Math.round((diff / res.prev) * 1000) / 10;
      msg += ` (${sign}${fmt(diff)}, ${sign}${pct}%)`;
    }
    showMsg("inv-msg", msg, true);
    showToast("Snapshot saved");
  } catch (e) { showMsg("inv-msg", e.message, false); }
}

// ═══════════════════════════════════════════════════════════════
// CHARTS
// ═══════════════════════════════════════════════════════════════
async function loadChart(kind) {
  const wrap = document.getElementById("chart-wrap");
  wrap.innerHTML = '<div class="muted center">Loading chart…</div>';
  try {
    const res = await fetch(`/api/charts/${kind}`, {
      credentials: "include",
      headers: { "X-Telegram-Init-Data": initData },
    });
    if (!res.ok) {
      if (res.status === 404) {
        wrap.innerHTML = '<div class="muted center">Not enough data yet for this chart.</div>';
        return;
      }
      throw new Error(`HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    wrap.innerHTML = `<img src="${url}" alt="chart" />`;
  } catch (e) {
    wrap.innerHTML = `<div style="color:#E74C3C" class="center">${e.message}</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════
// EDIT / DELETE
// ═══════════════════════════════════════════════════════════════
async function loadEntries() {
  const kind = document.getElementById("edit-kind").value;
  const list = document.getElementById("entries-list");
  if (!kind) { list.innerHTML = ""; return; }

  list.innerHTML = '<div class="muted center">Loading…</div>';
  try {
    const { entries } = await api(`/api/entries/${kind}`);
    if (!entries.length) {
      list.innerHTML = '<div class="muted center">No entries yet.</div>';
      return;
    }
    list.innerHTML = entries.map((e) => renderEntry(kind, e)).join("");
  } catch (err) {
    list.innerHTML = `<div style="color:#E74C3C" class="center">${err.message}</div>`;
  }
}

function renderEntry(kind, e) {
  const v = e.values;
  let label = "";
  if (kind === "ef")       label = `${v[0]||""} — ${fmt(v[1]||0)}`;
  else if (kind === "exp") label = `${v[0]||""} — ${esc(v[1]||"?")}: ${fmt(v[2]||0)}`;
  else if (kind === "inc") label = `${v[0]||""} — ${esc(v[1]||"?")}: ${fmt(v[2]||0)}`;
  else if (kind === "acc") label = `${v[0]||""} — ${esc(v[1]||"?")}: ${fmt(v[2]||0)}`;
  else if (kind === "inv") label = `${v[0]||""} — ${esc(v[2]||"?")} / ${esc(v[1]||"?")}: ${fmt(v[3]||0)}`;

  return `
    <div class="entry" data-row="${e.row}">
      <div class="info">${label}</div>
      <div class="actions">
        <button onclick="editEntry('${kind}', ${e.row})">✏️</button>
        <button class="del" onclick="deleteEntry('${kind}', ${e.row})">🗑️</button>
      </div>
    </div>
  `;
}

async function editEntry(kind, row) {
  const raw = prompt("Enter new amount (₸):");
  if (!raw) return;
  const new_amount = parseInt(raw.replace(/[^0-9]/g, ""), 10);
  if (!new_amount || new_amount <= 0) return showToast("Invalid amount", false);
  try {
    await api("/api/edit", {
      method: "POST",
      body: JSON.stringify({ kind, row, new_amount }),
    });
    showToast("Updated");
    loadEntries();
  } catch (e) { showToast(e.message, false); }
}

async function deleteEntry(kind, row) {
  if (!confirm("Delete this entry? This cannot be undone.")) return;
  try {
    await api("/api/delete", {
      method: "POST",
      body: JSON.stringify({ kind, row }),
    });
    showToast("Deleted");
    loadEntries();
  } catch (e) { showToast(e.message, false); }
}
