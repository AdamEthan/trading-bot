// Minimal SVG line chart with crosshair tooltip and optional trade markers.
// No libraries, so the dashboard works offline and loads nothing from the internet.
(function () {
  const NS = "http://www.w3.org/2000/svg";

  function el(name, attrs, parent) {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  }

  function niceTicks(min, max, count) {
    if (min === max) { const pad = Math.abs(min) * 0.001 || 1; min -= pad; max += pad; }
    const span = max - min;
    const raw = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
    return { lo, hi, ticks };
  }

  function timeLabel(ms, spanMs) {
    const d = new Date(ms);
    const opts = spanMs > 36 * 3600e3 ? { month: "short", day: "numeric", hour: "2-digit" } : { hour: "2-digit", minute: "2-digit" };
    return d.toLocaleString([], opts);
  }

  function nearest(points, t) {
    let lo = 0, hi = points.length - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (points[mid].t < t) lo = mid; else hi = mid;
    }
    return Math.abs(points[lo].t - t) <= Math.abs(points[hi].t - t) ? points[lo] : points[hi];
  }

  function lineChart(container, opts) {
    const points = opts.points || [];
    container.textContent = "";
    if (points.length < 2) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = opts.emptyText || "Waiting for data…";
      container.appendChild(empty);
      return;
    }
    const width = Math.max(280, container.clientWidth);
    const height = container.clientHeight || 220;
    const fmt = opts.format || ((v) => v.toFixed(2));
    const markers = (opts.markers || []).filter((m) => m.t >= points[0].t - 60e3 && m.t <= points[points.length - 1].t + 120e3);

    let min = Math.min(...points.map((p) => p.v), ...markers.map((m) => m.v));
    let max = Math.max(...points.map((p) => p.v), ...markers.map((m) => m.v));
    if (opts.zero) { min = Math.min(min, 0); max = Math.max(max, 0); }
    const { lo, hi, ticks } = niceTicks(min, max, 4);

    // Left margin fits the widest tick label.
    const probe = el("svg", { width: 0, height: 0 }, container);
    const probeText = el("text", { class: "tick" }, probe);
    let labelW = 0;
    for (const t of ticks) { probeText.textContent = fmt(t); labelW = Math.max(labelW, probeText.getComputedTextLength()); }
    container.removeChild(probe);
    const m = { l: Math.ceil(labelW) + 10, r: 12, t: 10, b: 24 };
    const w = width - m.l - m.r, h = height - m.t - m.b;
    const t0 = points[0].t, t1 = points[points.length - 1].t;
    const x = (t) => m.l + ((t - t0) / Math.max(1, t1 - t0)) * w;
    const y = (v) => m.t + (1 - (v - lo) / (hi - lo || 1)) * h;

    const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": opts.label || "chart" }, container);
    for (const t of ticks) {
      el("line", { class: t === lo ? "baseline" : "gridline", x1: m.l, x2: m.l + w, y1: y(t), y2: y(t) }, svg);
      const label = el("text", { class: "tick", x: m.l - 8, y: y(t) + 4, "text-anchor": "end" }, svg);
      label.textContent = fmt(t);
    }
    if (opts.zero && lo < 0 && hi > 0) el("line", { class: "zero", x1: m.l, x2: m.l + w, y1: y(0), y2: y(0) }, svg);
    const xTicks = Math.max(2, Math.min(6, Math.floor(w / 110)));
    for (let i = 0; i <= xTicks; i++) {
      const t = t0 + ((t1 - t0) * i) / xTicks;
      const label = el("text", { class: "tick", x: x(t), y: height - 6, "text-anchor": i === 0 ? "start" : i === xTicks ? "end" : "middle" }, svg);
      label.textContent = timeLabel(t, t1 - t0);
    }

    const path = points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
    if (opts.area) {
      const base = y(opts.zero ? 0 : lo);
      el("path", { class: "area", d: `${path}L${x(t1).toFixed(1)},${base}L${x(t0).toFixed(1)},${base}Z` }, svg);
    }
    el("path", { class: "line", d: path }, svg);

    for (const mk of markers) {
      const cx = x(Math.min(Math.max(mk.t, t0), t1)), cy = y(mk.v);
      const d = mk.kind === "buy"
        ? `M${cx},${cy - 2}l6,10h-12z`   // up-pointing triangle under the price
        : `M${cx},${cy + 2}l6,-10h-12z`; // down-pointing triangle above the price
      el("path", { class: mk.kind, d }, svg);
    }

    // Crosshair + tooltip
    const cross = el("line", { class: "cross", y1: m.t, y2: m.t + h, visibility: "hidden" }, svg);
    const dot = el("circle", { class: "hover-dot", r: 4, visibility: "hidden" }, svg);
    const tip = document.createElement("div");
    tip.className = "tooltip";
    tip.hidden = true;
    container.appendChild(tip);
    const hit = el("rect", { x: m.l, y: 0, width: w, height: height, fill: "transparent" }, svg);

    function show(evt) {
      const rect = svg.getBoundingClientRect();
      const px = ((evt.clientX - rect.left) / rect.width) * width;
      const t = t0 + ((px - m.l) / w) * (t1 - t0);
      const p = nearest(points, t);
      const cx = x(p.t), cy = y(p.v);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("visibility", "visible");
      tip.textContent = "";
      const time = document.createElement("div");
      time.className = "t";
      time.textContent = new Date(p.t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
      const row = document.createElement("div");
      row.className = "row";
      row.appendChild(document.createElement("i"));
      row.appendChild(document.createTextNode(`${opts.seriesName || "Value"} ${fmt(p.v)}`));
      tip.append(time, row);
      const near = markers.filter((mk) => Math.abs(mk.t - p.t) <= (opts.markerWindowMs || 60e3));
      for (const mk of near) {
        const r = document.createElement("div");
        r.className = "row";
        r.textContent = mk.label;
        tip.appendChild(r);
      }
      tip.hidden = false;
      const scale = rect.width / width;
      const left = cx * scale + 12;
      tip.style.left = `${Math.min(left, rect.width - tip.offsetWidth - 4)}px`;
      tip.style.top = `${Math.max(0, cy * scale - tip.offsetHeight - 10)}px`;
    }
    function hide() {
      cross.setAttribute("visibility", "hidden");
      dot.setAttribute("visibility", "hidden");
      tip.hidden = true;
    }
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerdown", show);
    hit.addEventListener("pointerleave", hide);
  }

  window.lineChart = lineChart;
})();
