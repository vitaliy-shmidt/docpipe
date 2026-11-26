/* DocPipe Monitoring - theme/language preferences and the shell's status polling.
 * No inline scripts (CSP script-src 'self'); data is only written with textContent.
 * Automatic requests are marked with X-Monitor-Poll; independently of that header the
 * server never extends the idle timeout for /monitor/api/* requests. */
(function () {
  "use strict";

  var THEME_KEY = "dpm-theme";

  function storedTheme() {
    try { return window.localStorage.getItem(THEME_KEY) || "auto"; } catch (e) { return "auto"; }
  }

  function applyTheme(choice) {
    var dark = choice === "dark" ||
      (choice === "auto" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.setAttribute("data-bs-theme", dark ? "dark" : "light");
    document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
      button.classList.toggle("active", button.getAttribute("data-theme-choice") === choice);
      button.setAttribute("aria-pressed", button.getAttribute("data-theme-choice") === choice ? "true" : "false");
    });
  }

  function setupPreferences() {
    applyTheme(storedTheme());
    document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
      button.addEventListener("click", function () {
        var choice = button.getAttribute("data-theme-choice");
        try { window.localStorage.setItem(THEME_KEY, choice); } catch (e) { /* private mode */ }
        applyTheme(choice);
      });
    });
    document.querySelectorAll("[data-lang-choice]").forEach(function (button) {
      button.addEventListener("click", function () {
        var lang = button.getAttribute("data-lang-choice");
        if (lang !== "de" && lang !== "en") { return; }
        document.cookie = "dp_mon_lang=" + lang + "; Path=/monitor; Max-Age=31536000; SameSite=Strict" +
          (window.location.protocol === "https:" ? "; Secure" : "");
        window.location.reload();
      });
    });
  }

  function formatTime(ms) {
    if (typeof ms !== "number") { return null; }
    return new Date(ms).toLocaleTimeString(document.documentElement.lang || undefined);
  }

  function poll(url) {
    return fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Accept": "application/json", "X-Monitor-Poll": "1" }
    }).then(function (response) {
      if (response.status === 401) {
        window.location.assign("/monitor/login");
        throw new Error("session ended");
      }
      if (!response.ok) { throw new Error("HTTP " + response.status); }
      return response.json();
    });
  }

  function setupShell() {
    var status = document.getElementById("dpm-status");
    var updated = document.getElementById("dpm-updated");
    var idle = document.getElementById("dpm-session-idle");
    if (!status) { return; }
    var unavailable = status.getAttribute("data-unavailable");

    function showIdle(ms) {
      if (idle) { idle.textContent = formatTime(ms) || unavailable; }
    }
    showIdle(Number(idle && idle.getAttribute("data-ms")));

    function render(data) {
      var values = {
        collector: data.collector,
        store: data.store,
        last_flush_ms: formatTime(data.last_flush_ms),
        requests_buffered: data.requests_buffered,
        dropped: data.dropped
      };
      status.querySelectorAll("[data-field]").forEach(function (cell) {
        var value = values[cell.getAttribute("data-field")];
        cell.textContent = value === null || value === undefined ? unavailable : String(value);
      });
      if (updated) { updated.textContent = updated.getAttribute("data-label") + ": " + formatTime(Date.now()); }
    }

    function renderUnavailable() {
      status.querySelectorAll("[data-field]").forEach(function (cell) { cell.textContent = unavailable; });
    }

    function refresh() {
      if (document.hidden) {
        if (updated) { updated.textContent = updated.getAttribute("data-paused"); }
        return;
      }
      poll("/monitor/api/v1/status").then(render).catch(renderUnavailable);
      poll("/monitor/api/v1/session").then(function (s) { showIdle(s.idle_expires_ms); }).catch(function () {});
    }

    refresh();
    window.setInterval(refresh, 30000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) { refresh(); } });
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupPreferences();
    if (document.body.getAttribute("data-page") === "shell") { setupShell(); }
  });
}());
