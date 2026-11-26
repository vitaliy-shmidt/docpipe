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
