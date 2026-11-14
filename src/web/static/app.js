// Dashboard: polls the local server and renders the selected run.
(function () {
  const $ = (id) => document.getElementById(id);
  const usd = (v, dp = 2) => (v == null || isNaN(v)) ? "–" : `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
  const signed = (v) => (v == null || isNaN(v)) ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${usd(Math.abs(v))}`;
  const compactUsd = (v) => Math.abs(v) >= 10000 ? `$${(v / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })}K` : usd(v, Math.abs(v) >= 100 ? 0 : 2);
  const ts = (iso) => iso ? Date.parse(iso) : NaN;
  const clock = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";
  const hhmm = (iso, plusSeconds = 0) => iso ? new Date(Date.parse(iso) + plusSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "–";
  const STATES = { running: "Running", waiting: "Waiting for session start", starting: "Starting", halted: "Halted - no new trades", finished: "Finished", error: "Stopped by an error" };

  let selected = new URLSearchParams(location.search).get("run") || "";
  let live = null;
  let timer = null;
  let last = { status: null, trades: [], equity: [], bars: [] };

  async function get(path) {
    const sep = path.includes("?") ? "&" : "?";
    const res = await fetch(selected ? `${path}${sep}run=${encodeURIComponent(selected)}` : path, { cache: "no-store" });
    if (!res.ok) throw new Error(`${path}: ${res.status}`);
    return res.json();
  }

  function setText(id, text, cls) {
    const node = $(id);
    node.textContent = text;
    if (cls !== undefined) node.className = node.className.replace(/\b(up|down)\b/g, "").trim() + (cls ? ` ${cls}` : "");
  }

  function meter(id, used, limit) {
    const node = $(id);
    const frac = limit > 0 ? Math.min(1, Math.max(0, used / limit)) : 0;
    node.firstElementChild.style.width = `${(frac * 100).toFixed(1)}%`;
    node.dataset.level = frac >= 1 ? "bad" : frac >= 0.7 ? "warn" : "ok";
  }

  async function loadRuns() {
    const data = await fetch("/api/runs", { cache: "no-store" }).then((r) => r.json());
    live = data.live;
    const select = $("run");
    select.textContent = "";
    for (const name of data.runs) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name === data.live ? `${name} (live)` : name;
      select.appendChild(opt);
    }
    if (!selected) selected = data.live || data.runs[0] || "";
    select.value = selected;
    select.hidden = data.runs.length === 0;
  }

  function renderStatus(s) {
    const isLive = s.live && !["finished", "error"].includes(s.state);
    $("kicker").textContent = `Paper trading · ${s.symbol || ""} · ${s.source || ""} · ${s.bar_seconds ? s.bar_seconds / 60 + "-minute bars" : ""}`;
    $("title").textContent = s.run_id ? `Run ${s.run_id}` : "Paper trading desk";
    $("state").dataset.state = s.state;
    $("state-text").textContent = STATES[s.state] || s.state;
    if (s.session) $("session").textContent = ` · session ${hhmm(s.session.start)} – ${hhmm(s.session.end)}`;
    $("kill").hidden = !isLive || (s.risk && s.risk.halted);
    $("run-dir").textContent = s.run_dir || "–";
    $("verify-cmd").textContent = `python run.py verify logs/${s.run_id || "<run>"}`;
    if (!s.account) return;

    const a = s.account, p = s.position, r = s.risk;
    setText("pnl", signed(a.day_pnl), a.day_pnl > 0.005 ? "up" : a.day_pnl < -0.005 ? "down" : "");
    $("pnl-note").textContent = `realized ${signed(a.realized)} · fees ${usd(a.fees)} · open ${signed(p.unrealized)}`;
    setText("equity", usd(a.equity));
    $("equity-note").textContent = `started with ${usd(a.starting_cash, 0)}`;
    const side = p.side === "flat" ? "Flat" : `${p.side === "long" ? "Long" : "Short"} ${Math.abs(p.qty).toFixed(5)}`;
    setText("position", side);
    $("position-note").textContent = p.avg_price ? `entry ${usd(p.avg_price)}` : "no open position";
    setText("price", usd(s.price));
    $("price-note").textContent = s.quote_time ? `as of ${clock(s.quote_time)}` : "";
    setText("trades", `${r.trades_today} / ${r.max_trades}`);
    meter("trades-meter", r.trades_today, r.max_trades);
    const lossUsed = Math.max(0, -a.day_pnl);
    setText("loss", r.max_daily_loss > 0 ? `${usd(lossUsed, 0)} / ${usd(r.max_daily_loss, 0)}` : "no limit");
    meter("loss-meter", lossUsed, r.max_daily_loss);

    const sig = s.last_signal;
    const cap = (w) => w.charAt(0).toUpperCase() + w.slice(1);
    $("signal").textContent = sig ? (sig.acted_on === sig.target ? cap(sig.target) : `${cap(sig.target)} → ${sig.acted_on}`) : "No signal yet";
    let waiting = "";
    if (s.strategy) waiting = s.strategy.bars < s.strategy.warmup ? `Warming up: ${s.strategy.bars} of ${s.strategy.warmup} bars` : "Waiting for the next bar to close";
    $("signal-reason").textContent = sig ? `Bar ending ${hhmm(sig.bar, s.bar_seconds)} · ${sig.reason}` : waiting;
    const kv = $("strategy");
    kv.textContent = "";
    if (s.strategy) {
      const rows = [["Strategy", s.strategy.name], ...Object.entries(s.strategy.params || {}).map(([k, v]) => [k, String(v)]),
                    ["Max position", usd(r.max_position_usd, 0)]];
      for (const [k, v] of rows) {
        const dt = document.createElement("dt"); dt.textContent = k;
        const dd = document.createElement("dd"); dd.textContent = v;
        kv.append(dt, dd);
      }
    }
    const alerts = $("alerts");
    alerts.textContent = "";
    const addAlert = (text, bad) => { const d = document.createElement("div"); d.className = bad ? "alert bad" : "alert"; d.textContent = text; alerts.appendChild(d); };
    if (r.halted) addAlert(`Halted: ${r.halt_reason}`, true);
    if (s.data && s.data.errors_in_row) addAlert(`Data feed: ${s.data.errors_in_row} failed request(s) in a row. ${s.data.last_error}`, s.data.errors_in_row >= 5);

    const blackout = $("blackout");
    blackout.textContent = "";
    const list = $("news");
    list.textContent = "";
    const news = s.news || {};
    if (!news.enabled) {
      const li = document.createElement("li"); li.className = "muted"; li.textContent = "News filter is off.";
      list.appendChild(li);
      return;
    }
    if (news.blackout) {
      const d = document.createElement("div"); d.className = "alert";
      d.textContent = `No new entries now: ${news.blackout.country} ${news.blackout.title} at ${clock(news.blackout.time)}`;
      blackout.appendChild(d);
    }
    if (news.error) {
      const d = document.createElement("div"); d.className = "alert"; d.textContent = `Calendar unavailable: ${news.error}`;
      blackout.appendChild(d);
    }
    if (!news.upcoming || !news.upcoming.length) {
      const li = document.createElement("li"); li.className = "muted"; li.textContent = "No matching events in the next 24 hours.";
      list.appendChild(li);
    }
    for (const e of news.upcoming || []) {
      const li = document.createElement("li");
      const when = document.createElement("span"); when.className = "when";
      when.textContent = new Date(e.time).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
      const what = document.createElement("span");
      what.textContent = `${e.country} · ${e.title}${e.forecast ? ` (forecast ${e.forecast})` : ""}`;
      li.append(when, what);
      list.appendChild(li);
    }
  }

  function renderTrades(trades) {
    const body = $("trade-rows");
    body.textContent = "";
    if (!trades.length) {
      const tr = document.createElement("tr"); const td = document.createElement("td");
      td.colSpan = 9; td.className = "muted"; td.textContent = "No trades yet.";
      tr.appendChild(td); body.appendChild(tr);
      return;
    }
    for (const t of trades.slice().reverse()) {
      const tr = document.createElement("tr");
      const realized = Number(t.realized_pnl);
      const cells = [
        [clock(t.time)], [t.side === "buy" ? "▲ Buy" : "▼ Sell"], [Number(t.qty).toFixed(6), "num"],
        [usd(t.market_price), "num"], [usd(t.price), "num"], [usd(t.fee), "num"],
        [realized ? signed(realized) : "–", `num ${realized > 0 ? "up" : realized < 0 ? "down" : ""}`],
        [usd(t.cash_after), "num"], [t.reason, "reason"],
      ];
      for (const [text, cls] of cells) {
        const td = document.createElement("td");
        td.textContent = text;
        if (cls) td.className = cls;
        tr.appendChild(td);
      }
      body.appendChild(tr);
    }
  }

  function renderLog(events) {
    const list = $("log");
    list.textContent = "";
    for (const e of events.slice().reverse()) {
      if (e.kind === "signal") continue; // every bar logs one; the panel above shows the latest
      const li = document.createElement("li");
      const t = document.createElement("span"); t.className = "t"; t.textContent = clock(e.time);
      const k = document.createElement("span"); k.className = "k"; k.textContent = e.kind;
      li.append(t, k, document.createTextNode(e.message || ""));
      list.appendChild(li);
    }
  }

  function renderCharts() {
    const { trades, equity, bars, status } = last;
    const barMs = ((status && status.bar_seconds) || 60) * 1000;
    $("price-title").textContent = `${(status && status.symbol) || "Price"} and trades`;
    // Show the session plus a short lead-in (at least the last hour), not all the warm-up history.
    let points = bars.map((b) => ({ t: Number(b.start) * 1000 + barMs, v: Number(b.close) }));
    if (points.length && status && status.session) {
      const cutoff = Math.min(ts(status.session.start) - 30 * barMs, points[points.length - 1].t - 60 * barMs);
      points = points.filter((p) => p.t >= cutoff);
    }
    lineChart($("price-chart"), {
      label: "Price with buy and sell markers",
      seriesName: "Close",
      points,
      markers: trades.map((t) => ({ t: ts(t.time), v: Number(t.price), kind: t.side,
        label: `${t.side === "buy" ? "▲ Buy" : "▼ Sell"} ${Number(t.qty).toFixed(5)} @ ${usd(t.price)}` })),
      markerWindowMs: barMs,
      format: compactUsd,
      emptyText: "Waiting for closed bars…",
    });
    lineChart($("equity-chart"), {
      label: "Day profit and loss over time",
      seriesName: "Day P&L",
      points: equity.map((e) => ({ t: ts(e.time), v: Number(e.day_pnl) })),
      format: (v) => `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: Math.abs(v) >= 100 ? 0 : 2 })}`,
      zero: true,
      area: true,
      emptyText: "Plotted after the first bar closes.",
    });
  }

  async function refresh() {
    try {
      const [status, trades, equity, bars, events] = await Promise.all([
        get("/api/status"), get("/api/trades"), get("/api/equity"), get("/api/bars?limit=720"), get("/api/events?limit=150"),
      ]);
      last = { status, trades, equity, bars };
      renderStatus(status);
      renderTrades(trades);
      renderLog(events);
      renderCharts();
    } catch (err) {
      $("state").dataset.state = "error";
      $("state-text").textContent = live || selected ? `Dashboard lost contact with the bot (${err.message})` : "No runs yet. Start one with: python run.py";
    }
    clearTimeout(timer);
    const isLive = last.status && last.status.live && !["finished", "error"].includes(last.status.state);
    timer = setTimeout(refresh, isLive ? 5000 : 30000);
  }

  $("run").addEventListener("change", (e) => {
    selected = e.target.value;
    history.replaceState(null, "", `?run=${encodeURIComponent(selected)}`);
    refresh();
  });
  $("kill").addEventListener("click", async () => {
    if (!confirm("Close the paper position and stop trading for the rest of this run?")) return;
    $("kill").disabled = true;
    await fetch("/api/kill", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    setTimeout(refresh, 1500);
  });
  let resizeTimer = null;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderCharts, 150); });

  loadRuns().catch(() => {}).finally(refresh);
})();
