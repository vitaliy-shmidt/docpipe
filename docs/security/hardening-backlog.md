# Security Hardening Backlog

Diese Liste beschreibt nur Eigenschaften des Quellcodes in diesem öffentlichen
Repository. Sie enthält bewusst keine Angaben zu konkreten Installationen,
Hosts, Netzwerken oder Konfigurationen. Eine Bewertung, ob und wie stark eine
bestimmte Installation betroffen ist, gehört nicht in dieses Repository.

Gefunden bei der Bestandsanalyse für das Monitoring (Phase M1,
[monitoring-dashboard.md](../architecture/monitoring-dashboard.md)).

| # | Bereich | Beobachtung | Status |
|---|---|---|---|
| H1 | API-Key-Prüfung (`Settings.find_client_by_api_key`) | Keys wurden mit `==` verglichen. | **Umgesetzt (26.11.2026, Branch `feature/monitoring-dashboard`).** SHA-256-Digests beider Seiten werden mit `hmac.compare_digest` verglichen, ohne frühen Abbruch und unabhängig von der Key-Länge; geprüft wird immer gegen alle Clients. Das Verhalten ist unverändert (gleiche Keys, gleiche Clients, bestehende Konfigurationen kompatibel). Tests: `tests/test_api_key_constant_time.py`. |
| H1b | API-Keys im Klartext in der Konfiguration | Die Server-Konfiguration enthält die Client-Keys im Klartext. | **Offen, separate Aufgabe:** Umstellung auf gehashte Keys (z. B. SHA-256 mit Key-ID-Präfix) mit abwärtskompatibler Migration. |
| H2 | Access-Log (`system/main.py`) | Die Log-Zeile enthielt den URL-dekodierten Rohpfad (Log-Injection über Steuerzeichen, Pfadparameter im Log). | **Umgesetzt (26.11.2026).** `path=` enthält das Route-Template (`/api/v1/lab/prompts/{mode}`) bzw. `<unmatched>`, die Methode wird normalisiert. Feldnamen und Request-ID-Korrelation sind unverändert. Tests: `tests/test_access_log_hardening.py`. |
| H3 | Access-Log bei unbehandelten Exceptions | Die Access-Log-Middleware schreibt bei einer unbehandelten Exception keine Zeile. | **Offen** (nicht in M3 freigegeben). Das Monitoring erfasst diese Requests seit M2 (Status 500, Event `unhandled_exception`). Vorschlag: `try/finally` in der Middleware. |
| H4 | Fehlermeldungen (`DocPipeError.message`) | Einige Meldungen enthalten Werte aus dem Request und gehen in die API-Antwort. | **Offen** (nicht in M3 freigegeben). Das Monitoring speichert nur Fehlercodes, nie Meldungen. Vorschlag: Request-Werte nicht übernehmen oder gekürzt und escaped. |
| H5 | Proxy-Header | Uvicorn wertet `X-Forwarded-For` standardmäßig selbst aus (für 127.0.0.1). | **Umgesetzt mit M3 (26.11.2026).** Das `Dockerfile` startet Uvicorn mit `--no-proxy-headers`; die API nutzt weder Client-Adresse noch Schema. Forwarded-Header werden genau an einer Stelle ausgewertet (Monitoring-Weboberfläche, nur von `trusted_proxies`); erkennbar falsche Uvicorn-Starts sperren die Oberfläche. Deploy-Anforderung: [monitoring-dashboard.md §14.10](../architecture/monitoring-dashboard.md). |

## Bewertung zum Release-Kandidaten (Phase M7)

Erneut geprüft auf dem Branch `feature/monitoring-dashboard`. Die Tests stehen in
[monitoring-dashboard.md §17](../architecture/monitoring-dashboard.md). Bewertet wird
der Quellcode, nicht eine bestimmte Installation.

| # | Risiko | Schwere | Dringlichkeit | Release-blockierend? |
|---|---|---|---|---|
| H1, H2, H5 | umgesetzt, Tests erneut grün | – | – | nein |
| H1b | Kunden-Keys im Klartext in `config.yaml` (vorbestehend, unabhängig vom Monitoring) | mittel | mittel | nein für das Monitoring. Datei mit 0600 bzw. read-only mounten (Betriebsanleitung §4.1). Umstellung als eigene Aufgabe. |
| H3 | keine Access-Log-Zeile bei unbehandelter Exception (das Monitoring erfasst sie) | niedrig | niedrig | nein |
| H4 | Request-Werte in einzelnen Fehlermeldungen der API-Antwort (an denselben Aufrufer; nicht im Log, nicht im Monitoring) | niedrig | niedrig | nein |
| M3-1 | Lockout-DoS: Fehlversuche auf einen bekannten Benutzernamen sperren ihn für 15 min | `allowlist`: niedrig; `public_mfa`: mittel | – | nein für `allowlist`. `public_mfa` ist **nicht** für die Produktion freigegeben. |
| M3-2 | Proxy-Header-Auswertung nur für den Uvicorn-CLI-Start technisch erkannt; andere Launcher nicht | niedrig | niedrig | nein. Das Image startet korrekt; der Prüfpunkt steht in der Betriebsanleitung §4.2. |
| M3-3 | Eigentümer und Rechte unter Linux: Benutzerdatei (Eigentümer, 0600) und Datenbanken (0600, inklusive `-wal`/`-shm`) werden geprüft bzw. gesetzt. Ein **bereits vorhandenes** Datenverzeichnis behält seine Rechte. | niedrig | niedrig | nein. Das Verzeichnis mit 0700 anlegen (Betriebsanleitung §4.1). |
| M3-4 | Sessions nicht an die IP gebunden | niedrig | niedrig | nein |
| M7-1 | `requirements.txt` mit Versionsbereichen: ein neuer Build kann andere Abhängigkeiten ziehen. `pip-audit` (Stand M7): keine bekannten Schwachstellen. | niedrig | mittel | nein. Für das Release-Image `pip freeze` festhalten und gegen dieses Set prüfen. |
| M7-2 | Backups der Auth-Datenbank enthalten Sessions und Schlüsselmaterial | mittel, falls ein Backup offenliegt | – | nein. Backups wie Geheimnisse behandeln (Betriebsanleitung §8). |

Keine der Positionen ist für den Monitoring-Code release-blockierend. Release-blockierende
**Betriebsaufgaben** (think-Patch, PDF4Fill-Produktionsprüfung, Messungen auf dem
Zielsystem) stehen in §17 der Architektur.
