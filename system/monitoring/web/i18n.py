"""German/English texts of the monitoring interface.

Language: the `dp_mon_lang` cookie (set by the language switch) if it is
"de" or "en", otherwise the first supported language in Accept-Language,
otherwise English.
"""

from __future__ import annotations

LANGUAGES = ("de", "en")
FALLBACK = "en"

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "title": "DocPipe Monitoring",
        "subtitle": "Operator access - read-only technical monitoring",
        "username": "Username",
        "password": "Password",
        "sign_in": "Sign in",
        "continue": "Continue",
        "mfa_title": "Two-factor authentication",
        "mfa_hint": "Enter the 6-digit code from your authenticator app.",
        "mfa_code": "Authentication code",
        "verify": "Verify",
        "back_to_login": "Back to sign-in",
        "login_failed": "Sign-in failed. Check your details or try again later.",
        "mfa_failed": "The code could not be verified. Check the code or try again later.",
        "login_expired": "The sign-in has expired. Please sign in again.",
        "request_rejected": "The request was rejected. Please reload the page and try again.",
        "logout": "Sign out",
        "signed_in_as": "Signed in as",
        "session_ends": "Session ends without activity at",
        "theme": "Theme",
        "theme_light": "Light",
        "theme_dark": "Dark",
        "theme_auto": "System",
        "language": "Language",
        "menu": "Menu",
        "nav_overview": "Overview",
        "nav_activity": "Live Activity",
        "nav_clients": "Projects & Clients",
        "nav_performance": "Performance",
        "nav_server": "Server",
        "nav_ollama": "Ollama",
        "nav_stirling": "Stirling PDF",
        "nav_connectors": "AI Connectors",
        "nav_logs": "Logs",
        "nav_security": "Security",
        "planned": "planned",
        "shell_heading": "Monitoring shell",
        "shell_notice": "Statistics and performance dashboards are not implemented yet (phase M4). "
        "Sections marked as planned contain no data.",
        "status_heading": "Data collection",
        "status_collector": "Collector",
        "status_store": "Telemetry store",
        "status_last_flush": "Last write",
        "status_buffered": "Buffered records",
        "status_dropped": "Dropped records (this process)",
        "status_unavailable": "Not available",
        "last_updated": "Last updated",
        "paused": "Updates paused while the tab is hidden",
        "denied_title": "Access denied",
        "denied_text": "Access to this page is not permitted.",
        "unavailable_title": "Monitoring unavailable",
        "unavailable_text": (
            "The monitoring interface is currently unavailable. DocPipe's API is not affected."
        ),
        "not_found": "Not found",
    },
    "de": {
        "title": "DocPipe Monitoring",
        "subtitle": "Betreiberzugang – technische Überwachung, nur lesend",
        "username": "Benutzername",
        "password": "Passwort",
        "sign_in": "Anmelden",
        "continue": "Weiter",
        "mfa_title": "Zwei-Faktor-Authentifizierung",
        "mfa_hint": "Geben Sie den 6-stelligen Code aus Ihrer Authenticator-App ein.",
        "mfa_code": "Sicherheitscode",
        "verify": "Bestätigen",
        "back_to_login": "Zurück zur Anmeldung",
        "login_failed": (
            "Anmeldung fehlgeschlagen. Bitte prüfen Sie Ihre Angaben oder versuchen Sie es später erneut."
        ),
        "mfa_failed": "Der Code konnte nicht bestätigt werden. Bitte prüfen Sie den Code oder versuchen "
        "Sie es "
        "später erneut.",
        "login_expired": "Die Anmeldung ist abgelaufen. Bitte melden Sie sich erneut an.",
        "request_rejected": (
            "Die Anfrage wurde abgelehnt. Bitte laden Sie die Seite neu und versuchen Sie es erneut."
        ),
        "logout": "Abmelden",
        "signed_in_as": "Angemeldet als",
        "session_ends": "Sitzung endet ohne Aktivität um",
        "theme": "Darstellung",
        "theme_light": "Hell",
        "theme_dark": "Dunkel",
        "theme_auto": "System",
        "language": "Sprache",
        "menu": "Menü",
        "nav_overview": "Übersicht",
        "nav_activity": "Live-Aktivität",
        "nav_clients": "Projekte & Clients",
        "nav_performance": "Performance",
        "nav_server": "Server",
        "nav_ollama": "Ollama",
        "nav_stirling": "Stirling PDF",
        "nav_connectors": "KI-Connectoren",
        "nav_logs": "Logs",
        "nav_security": "Sicherheit",
        "planned": "geplant",
        "shell_heading": "Monitoring-Oberfläche",
        "shell_notice": "Statistik- und Performance-Dashboards sind noch nicht umgesetzt (Phase M4). "
        "Als geplant markierte Bereiche enthalten keine Daten.",
        "status_heading": "Datenerfassung",
        "status_collector": "Collector",
        "status_store": "Telemetrie-Speicher",
        "status_last_flush": "Letzter Schreibvorgang",
        "status_buffered": "Gepufferte Datensätze",
        "status_dropped": "Verworfene Datensätze (dieser Prozess)",
        "status_unavailable": "Nicht verfügbar",
        "last_updated": "Zuletzt aktualisiert",
        "paused": "Aktualisierung pausiert, solange der Tab verborgen ist",
        "denied_title": "Zugriff verweigert",
        "denied_text": "Der Zugriff auf diese Seite ist nicht erlaubt.",
        "unavailable_title": "Monitoring nicht verfügbar",
        "unavailable_text": "Die Monitoring-Oberfläche ist derzeit nicht verfügbar. Die DocPipe-API ist "
        "davon "
        "nicht betroffen.",
        "not_found": "Nicht gefunden",
    },
}

# Navigation: (key, icon, phase that implements it). None = available now.
SECTIONS = (
    ("nav_overview", "speedometer2", None),
    ("nav_activity", "activity", "M4"),
    ("nav_clients", "people", "M4"),
    ("nav_performance", "graph-up", "M4"),
    ("nav_server", "hdd-stack", "M4"),
    ("nav_ollama", "cpu", "M5"),
    ("nav_stirling", "file-earmark-pdf", "M5"),
    ("nav_connectors", "plug", "#2"),
    ("nav_logs", "journal-text", "M6"),
    ("nav_security", "shield-lock", "M4"),
)


def choose_language(cookie: str | None, accept_language: str | None) -> str:
    if cookie in LANGUAGES:
        return cookie
    candidates = []
    for index, part in enumerate((accept_language or "")[:500].split(",")):
        tag, _, params = part.strip().partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        primary = tag.strip().lower().split("-")[0]
        if primary in LANGUAGES and q > 0:
            candidates.append((-q, index, primary))
    return min(candidates)[2] if candidates else FALLBACK
