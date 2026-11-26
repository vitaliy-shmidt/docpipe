/* DocPipe Monitoring - theme and language preferences (all pages).
 * No inline scripts (CSP script-src 'self'). A theme change emits "dpm:theme" so the
 * dashboard can recolour its charts. */
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
      var active = button.getAttribute("data-theme-choice") === choice;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
    document.dispatchEvent(new CustomEvent("dpm:theme"));
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(storedTheme());
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        if (storedTheme() === "auto") { applyTheme("auto"); }
      });
    }
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
  });
}());
