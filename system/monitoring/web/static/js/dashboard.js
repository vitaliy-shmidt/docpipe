/* DocPipe Monitoring dashboard (Phase M4).
 *
 * Security: data is only ever written with textContent / Chart.js canvas text - never as
 * HTML. No inline code (CSP script-src 'self'), no eval.
 * Polling: one request at a time (AbortController), server-controlled interval, paused while
 * the tab is hidden or the user pressed "Pause", exponential backoff on errors, stops on
 * 401/403. On errors the last data stays visible and is marked stale - never replaced by
 * zeros. Polling requests never extend the session; only explicit user actions (filter
 * changes) call the CSRF-protected activity route, at most every 30 s.
 */
(function () {
  "use strict";

  var app = document.getElementById("dpm-app");
  if (!app || typeof window.Chart === "undefined") { return; }

  var T = {};
  try { T = JSON.parse(app.getAttribute("data-texts") || "{}"); } catch (e) { T = {}; }
  var page = document.body.getAttribute("data-page");
  var lang = document.documentElement.lang || "en";
  var csrf = app.getAttribute("data-csrf");
  var pollMs = Math.max(2, Number(app.getAttribute("data-poll")) || 15) * 1000;
  var MAX_BACKOFF_MS = 5 * 60 * 1000;
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var NA = "—";

  function t(key) { return Object.prototype.hasOwnProperty.call(T, key) ? T[key] : key; }

  // --- formatting ---------------------------------------------------------------------------
  var nf = new Intl.NumberFormat(lang);
  var nf1 = new Intl.NumberFormat(lang, { maximumFractionDigits: 1 });
  var pf = new Intl.NumberFormat(lang, { style: "percent", maximumFractionDigits: 1 });

  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function fmtCount(v) { return isNum(v) ? nf.format(v) : NA; }
  function fmtPct(v) { return isNum(v) ? pf.format(v) : NA; }
  function fmtPercentValue(v) { return isNum(v) ? nf1.format(v) + " %" : NA; }
  function fmtMs(v) {
    if (!isNum(v)) { return NA; }
    if (v < 1) { return nf1.format(v) + " ms"; }
    if (v < 1000) { return nf.format(Math.round(v)) + " ms"; }
    if (v < 60000) { return nf1.format(v / 1000) + " s"; }
    return nf1.format(v / 60000) + " min";
  }
  function fmtBytes(v) {
    if (!isNum(v)) { return NA; }
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    var i = 0;
    while (Math.abs(v) >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return nf1.format(v) + " " + units[i];
  }
  function fmtTime(ms) {
    return isNum(ms) ? new Date(ms).toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : NA;
  }
  function fmtDateTime(ms) {
    return isNum(ms) ? new Date(ms).toLocaleString(lang, { dateStyle: "short", timeStyle: "medium" }) : NA;
  }
  function fmtAxis(ms, bucket) {
    var d = new Date(ms);
    if (bucket >= 86400000) { return d.toLocaleDateString(lang, { day: "2-digit", month: "2-digit" }); }
    if (bucket >= 3600000) {
      return d.toLocaleString(lang, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit", second: bucket < 60000 ? "2-digit" : undefined });
  }
  function fmtUptime(ms) {
    if (!isNum(ms)) { return NA; }
    var s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
      m = Math.floor((s % 3600) / 60);
    return (d ? d + " d " : "") + (d || h ? h + " h " : "") + m + " min";
  }

  // --- DOM helpers (textContent only) --------------------------------------------------------
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }
  function clear(node) { while (node && node.firstChild) { node.removeChild(node.firstChild); } return node; }
  function byId(id) { return document.getElementById(id); }
  function badge(text, cls, title) {
    var b = el("span", "badge rounded-pill " + cls, text);
    if (title) { b.title = title; }
    return b;
  }
  function approxBadge(method) {
    return method === "histogram" || method === true ? badge(t("approximate"), "text-bg-light border dpm-approx", t("approximate_hint")) : null;
  }
  var STATE_CLASS = {
    online: "success", degraded: "warning", offline: "danger", stale: "secondary",
    unavailable: "secondary", not_configured: "light"
  };

  // --- notices and freshness -----------------------------------------------------------------
  var notice = byId("dpm-notice");
  var freshness = byId("dpm-freshness");
  var lastOk = null, lastError = null;

  function showNotice(key) {
    if (!notice) { return; }
    if (!key) { notice.classList.add("d-none"); notice.textContent = ""; return; }
    notice.textContent = t(key);
    notice.classList.remove("d-none");
  }
  function updateFreshness() {
    if (!freshness) { return; }
    var stale = lastError || (lastOk && Date.now() - lastOk > 2 * pollMs + 2000);
    freshness.textContent = lastOk ? t("last_updated") + ": " + fmtTime(lastOk) + (stale ? " · " + t("stale") : "") :
      t("loading");
    freshness.classList.toggle("text-warning-emphasis", !!stale);
    app.classList.toggle("dpm-is-stale", !!stale);
  }
  window.setInterval(updateFreshness, 5000);

  // --- filters and user activity -------------------------------------------------------------
  var form = byId("dpm-filters");
  var lastActivity = 0;

  function toLocalInput(ms) {
    var d = new Date(ms - new Date(ms).getTimezoneOffset() * 60000);
    return d.toISOString().slice(0, 16);
  }
  function params() {
    var p = {};
    if (!form) { return p; }
    var range = form.elements.range ? form.elements.range.value : "";
    if (range) { p.range = range; }
    if (range === "custom") {
      var from = form.elements.from.value, to = form.elements.to.value;
      if (from && to) {
        p.from = String(new Date(from).getTime());  // local input -> UTC epoch ms
        p.to = String(new Date(to).getTime());
      } else {
        p.range = "24h";
      }
    }
    ["client", "service", "status", "category"].forEach(function (name) {
      if (form.elements[name] && form.elements[name].value) { p[name] = form.elements[name].value; }
    });
    return p;
  }
  function sendActivity() {
    // Explicit user action only - never from timers. The server limits its effect.
    if (Date.now() - lastActivity < 30000) { return; }
    lastActivity = Date.now();
    var body = new URLSearchParams({ csrf: csrf });
    fetch("/monitor/session/activity", {
      method: "POST", credentials: "same-origin", cache: "no-store", body: body,
      headers: { "Content-Type": "application/x-www-form-urlencoded" }
    }).catch(function () { /* best effort */ });
  }
  if (form) {
    var toggleCustom = function () {
      var custom = form.elements.range.value === "custom";
      form.querySelectorAll(".dpm-custom").forEach(function (n) { n.classList.toggle("d-none", !custom); });
      if (custom && !form.elements.from.value) {
        form.elements.from.value = toLocalInput(Date.now() - 86400000);
        form.elements.to.value = toLocalInput(Date.now());
      }
    };
    form.addEventListener("change", function (event) {
      toggleCustom();
      if (event.target.name === "from" || event.target.name === "to") { return; }
      if (form.elements.range.value === "custom") { return; }
      sendActivity();
      resetPaging();
      refreshNow();
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      sendActivity();
      resetPaging();
      refreshNow();
    });
    toggleCustom();
  }

  // --- polling --------------------------------------------------------------------------------
  var live = true;
  try { live = window.sessionStorage.getItem("dpm-live") !== "0"; } catch (e) { live = true; }
  var timer = null, controller = null, failures = 0, stopped = false, inFlight = false;
  var liveButton = byId("dpm-live-toggle");

  function renderLiveButton() {
    if (!liveButton) { return; }
    liveButton.setAttribute("aria-pressed", live ? "true" : "false");
    liveButton.classList.toggle("dpm-paused", !live);
    liveButton.querySelector(".dpm-live-label").textContent = live ? t("live") : t("paused");
    liveButton.title = live ? t("pause") : t("resume");
  }
  function setLive(value) {
    live = value;
    try { window.sessionStorage.setItem("dpm-live", live ? "1" : "0"); } catch (e) { /* ignore */ }
    renderLiveButton();
    if (live) { refreshNow(); } else { cancel(); }
  }
  if (liveButton) { liveButton.addEventListener("click", function () { setLive(!live); }); }

  function cancel() {
    if (timer) { window.clearTimeout(timer); timer = null; }
    if (controller) { controller.abort(); controller = null; }
    inFlight = false;
  }
  function schedule() {
    if (timer) { window.clearTimeout(timer); timer = null; }
    if (!live || stopped || document.hidden) { return; }
    var delay = failures ? Math.min(pollMs * Math.pow(2, failures), MAX_BACKOFF_MS) : pollMs;
    timer = window.setTimeout(tick, delay);
  }
  function refreshNow() { cancel(); tick(); }
  function tick() {
    if (stopped || inFlight) { return; }
    load(null).then(schedule, schedule);
  }

  function apiUrl(extra) {
    var p = params();
    if (extra) { Object.keys(extra).forEach(function (k) { p[k] = extra[k]; }); }
    return "/monitor/api/v1/" + page + "?" + new URLSearchParams(p).toString();
  }

  function load(extra) {
    if (controller) { controller.abort(); }
    controller = new AbortController();
    inFlight = true;
    var signal = controller.signal;
    return fetch(apiUrl(extra), {
      credentials: "same-origin", cache: "no-store", signal: signal,
      headers: { "Accept": "application/json", "X-Monitor-Poll": "1" }
    }).then(function (response) {
      if (response.status === 401) {
        stopped = true; cancel(); showNotice("session_ended");
        window.setTimeout(function () { window.location.assign("/monitor/login"); }, 1500);
        throw new Error("401");
      }
      if (response.status === 403) { stopped = true; cancel(); showNotice("denied_text"); throw new Error("403"); }
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (response.status === 429) { failures += 1; showNotice("rate_limited"); throw new Error("429"); }
        if (response.status === 400) { showNotice("invalid_filter"); lastError = null; throw new Error("400"); }
        if (response.status === 503) { failures += 1; lastError = Date.now(); showNotice("telemetry_unavailable"); throw new Error("503"); }
        if (!response.ok || !body.ok) { failures += 1; lastError = Date.now(); showNotice("connection_lost"); throw new Error("http"); }
        failures = 0; lastError = null; lastOk = Date.now();
        showNotice(null);
        try { RENDER[page](body, extra); } catch (err) { if (window.console) { window.console.error(err); } }
        return body;
      });
    }, function (err) {
      if (err && err.name === "AbortError") { throw err; }
      failures += 1; lastError = Date.now(); showNotice("connection_lost");
      throw err;
    }).then(function (body) { inFlight = false; updateFreshness(); return body; },
      function (err) { inFlight = false; updateFreshness(); throw err; });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { cancel(); updateFreshness(); } else if (live && !stopped) { refreshNow(); }
  });
  window.addEventListener("pagehide", cancel);

  // --- charts -----------------------------------------------------------------------------------
  var charts = {};
  var lastData = null;

  function css(name, fallback) {
    var v = window.getComputedStyle(document.documentElement).getPropertyValue(name);
    return v && v.trim() ? v.trim() : fallback;
  }
  function palette() {
    var out = [];
    for (var i = 1; i <= 8; i += 1) { out.push(css("--dpm-c" + i, "#1f77b4")); }
    return out;
  }
  function applyChartDefaults() {
    Chart.defaults.color = css("--bs-secondary-color", "#6c757d");
    Chart.defaults.borderColor = css("--bs-border-color-translucent", "rgba(0,0,0,.1)");
    Chart.defaults.font.family = window.getComputedStyle(document.body).fontFamily;
    Chart.defaults.animation = reduceMotion ? false : { duration: 250 };
    Chart.defaults.maintainAspectRatio = false;
    Chart.defaults.plugins.legend.labels.boxWidth = 12;
  }
  applyChartDefaults();
  document.addEventListener("dpm:theme", function () {
    applyChartDefaults();
    Object.keys(charts).forEach(function (id) { charts[id].destroy(); delete charts[id]; });
    if (lastData) { try { RENDER[page](lastData); } catch (e) { /* ignore */ } }
  });

  function chart(id, type, labels, datasets, options) {
    var canvas = byId(id);
    if (!canvas) { return null; }
    var existing = charts[id];
    if (existing && existing.config.type === type && existing.data.datasets.length === datasets.length &&
        existing.data.datasets.every(function (ds, i) { return ds.label === datasets[i].label; })) {
      existing.data.labels = labels;
      datasets.forEach(function (ds, i) { existing.data.datasets[i].data = ds.data; });
      existing.update("none");  // in-place update, no re-render of the page
      return existing;
    }
    if (existing) { existing.destroy(); }
    charts[id] = new Chart(canvas, { type: type, data: { labels: labels, datasets: datasets }, options: options || {} });
    return charts[id];
  }
  function timeLabels(ts, bucket) { return (ts || []).map(function (v) { return fmtAxis(v, bucket); }); }
  function lineDs(label, data, color, extra) {
    var ds = { label: label, data: data, borderColor: color, backgroundColor: color, borderWidth: 2,
      pointRadius: 0, pointHitRadius: 6, tension: 0.25, spanGaps: false };
    if (extra) { Object.keys(extra).forEach(function (k) { ds[k] = extra[k]; }); }
    return ds;
  }
  function barDs(label, data, color) {
    return { label: label, data: data, backgroundColor: color, borderWidth: 0, borderRadius: 2, maxBarThickness: 28 };
  }
  function axisOpts(formatter, stacked) {
    return {
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { stacked: !!stacked, ticks: { maxRotation: 0, autoSkipPadding: 18 }, grid: { display: false } },
        y: { stacked: !!stacked, beginAtZero: true, ticks: { precision: formatter === fmtCount ? 0 : undefined,
          callback: function (v) { return formatter(v); } } }
      },
      plugins: { tooltip: { callbacks: { label: function (ctx) {
        return ctx.dataset.label + ": " + (ctx.raw === null ? t("coverage_gap") : formatter(ctx.raw));
      } } } }
    };
  }

  function horizontalOpts(formatter) {
    // indexAxis "y": categories on y, values (formatted) on x.
    return {
      indexAxis: "y",
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { beginAtZero: true, ticks: { callback: function (v) { return formatter(v); } } },
        y: { grid: { display: false } }
      },
      plugins: { tooltip: { callbacks: { label: function (ctx) {
        return ctx.dataset.label + ": " + (ctx.raw === null ? t("not_available") : formatter(ctx.raw));
      } } } }
    };
  }

  // --- shared widgets ----------------------------------------------------------------------------
  function metaRange(range) {
    if (!range) { return; }
    var text = t("dq_source") + ": " + t("dq_source_" + range.source) + " · " + t("bucket") + ": " +
      fmtMs(range.bucket_ms);
    document.querySelectorAll("[data-meta=range]").forEach(function (n) { n.textContent = text; });
  }

  function kpi(container, label, value, opts) {
    opts = opts || {};
    var col = el("div", opts.col || "col-6 col-md-4 col-xl-3 col-xxl");
    var card = el("div", "card h-100 dpm-kpi" + (opts.tone ? " dpm-kpi-" + opts.tone : ""));
    var body = el("div", "card-body py-3");
    var head = el("div", "d-flex align-items-center gap-1 mb-1");
    head.appendChild(el("span", "dpm-kpi-label", label));
    if (opts.badge) { head.appendChild(opts.badge); }
    body.appendChild(head);
    var val = el("div", "dpm-kpi-value", value);
    if (value === NA) { val.title = t("not_available"); val.classList.add("dpm-na"); }
    body.appendChild(val);
    if (opts.sub) { body.appendChild(el("div", "dpm-kpi-sub", opts.sub)); }
    card.appendChild(body);
    col.appendChild(card);
    container.appendChild(col);
  }

  function healthCards(container, services) {
    clear(container);
    (services || []).forEach(function (s) {
      var col = el("div", "col-6 col-xl-3");
      var card = el("div", "card h-100 dpm-health dpm-state-" + s.state);
      var body = el("div", "card-body py-3");
      var head = el("div", "d-flex align-items-center gap-2 mb-1");
      head.appendChild(el("span", "dpm-dot", ""));
      head.appendChild(el("span", "fw-semibold", t("svc_" + s.service)));
      body.appendChild(head);
      body.appendChild(badge(t("state_" + s.state), "text-bg-" + (STATE_CLASS[s.state] || "secondary")));
      var meta = el("div", "dpm-kpi-sub mt-2");
      meta.appendChild(el("div", "", t("last_check") + ": " + fmtTime(s.last_check_ms)));
      meta.appendChild(el("div", "", t("last_ok") + ": " + fmtTime(s.last_ok_ms)));
      if (s.version) { meta.appendChild(el("div", "", t("version") + ": " + s.version)); }
      meta.appendChild(el("div", "", t("source") + ": " + t("src_" + s.source)));
      body.appendChild(meta);
      card.appendChild(body);
      col.appendChild(card);
      container.appendChild(col);
    });
  }

  function qualityPanel(container, c, range) {
    if (!container) { return; }
    clear(container);
    if (!c) { container.appendChild(el("p", "mb-0 text-body-secondary", t("not_available"))); return; }
    var head = el("div", "d-flex align-items-center gap-2 mb-2");
    head.appendChild(el("span", "fw-semibold", t("data_quality")));
    head.appendChild(badge(c.complete ? t("complete") : t("incomplete"), c.complete ? "text-bg-success" : "text-bg-warning"));
    if (range && range.approximate) { head.appendChild(approxBadge(true)); }
    container.appendChild(head);
    var dl = el("dl", "row mb-0 small dpm-dl");
    function row(label, value) { dl.appendChild(el("dt", "col-7 fw-normal text-body-secondary", label)); dl.appendChild(el("dd", "col-5 text-end mb-1", value)); }
    if (range) { row(t("dq_source"), t("dq_source_" + range.source) + " · " + fmtMs(range.bucket_ms)); }
    row(t("dq_recorded"), fmtCount(c.requests_persisted));
    row(t("dq_dropped"), fmtCount(c.requests_dropped));
    row(t("dq_gaps"), fmtCount(c.collector_gap_minutes));
    row(t("dq_events_dropped"), fmtCount(c.events_dropped));
    row(t("dq_flush_failures"), fmtCount(c.flush_failures));
    container.appendChild(dl);
    if (c.range_before_first_data) { container.appendChild(el("p", "small text-body-secondary mt-2 mb-0", t("dq_before_data"))); }
  }

  function metricTile(container, label, sample, formatter, col) {
    var c = el("div", col || "col-6 col-md-3");
    var box = el("div", "dpm-tile h-100");
    box.appendChild(el("div", "dpm-kpi-label", label));
    var ok = sample && (sample.status === "ok" || sample.status === "stale") && isNum(sample.value);
    var value = el("div", "dpm-tile-value", ok ? formatter(sample.value) : NA);
    if (!ok) { value.classList.add("dpm-na"); }
    box.appendChild(value);
    var sub = el("div", "dpm-kpi-sub");
    if (sample && sample.source) { sub.appendChild(badge(t("src_" + sample.source), "text-bg-light border")); }
    if (sample && sample.status === "stale") { sub.appendChild(badge(t("stale"), "text-bg-secondary ms-1")); }
    if (!ok) {
      var reason = sample && sample.reason;
      sub.appendChild(el("span", "ms-1", reason === "not_configured" ? t("gpu_not_configured") :
        reason === "no_sample" ? t("no_sample") : t("not_available") + (reason ? " (" + reason + ")" : "")));
    }
    box.appendChild(sub);
    c.appendChild(box);
    container.appendChild(c);
  }

  function statusBadge(code) {
    var cls = code >= 500 || code === 0 ? "text-bg-danger" : code >= 400 ? "text-bg-warning" : "text-bg-success";
    return badge(code === 0 ? "–" : String(code), cls);
  }

  function emptyToggle(id, empty) { var n = byId(id); if (n) { n.classList.toggle("d-none", !empty); } }

  // --- pages ----------------------------------------------------------------------------------
  var paging = { cursor: null, extraPages: false };
  function resetPaging() { paging = { cursor: null, extraPages: false }; }

  var RENDER = {
    overview: function (d) {
      lastData = d;
      var colors = palette();
      healthCards(byId("ov-health"), d.services);
      var k = clear(byId("ov-kpis"));
      var tot = d.totals || {}, pct = d.percentiles || {};
      var approx = approxBadge(pct.method);
      kpi(k, t("requests"), fmtCount(tot.count), { badge: d.completeness && !d.completeness.complete ?
        badge(t("incomplete"), "text-bg-warning", t("data_quality")) : null });
      kpi(k, t("active_requests"), fmtCount(d.active_requests));
      kpi(k, t("success_rate"), fmtPct(tot.success_rate), { tone: "ok", sub: fmtCount(tot.success) + " " + t("successful") });
      kpi(k, t("failure_rate"), fmtPct(tot.failure_rate), { tone: tot.failed ? "bad" : null, sub: fmtCount(tot.failed) + " " + t("failed") });
      kpi(k, t("avg"), fmtMs(tot.avg_ms));
      kpi(k, t("median"), fmtMs(pct.p50), { badge: approx });
      kpi(k, t("p95"), fmtMs(pct.p95), { badge: approxBadge(pct.method) });
      kpi(k, t("p99"), fmtMs(pct.p99), { badge: approxBadge(pct.method) });
      kpi(k, t("uptime"), fmtUptime(d.uptime_ms));
      emptyToggle("ov-empty", !tot.count);
      metaRange(d.range);
      var labels = timeLabels((d.series || []).map(function (p) { return p.t; }), d.range.bucket_ms);
      chart("ov-requests", "bar", labels, [
        barDs(t("successful"), d.series.map(function (p) { return p.success; }), colors[0]),
        barDs(t("failed"), d.series.map(function (p) { return p.failed; }), colors[3])
      ], axisOpts(fmtCount, true));
      chart("ov-latency", "line", labels, [
        lineDs(t("avg"), d.series.map(function (p) { return p.avg_ms; }), colors[1]),
        lineDs(t("p95") + (d.range.approximate ? " ≈" : ""), d.series.map(function (p) { return p.p95_ms; }), colors[4], { borderDash: [5, 4] })
      ], axisOpts(fmtMs));
      var cs = d.clients_series || { labels: [], points: {}, t: [] };
      chart("ov-clients", "bar", labels, cs.labels.map(function (name, i) {
        return barDs(name === "unauthenticated" ? t("unauthenticated") : name === "other" ? t("other") : name,
          cs.points[name], colors[i % colors.length]);
      }), axisOpts(fmtCount, true));
      var sys = clear(byId("ov-system")), s = d.system || {};
      metricTile(sys, t("cpu"), s["host.cpu.percent"], fmtPercentValue);
      metricTile(sys, t("memory"), s["host.memory.used_percent"], fmtPercentValue);
      metricTile(sys, t("container_memory"), s["container.memory.current_bytes"], fmtBytes);
      var free = s["disk.data.free_bytes"], total = s["disk.data.total_bytes"];
      var used = free && total && isNum(free.value) && isNum(total.value) && total.value > 0 ?
        { value: 100 * (1 - free.value / total.value), source: free.source, status: free.status } : free;
      metricTile(sys, t("disk"), used, fmtPercentValue);
      metricTile(sys, t("gpu"), s["gpu.utilization_percent"], fmtPercentValue, "col-12 col-md-6");
      metricTile(sys, t("vram"), s["gpu.memory_used_bytes"], fmtBytes, "col-12 col-md-6");
      var ss = d.system_series || { points: {} };
      var cpu = ss.points["host.cpu.percent"] || [], mem = ss.points["host.memory.used_percent"] || [];
      chart("ov-load", "line", timeLabels(cpu.map(function (p) { return p.t; }), d.range.bucket_ms), [
        lineDs(t("cpu") + " (" + t("src_host") + ")", cpu.map(function (p) { return p.avg; }), colors[0]),
        lineDs(t("memory") + " (" + t("src_host") + ")", mem.map(function (p) { return p.avg; }), colors[2])
      ], axisOpts(fmtPercentValue));
      var b = clear(byId("ov-build")), build = d.build || {};
      function brow(label, value) { b.appendChild(el("dt", "col-5 fw-normal text-body-secondary", label)); b.appendChild(el("dd", "col-7 mb-1", value)); }
      brow(t("version"), build.version || NA);
      brow(t("commit"), build.commit || t("not_available"));
      brow(t("image_tag"), build.image_tag || t("not_available"));
      brow(t("build_state"), t("build_" + (build.source_state || "unknown")) +
        (build.local_patches && build.local_patches.length ? " (" + build.local_patches.join(", ") + ")" : ""));
      qualityPanel(byId("ov-quality"), d.completeness, d.range);
    },

    activity: function (d, extra) {
      var active = clear(byId("act-active"));
      byId("act-active-count").textContent = fmtCount((d.active || []).length);
      if (!(d.active || []).length) {
        var r0 = el("tr"); var c0 = el("td", "text-body-secondary", t("no_active")); c0.colSpan = 5; r0.appendChild(c0); active.appendChild(r0);
      }
      (d.active || []).forEach(function (a) {
        var r = el("tr", a.stale ? "dpm-row-stale" : "");
        r.appendChild(el("td", "", fmtTime(a.started_ms)));
        var age = el("td", "", fmtMs(a.age_ms));
        if (a.stale) { age.appendChild(badge(t("stale"), "text-bg-secondary ms-1")); }
        r.appendChild(age);
        r.appendChild(el("td", "font-monospace small", a.route));
        r.appendChild(el("td", "", a.service));
        r.appendChild(el("td", "font-monospace small text-truncate dpm-id", a.request_id));
        active.appendChild(r);
      });
      var tbody = byId("act-finished");
      if (!extra || !extra.cursor) { clear(tbody); }
      (d.items || []).forEach(function (i) {
        var r = el("tr");
        r.appendChild(el("td", "text-nowrap", fmtTime(i.ts_ms)));
        r.appendChild(el("td", i.client_id ? "" : "text-body-secondary", i.client_id || t("unauthenticated")));
        r.appendChild(el("td", "font-monospace small", i.method + " " + i.route));
        var st = el("td"); st.appendChild(statusBadge(i.status_code));
        if (i.error_code) { st.appendChild(el("span", "small text-body-secondary ms-1", i.error_code)); }
        r.appendChild(st);
        r.appendChild(el("td", "text-end text-nowrap", fmtMs(i.duration_ms)));
        var steps = el("td", "small");
        (i.steps || []).forEach(function (s) {
          var label = t("step_" + s.step) + " " + fmtMs(s.duration_ms);
          steps.appendChild(badge(label, s.outcome === "ok" ? "text-bg-light border me-1" : "text-bg-danger me-1",
            s.inclusive_of_parent ? t("inclusive") : ""));
        });
        r.appendChild(steps);
        r.appendChild(el("td", "small", i.ai_model || ""));
        var id = el("td", "font-monospace small text-truncate dpm-id", i.request_id);
        id.title = i.request_id;
        r.appendChild(id);
        tbody.appendChild(r);
      });
      emptyToggle("act-empty", !tbody.children.length);
      var until = byId("act-until");
      until.textContent = t("data_until") + ": " + fmtTime(d.data_until_ms);
      paging.cursor = d.next_cursor;
      byId("act-more").classList.toggle("d-none", !d.next_cursor);
    },

    clients: function (d) {
      lastData = d;
      var colors = palette();
      metaRange(d.range);
      emptyToggle("cl-empty", !d.total);
      var clients = d.clients || [];
      var names = clients.map(function (c) { return c.client_id || t("unauthenticated"); });
      chart("cl-share", "doughnut", names, [{ data: clients.map(function (c) { return c.count; }),
        backgroundColor: clients.map(function (c, i) { return colors[i % colors.length]; }), borderWidth: 1 }],
        { plugins: { tooltip: { callbacks: { label: function (ctx) {
          return ctx.label + ": " + fmtCount(ctx.raw) + " (" + fmtPct(clients[ctx.dataIndex].share) + ")"; } } } } });
      var cs = d.series || { labels: [], points: {}, t: [] };
      var labels = timeLabels(cs.t, d.range.bucket_ms);
      chart("cl-series", "bar", labels, cs.labels.map(function (name, i) {
        return barDs(name === "unauthenticated" ? t("unauthenticated") : name === "other" ? t("other") : name,
          cs.points[name], colors[i % colors.length]);
      }), axisOpts(fmtCount, true));
      chart("cl-compare", "bar", names, [
        barDs(t("requests"), clients.map(function (c) { return c.count; }), colors[0]),
        barDs(t("previous_period"), clients.map(function (c) { return c.previous_available ? (c.previous_count || 0) : null; }), colors[5])
      ], axisOpts(fmtCount));
      var tb = clear(byId("cl-table"));
      clients.forEach(function (c) {
        var r = el("tr");
        r.appendChild(el("td", c.authenticated ? "fw-semibold" : "text-body-secondary", c.client_id || t("unauthenticated")));
        [fmtCount(c.count), fmtPct(c.share), fmtPct(c.success_rate), fmtMs(c.avg_ms)].forEach(function (v) { r.appendChild(el("td", "text-end", v)); });
        var p95 = el("td", "text-end", (c.p95_method === "histogram" && isNum(c.p95_ms) ? "≈ " : "") + fmtMs(c.p95_ms));
        r.appendChild(p95);
        r.appendChild(el("td", "text-end", fmtCount(c.documents)));
        var pages = el("td", "text-end", c.pages_known ? fmtCount(c.pages) : NA);
        if (c.pages_known) { pages.title = t("pages_unknown") + " " + fmtCount(c.pages_known) + " " + t("requests"); }
        r.appendChild(pages);
        [fmtBytes(c.bytes_in), fmtBytes(c.bytes_out), fmtCount(c.ai_calls), fmtCount(c.tokens_in), fmtCount(c.tokens_out)].forEach(function (v) {
          r.appendChild(el("td", "text-end", v)); });
        var svc = el("td", "small");
        Object.keys(c.services || {}).sort().forEach(function (s) { svc.appendChild(badge(s + " " + fmtCount(c.services[s]), "text-bg-light border me-1")); });
        r.appendChild(svc);
        tb.appendChild(r);
      });
      qualityPanel(byId("cl-quality"), d.completeness, d.range);
    },

    performance: function (d) {
      lastData = d;
      var colors = palette();
      metaRange(d.range);
      var req = d.requests || {}, pct = req.percentiles || {};
      var k = clear(byId("pf-request"));
      var opt = { col: "col-6 col-md-3 col-xxl" };
      kpi(k, t("request_duration") + " · " + t("count"), fmtCount(req.count), opt);
      kpi(k, t("avg"), fmtMs(req.avg_ms), opt);
      kpi(k, t("median"), fmtMs(pct.p50), { col: opt.col, badge: approxBadge(pct.method) });
      kpi(k, t("p95"), fmtMs(pct.p95), { col: opt.col, badge: approxBadge(pct.method) });
      kpi(k, t("p99"), fmtMs(pct.p99), { col: opt.col, badge: approxBadge(pct.method) });
      kpi(k, t("min") + " / " + t("max"), fmtMs(req.min_ms) + " / " + fmtMs(req.max_ms), opt);
      kpi(k, t("failure_rate_tech"), fmtPct(req.failure_rate), opt);
      var steps = d.steps || [];
      emptyToggle("pf-empty", !req.count && !steps.length);
      var names = steps.map(function (s) { return t("step_" + s.step); });
      chart("pf-steps", "bar", names, [
        barDs(t("avg"), steps.map(function (s) { return s.avg_ms; }), colors[1]),
        barDs(t("p95") + (d.range.approximate ? " ≈" : ""), steps.map(function (s) { return s.p95_ms; }), colors[4])
      ], horizontalOpts(fmtMs));
      var ser = d.step_series || { t: [], avg_ms: {} };
      var stepKeys = Object.keys(ser.avg_ms).sort();
      chart("pf-series", "line", timeLabels(ser.t, d.range.bucket_ms), stepKeys.map(function (s, i) {
        return lineDs(t("step_" + s), ser.avg_ms[s], colors[i % colors.length], { spanGaps: false });
      }), axisOpts(fmtMs));
      var tb = clear(byId("pf-table"));
      steps.forEach(function (s) {
        var r = el("tr");
        var name = el("td", "");
        name.appendChild(el("div", "fw-semibold", t("step_" + s.step)));
        var kind = el("div", "small text-body-secondary", t(s.duration_kind));
        if (isNum(s.exclusive_avg_ms)) { kind.textContent += " · " + t("exclusive") + " Ø " + fmtMs(s.exclusive_avg_ms); }
        if (s.parent) { kind.textContent += " · ⊂ " + t("step_" + s.parent); }
        name.appendChild(kind);
        r.appendChild(name);
        var prefix = s.method === "histogram" ? "≈ " : "";
        r.appendChild(el("td", "text-end", fmtCount(s.count)));
        r.appendChild(el("td", "text-end", fmtMs(s.avg_ms)));
        [s.p50_ms, s.p95_ms, s.p99_ms].forEach(function (v) { r.appendChild(el("td", "text-end", isNum(v) ? prefix + fmtMs(v) : NA)); });
        r.appendChild(el("td", "text-end", fmtMs(s.min_ms)));
        r.appendChild(el("td", "text-end", fmtMs(s.max_ms)));
        r.appendChild(el("td", "text-end", fmtPct(s.failure_rate)));
        var ol = el("td", "small");
        if (s.ollama) {
          ol.appendChild(el("div", "", t("load_time") + ": " + fmtMs(s.ollama.load_avg_ms)));
          ol.appendChild(el("div", "", t("prompt_eval") + ": " + fmtMs(s.ollama.prompt_eval_avg_ms)));
          ol.appendChild(el("div", "", t("generation") + ": " + fmtMs(s.ollama.eval_avg_ms)));
          ol.appendChild(el("div", "", t("tokens_per_s") + ": " + (isNum(s.ollama.eval_tokens_per_s) ? nf1.format(s.ollama.eval_tokens_per_s) : NA)));
        } else { ol.textContent = NA; }
        r.appendChild(ol);
        tb.appendChild(r);
      });
      qualityPanel(byId("pf-quality"), d.completeness, d.range);
    },

    server: function (d) {
      lastData = d;
      var colors = palette();
      metaRange(d.range);
      healthCards(byId("sv-health"), d.services);
      var l = d.latest || {}, box = clear(byId("sv-latest"));
      var col = "col-6 col-md-4 col-xl-3";
      metricTile(box, t("cpu") + " (" + t("src_host") + ")", l["host.cpu.percent"], fmtPercentValue, col);
      metricTile(box, t("load1"), l["host.load1"], function (v) { return nf1.format(v); }, col);
      metricTile(box, t("memory") + " (" + t("src_host") + ")", l["host.memory.used_percent"], fmtPercentValue, col);
      metricTile(box, t("container_cpu"), l["container.cpu.cores_used"], function (v) { return nf1.format(v) + " cores"; }, col);
      metricTile(box, t("container_memory"), l["container.memory.current_bytes"], fmtBytes, col);
      metricTile(box, t("process_memory"), l["process.rss_bytes"], fmtBytes, col);
      metricTile(box, t("disk_free"), l["disk.data.free_bytes"], fmtBytes, col);
      metricTile(box, t("network_rx"), l["net.rx_bytes_per_s"], function (v) { return fmtBytes(v) + "/s"; }, col);
      metricTile(box, t("gpu"), l["gpu.utilization_percent"], fmtPercentValue, col);
      metricTile(box, t("vram"), l["gpu.memory_used_bytes"], fmtBytes, col);
      var p = (d.series || {}).points || {};
      function pts(m) { return (p[m] || []).map(function (x) { return x.avg; }); }
      var labels = timeLabels((p["host.cpu.percent"] || []).map(function (x) { return x.t; }), d.range.bucket_ms);
      chart("sv-cpu", "line", labels, [
        lineDs(t("cpu") + " (" + t("src_host") + ")", pts("host.cpu.percent"), colors[0]),
        lineDs(t("cpu") + " (" + t("src_process") + ")", pts("process.cpu.percent"), colors[2])
      ], axisOpts(fmtPercentValue));
      chart("sv-mem", "line", labels, [
        lineDs(t("container_memory"), pts("container.memory.current_bytes"), colors[1]),
        lineDs(t("process_memory"), pts("process.rss_bytes"), colors[4]),
        lineDs(t("memory") + " " + t("src_host") + " (avail.)", pts("host.memory.available_bytes"), colors[6], { borderDash: [5, 4] })
      ], axisOpts(fmtBytes));
      chart("sv-net", "line", labels, [
        lineDs(t("network_rx"), pts("net.rx_bytes_per_s"), colors[0]),
        lineDs(t("network_tx"), pts("net.tx_bytes_per_s"), colors[3])
      ], axisOpts(function (v) { return fmtBytes(v) + "/s"; }));
      chart("sv-disk", "line", labels, [
        lineDs(t("disk_free") + " (data)", pts("disk.data.free_bytes"), colors[1]),
        lineDs(t("disk_free") + " (/)", pts("disk.root.free_bytes"), colors[5])
      ], axisOpts(fmtBytes));
    },

    security: function (d, extra) {
      lastData = d;
      var colors = palette();
      metaRange(d.range);
      var k = clear(byId("sec-kpis")), cats = d.counts_by_category || {};
      ["login", "failure", "lockout", "session", "denied", "config"].forEach(function (c) {
        kpi(k, t("cat_" + c), fmtCount(cats[c] || 0), { col: "col-6 col-md-4 col-xl-2",
          tone: (c === "failure" || c === "lockout" || c === "denied" || c === "config") && cats[c] ? "bad" : null });
      });
      var tl = d.timeline || { t: [], categories: {} };
      chart("sec-timeline", "bar", timeLabels(tl.t, d.range.bucket_ms), Object.keys(tl.categories).map(function (c, i) {
        return barDs(t("cat_" + c), tl.categories[c], colors[i % colors.length]);
      }), axisOpts(fmtCount, true));
      var ss = clear(byId("sec-sessions"));
      (d.sessions || []).forEach(function (s) {
        var r = el("tr");
        var ref = el("td", "font-monospace small", s.session_ref);
        if (s.current) { ref.appendChild(badge(t("current_session"), "text-bg-primary ms-1")); }
        r.appendChild(ref);
        r.appendChild(el("td", "small", fmtDateTime(s.created_ms)));
        r.appendChild(el("td", "small", fmtDateTime(s.last_activity_ms)));
        r.appendChild(el("td", "small", fmtDateTime(s.absolute_expires_ms)));
        ss.appendChild(r);
      });
      var tb = byId("sec-events");
      if (!extra || !extra.cursor) { clear(tb); }
      (d.events || []).forEach(function (e) {
        var r = el("tr");
        r.appendChild(el("td", "text-nowrap small", fmtDateTime(e.ts_ms)));
        var ev = el("td"); ev.appendChild(badge(e.event, (d.categories[e.event] === "login" || d.categories[e.event] === "session") ? "text-bg-light border" : "text-bg-warning"));
        r.appendChild(ev);
        r.appendChild(el("td", "small", e.username || NA));
        r.appendChild(el("td", "small font-monospace", e.ip || NA));
        r.appendChild(el("td", "small", e.reason || ""));
        r.appendChild(el("td", "small text-end", fmtCount(e.repeat_count)));
        tb.appendChild(r);
      });
      emptyToggle("sec-empty", !tb.children.length);
      paging.cursor = d.next_cursor;
      byId("sec-more").classList.toggle("d-none", !d.next_cursor);
    }
  };

  ["act-more", "sec-more"].forEach(function (id) {
    var button = byId(id);
    if (!button) { return; }
    button.addEventListener("click", function () {
      if (!paging.cursor) { return; }
      setLive(false);  // keep the loaded pages stable while browsing older entries
      sendActivity();
      load({ cursor: paging.cursor }).catch(function () { /* notice shown */ });
    });
  });

  if (!RENDER[page]) { return; }
  renderLiveButton();
  updateFreshness();
  tick();
}());
