# Security Hardening Backlog

Status: **identifiziert, nicht umgesetzt**. Jeder Punkt ist eine eigene, separat
freizugebende Änderung und gehört nicht zu den Monitoring-Phasen M2–M7.

Diese Liste beschreibt nur Eigenschaften des Quellcodes in diesem öffentlichen
Repository. Sie enthält bewusst keine Angaben zu konkreten Installationen,
Hosts, Netzwerken oder Konfigurationen. Eine Bewertung, ob und wie stark eine
bestimmte Installation betroffen ist, gehört nicht in dieses Repository.

Gefunden bei der Bestandsanalyse für das Monitoring (Phase M1,
[monitoring-dashboard.md](../architecture/monitoring-dashboard.md)).

| # | Bereich | Beobachtung im Code | Vorschlag | Bezug zum Monitoring |
|---|---|---|---|---|
| H1 | API-Key-Prüfung (`system/auth.py`, `Settings.find_client_by_api_key`) | Client-Keys liegen als Klartext in der Server-Konfiguration und werden mit `==` verglichen. | Vergleich mit `hmac.compare_digest`. Mittelfristig nur Hashes der Keys in der Konfiguration (z. B. SHA-256 mit Key-ID-Präfix) und abwärtskompatible Migration. | keiner; das Monitoring nutzt Kunden-Keys nie |
| H2 | Access-Log (`system/main.py`) | Die Log-Zeile enthält den URL-dekodierten Rohpfad. Steuerzeichen im Pfad landen unverändert im Log. | Route-Template statt Rohpfad loggen oder Steuerzeichen escapen. | Das Monitoring speichert nur Route-Templates (M2) und ist nicht betroffen. |
| H3 | Access-Log bei unbehandelten Exceptions | Die Access-Log-Middleware schreibt bei einer unbehandelten Exception keine Zeile, nur der Traceback wird geloggt. | `try/finally` in der Middleware. | Das Monitoring erfasst diese Requests seit M2 (Status 500, Event `unhandled_exception`). |
| H4 | Fehlermeldungen (`DocPipeError.message`) | Einige Meldungen enthalten Werte aus dem Request (z. B. einen unbekannten Modusnamen) und gehen in die API-Antwort. | Request-Werte nicht in Meldungen übernehmen, oder nur gekürzt und escaped. | Das Monitoring speichert nur den Fehlercode, nie die Meldung. |
| H5 | Uvicorn-Proxy-Header | Uvicorn wertet `X-Forwarded-For` standardmäßig für Verbindungen von `127.0.0.1` aus. Die Client-Adresse kann dadurch schon vor der Anwendung verändert sein. | Deploy-Doku: Proxy-Header-Verarbeitung bewusst festlegen. Für das Monitoring ist sie in der Architektur verbindlich definiert (§3.6). | M3-Voraussetzung |

Reihenfolge-Vorschlag: H1 und H2 zuerst (klein, abwärtskompatibel), H5 zusammen
mit M3, H3 und H4 bei nächster Gelegenheit.
