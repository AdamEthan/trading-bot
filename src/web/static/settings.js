// Settings form. Reads and writes config/config.yaml through the local server.
(function () {
  const $ = (id) => document.getElementById(id);

  const SECTIONS = [
    ["market", "Market data", [
      { key: "symbol", type: "text", label: "Symbol", help: "Exchange product, e.g. BTC-USD." },
      { key: "source", type: "select", label: "Data source", options: [["coinbase", "Coinbase (free, no key)"], ["kraken", "Kraken (free, no key)"]] },
      { key: "bar_seconds", type: "select", label: "Bar size", options: [[60, "1 minute"], [300, "5 minutes"], [900, "15 minutes"], [3600, "1 hour"]], help: "Candle size your strategy sees." },
      { key: "poll_seconds", type: "number", label: "Poll every (seconds)", min: 2, help: "How often to fetch the price." },
    ]],
    ["session", "Session", [
      { key: "start", type: "text", label: "Start", help: "now, or HH:MM (24-hour)." },
      { key: "end", type: "text", label: "End", help: "HH:MM. Position is closed here." },
      { key: "timezone", type: "text", label: "Time zone", help: "Blank = this computer. Or e.g. America/New_York." },
      { key: "flatten_at_end", type: "bool", label: "Close position at end" },
    ]],
    ["account", "Paper account", [
      { key: "starting_cash", type: "number", label: "Starting cash (USD)", min: 1 },
      { key: "position_size_usd", type: "number", label: "Size per entry (USD)", min: 1 },
      { key: "fee_bps", type: "number", label: "Fee (basis points)", min: 0, step: "any", help: "10 = 0.10% per fill. Use your exchange's taker fee." },
      { key: "slippage_bps", type: "number", label: "Slippage (basis points)", min: 0, step: "any", help: "Fills are this much worse than the quote." },
    ]],
    ["exits", "Stops", [
      { key: "stop_loss_pct", type: "number", label: "Stop loss %", min: 0, step: "any", help: "0 = off." },
      { key: "take_profit_pct", type: "number", label: "Take profit %", min: 0, step: "any", help: "0 = off." },
    ]],
    ["risk", "Risk limits", [
      { key: "max_position_usd", type: "number", label: "Max position (USD)", min: 1 },
      { key: "max_daily_loss_usd", type: "number", label: "Max daily loss (USD)", min: 0, help: "0 = no limit." },
      { key: "max_trades_per_day", type: "number", label: "Max entries per day", min: 1, help: "Exits are always allowed." },
      { key: "max_data_age_seconds", type: "number", label: "Max price age (seconds)", min: 5 },
      { key: "allow_short", type: "bool", label: "Allow short selling" },
      { key: "kill_switch", type: "bool", label: "Stop for the day at the loss limit" },
    ]],
    ["news", "News filter (Forex Factory)", [
      { key: "enabled", type: "bool", label: "Pause entries around news" },
      { key: "currencies", type: "list", label: "Currencies", help: "Comma separated, e.g. USD, EUR." },
      { key: "impact", type: "list", label: "Impact levels", help: "High, Medium, Low." },
      { key: "minutes_before", type: "number", label: "Minutes before", min: 0 },
      { key: "minutes_after", type: "number", label: "Minutes after", min: 0 },
      { key: "flatten_before", type: "bool", label: "Also close open positions" },
    ]],
    ["dashboard", "Dashboard", [
      { key: "port", type: "number", label: "Port", min: 1024, help: "Served on 127.0.0.1 only." },
      { key: "open_browser", type: "bool", label: "Open browser on start" },
    ]],
  ];

  let state = { config: null, catalog: [] };
  let uid = 0;

  function field(spec, value, onChange) {
    const wrap = document.createElement("div");
    wrap.className = `field${spec.type === "bool" ? " check" : ""}${spec.wide ? " wide" : ""}`;
    const id = `f${++uid}`;
    const label = document.createElement("label");
    label.htmlFor = id;
    let input;
    if (spec.type === "select") {
      input = document.createElement("select");
      for (const [val, text] of spec.options) {
        const opt = document.createElement("option");
        opt.value = String(val);
        opt.textContent = text;
        input.appendChild(opt);
      }
      input.value = String(value);
    } else {
      input = document.createElement("input");
      if (spec.type === "bool") {
        input.type = "checkbox";
        input.checked = !!value;
      } else if (spec.type === "number") {
        input.type = "number";
        input.step = spec.step || "any";
        if (spec.min != null) input.min = spec.min;
        if (spec.max != null) input.max = spec.max;
        input.value = value;
      } else {
        input.type = "text";
        input.value = Array.isArray(value) ? value.join(", ") : (value ?? "");
        input.spellcheck = false;
        input.autocomplete = "off";
      }
    }
    input.id = id;
    input.dataset.kind = spec.type;
    if (spec.type === "bool") {
      label.append(input, document.createTextNode(spec.label));
      wrap.appendChild(label);
    } else {
      label.textContent = spec.label;
      wrap.append(label, input);
    }
    if (spec.help) {
      const help = document.createElement("span");
      help.className = "help";
      help.textContent = spec.help;
      wrap.appendChild(help);
    }
    input.addEventListener("change", () => onChange(read(input)));
    return wrap;
  }

  function read(input) {
    switch (input.dataset.kind) {
      case "bool": return input.checked;
      case "number": return input.value === "" ? null : Number(input.value);
      case "int": return input.value === "" ? null : Math.trunc(Number(input.value));
      case "list": return input.value.split(",").map((s) => s.trim()).filter(Boolean);
      default: return input.value;
    }
  }

  function renderPrivacy(privacy, path) {
    const node = $("privacy");
    node.dataset.status = privacy.status;
    node.textContent = "";
    const strong = document.createElement("strong");
    strong.textContent = ["ignored", "outside-repo", "no-git"].includes(privacy.status) ? "Private: " : "Warning: ";
    const code = document.createElement("code");
    code.textContent = path;
    node.append(strong, document.createTextNode("settings are saved to "), code,
      document.createTextNode(` (${privacy.message}). This page is only reachable from this computer.`));
  }

  function renderStrategy() {
    const cfg = state.config.strategy;
    const fields = $("strategy-fields");
    fields.textContent = "";
    const names = state.catalog.map((c) => [c.name, c.name === "custom" ? "custom (your private file)" : c.name]);
    fields.appendChild(field({ key: "name", type: "select", label: "Strategy", options: names }, cfg.name, (v) => {
      cfg.name = v;
      cfg.params = {};
      renderStrategy();
    }));
    if (cfg.name === "custom") {
      fields.appendChild(field({ key: "file", type: "text", label: "Strategy file", wide: true,
        help: "Path to your .py file. Relative paths start at the bot folder, e.g. src/strategy/private/my_strategy.py (git-ignored). Save to load its parameters." },
        cfg.file, (v) => { cfg.file = v; }));
    }
    const entry = state.catalog.find((c) => c.name === cfg.name) || { params: {}, description: "" };
    const desc = $("strategy-desc");
    desc.textContent = entry.error ? `Could not load file: ${entry.error}` : entry.description;
    desc.className = entry.error ? "small errors" : "small muted";
    const params = $("param-fields");
    params.textContent = "";
    for (const [key, p] of Object.entries(entry.params || {})) {
      const value = key in (cfg.params || {}) ? cfg.params[key] : p.default;
      const spec = { key, label: p.label || key, help: p.help, min: p.min, max: p.max };
      if (p.kind === "bool") spec.type = "bool";
      else if (p.kind === "choice") { spec.type = "select"; spec.options = p.choices.map((c) => [c, c]); }
      else { spec.type = "number"; spec.step = p.kind === "int" ? "1" : "any"; }
      params.appendChild(field(spec, value, (v) => {
        cfg.params = cfg.params || {};
        cfg.params[key] = p.kind === "int" && v != null ? Math.trunc(v) : v;
      }));
    }
  }

  function renderSections() {
    const host = $("sections");
    host.textContent = "";
    for (const [section, title, specs] of SECTIONS) {
      const set = document.createElement("fieldset");
      const legend = document.createElement("legend");
      legend.textContent = title;
      const fields = document.createElement("div");
      fields.className = "fields";
      for (const spec of specs) {
        fields.appendChild(field(spec, state.config[section][spec.key], (v) => {
          state.config[section][spec.key] = spec.key === "bar_seconds" ? Number(v) : v;
        }));
      }
      set.append(legend, fields);
      host.appendChild(set);
    }
  }

  function showErrors(errors) {
    const list = $("errors");
    list.textContent = "";
    for (const err of errors || []) {
      const li = document.createElement("li");
      li.textContent = err;
      list.appendChild(li);
    }
  }

  async function load() {
    const data = await fetch("/api/config", { cache: "no-store" }).then((r) => r.json());
    state = { config: data.config, catalog: data.catalog };
    renderPrivacy(data.privacy, data.path);
    renderStrategy();
    renderSections();
    showErrors(data.errors);
    if (data.live) $("result").textContent = "A run is in progress. Changes apply to the next run.";
  }

  $("form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("save").disabled = true;
    $("result").textContent = "Saving…";
    try {
      const res = await fetch("/api/config", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: state.config }),
      });
      const data = await res.json();
      if (data.ok) {
        state.catalog = data.catalog;
        renderPrivacy(data.privacy, data.path);
        renderStrategy();
        showErrors([]);
        $("result").textContent = "Saved. Start a run to use these settings: python run.py";
      } else {
        showErrors(data.errors);
        $("result").textContent = "Not saved - fix the problems below.";
      }
    } catch (err) {
      $("result").textContent = `Could not reach the bot: ${err.message}`;
    } finally {
      $("save").disabled = false;
    }
  });

  load().catch((err) => showErrors([`Could not load settings: ${err.message}`]));
})();
