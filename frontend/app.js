const API = "";
const HISTORY_POLL_MS = 15000;

function short(addr, n = 6) {
  if (!addr) return "—";
  return `${addr.slice(0, n)}…${addr.slice(-4)}`;
}

function showToast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 4000);
}

async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    const msg = Array.isArray(detail)
      ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
      : detail || data.message || res.statusText;
    throw new Error(msg);
  }
  return data;
}

function setBusy(formId, busy) {
  const form = document.getElementById(formId);
  if (!form) return;
  form.querySelectorAll("button, input").forEach((el) => {
    el.disabled = busy;
  });
}

function setStatusPill(ok, text) {
  const pill = document.getElementById("status-pill");
  pill.textContent = text;
  pill.className = `pill ${ok ? "pill-ok" : "pill-error"}`;
}

function renderStatus(status) {
  const grid = document.getElementById("status-grid");
  const rows = [
    ["Network", status.network],
    ["Chain ID", status.chain_id],
    ["Latest block", status.latest_block],
    ["Last scanned", status.last_scanned_block ?? "—"],
    ["Wallet", status.wallet ? short(status.wallet, 8) : "—"],
    ["Balance", status.balance_bnb ? `${status.balance_bnb} BNB` : "—"],
    ["Factory", short(status.factory, 8)],
    ["RPC", status.rpc_url ? short(status.rpc_url.replace("https://", ""), 12) : "—"],
    ["Dry run", String(status.dry_run)],
  ];
  grid.innerHTML = rows
    .map(([k, v]) => `<dt>${k}</dt><dd>${v ?? "—"}</dd>`)
    .join("");
  setStatusPill(true, `Block ${status.latest_block}`);
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    renderStatus(status);
    if (status.quote_tokens?.length >= 2) {
      document.getElementById("token-a").placeholder = status.quote_tokens[0];
      document.getElementById("token-b").placeholder = status.quote_tokens[1];
    }
    if (status.wallet === null) {
      showToast("No wallet in config — create pair disabled", true);
    }
    const safetyNote = status.safety_mode || "";
    if (safetyNote) {
      const el = document.getElementById("status-grid");
      if (el) {
        el.insertAdjacentHTML(
          "beforeend",
          `<dt>Safety</dt><dd>${safetyNote}</dd>`
        );
      }
    }
  } catch (e) {
    setStatusPill(false, "Offline");
    showToast(e.message, true);
  }
}

function renderHistory(data) {
  const body = document.getElementById("history-body");
  if (!data.items?.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No records yet</td></tr>';
    return;
  }

  function safetyCell(safety) {
    if (!safety) return '<span class="tag tag-muted">n/a</span>';
    if (safety.bypassed) {
      return '<span class="tag tag-warn" title="testnet bypass">bypass</span>';
    }
    if (safety.is_safe) {
      return '<span class="tag tag-ok">pass</span>';
    }
    const failed = (safety.checks_failed || []).join(", ") || "fail";
    return `<span class="tag tag-danger" title="${failed}">fail</span>`;
  }
  body.innerHTML = data.items
    .map((row) => {
      const time = row.recorded_at
        ? new Date(row.recorded_at).toLocaleString()
        : "—";
      const snipe = row.snipeable
        ? '<span class="tag tag-ok">yes</span>'
        : '<span class="tag tag-muted">no</span>';
      const target = row.target_token
        ? `<span class="mono">${short(row.target_token)}</span> ${snipe}`
        : `— ${snipe}`;
      const txLink = row.explorer_tx
        ? `<a href="${row.explorer_tx}" target="_blank" rel="noopener">${short(row.tx_hash)}</a>`
        : short(row.tx_hash);
      const pairLink = row.explorer_pair
        ? `<a href="${row.explorer_pair}" target="_blank" rel="noopener">${short(row.pair)}</a>`
        : short(row.pair);
      return `<tr>
        <td>${time}</td>
        <td>${row.source || "—"}</td>
        <td>${row.block_number ?? "—"}</td>
        <td>${pairLink}</td>
        <td class="mono">${short(row.token0)} / ${short(row.token1)}</td>
        <td>${target}</td>
        <td>${safetyCell(row.safety)}</td>
        <td>${txLink}</td>
      </tr>`;
    })
    .join("");
}

async function loadHistory() {
  const snipeableOnly = document.getElementById("filter-snipeable").checked;
  const q = snipeableOnly ? "?snipeable_only=true&limit=200" : "?limit=200";
  const data = await api(`/api/history${q}`);
  renderHistory(data);
}

document.getElementById("btn-refresh-status").addEventListener("click", loadStatus);
document.getElementById("btn-refresh-history").addEventListener("click", () =>
  loadHistory().catch((e) => showToast(e.message, true))
);
document.getElementById("filter-snipeable").addEventListener("change", loadHistory);

document.getElementById("btn-clear-history").addEventListener("click", async () => {
  if (!confirm("Clear all history records?")) return;
  try {
    const r = await api("/api/history", { method: "DELETE" });
    showToast(`Removed ${r.removed} record(s)`);
    await loadHistory();
  } catch (e) {
    showToast(e.message, true);
  }
});

document.getElementById("btn-fill-wbnb").addEventListener("click", async () => {
  try {
    const status = await api("/api/status");
    const quotes = status.quote_tokens || [];
    if (quotes.length >= 2) {
      document.getElementById("token-a").value = quotes[0];
      document.getElementById("token-b").value = quotes[1];
    } else if (quotes.length === 1) {
      document.getElementById("token-a").value = quotes[0];
    }
  } catch (e) {
    showToast(e.message, true);
  }
});

async function submitCreatePair(payload) {
  const out = document.getElementById("create-result");
  out.classList.remove("hidden");
  out.textContent = payload.deploy_new_token
    ? "Deploying test token and creating pair (~2 txs)…"
    : "Sending createPair transaction…";
  setBusy("form-create-pair", true);
  try {
    const result = await api("/api/create-pair", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    out.textContent = JSON.stringify(result, null, 2);
    showToast(
      payload.deploy_new_token
        ? `Token deployed & pair created · ${short(result.tx_hash)}`
        : `Pair created · ${short(result.tx_hash)}`
    );
    await loadHistory();
    await loadStatus();
    return result;
  } catch (err) {
    out.textContent = err.message;
    showToast(err.message, true);
    throw err;
  } finally {
    setBusy("form-create-pair", false);
  }
}

document.getElementById("form-create-pair").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tokenA = document.getElementById("token-a").value.trim();
  const tokenB = document.getElementById("token-b").value.trim();
  if (!tokenA || !tokenB) {
    showToast("Enter both token addresses or use Deploy new token + pair", true);
    return;
  }
  try {
    await submitCreatePair({ token_a: tokenA, token_b: tokenB });
  } catch (_) {}
});

document.getElementById("btn-deploy-and-pair").addEventListener("click", async () => {
  const quote =
    document.getElementById("token-a").value.trim() ||
    document.getElementById("token-b").value.trim() ||
    null;
  try {
    await submitCreatePair({
      deploy_new_token: true,
      quote_token: quote || undefined,
    });
  } catch (_) {}
});

document.getElementById("form-scan").addEventListener("submit", async (e) => {
  e.preventDefault();
  const lookback = parseInt(document.getElementById("lookback").value, 10);
  const out = document.getElementById("scan-result");
  out.classList.remove("hidden");
  out.textContent = "Scanning…";
  setBusy("form-scan", true);
  try {
    const result = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ lookback_blocks: lookback }),
    });
    out.textContent = JSON.stringify(result, null, 2);
    showToast(`Scan done · ${result.found} event(s)`);
    await loadHistory();
  } catch (err) {
    out.textContent = err.message;
    showToast(err.message, true);
  } finally {
    setBusy("form-scan", false);
  }
});

document.getElementById("btn-live-scan").addEventListener("click", async () => {
  const out = document.getElementById("scan-result");
  out.classList.remove("hidden");
  out.textContent = "Running live cycle…";
  setBusy("form-scan", true);
  try {
    const result = await api("/api/scan/live", { method: "POST" });
    out.textContent = JSON.stringify(result, null, 2);
    showToast(`Live scan · ${result.found} new event(s)`);
    await loadHistory();
  } catch (err) {
    out.textContent = err.message;
    showToast(err.message, true);
  } finally {
    setBusy("form-scan", false);
  }
});

loadStatus();
loadHistory().catch(() => {});
setInterval(() => loadHistory().catch(() => {}), HISTORY_POLL_MS);
