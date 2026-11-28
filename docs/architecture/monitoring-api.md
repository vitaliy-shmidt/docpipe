# Monitoring-API (`/monitor/api/v1/`)

Stand: Phase M4 (26.11.2026). Gehört zur Operator-Weboberfläche,
[monitoring-dashboard.md](monitoring-dashboard.md) §14–§15. Die API ist **nicht**
Teil der Kunden-API (`/api/v1/`) und erscheint nicht im OpenAPI-Schema.

## Zugriff

Für jeden Endpunkt gilt dieselbe Kette wie für die Seiten (fail-closed):

1. Netzwerkprüfung (Allowlist bzw. `public_mfa`, Trusted Proxies, HTTPS) → sonst 403.
2. Gültige Monitoring-Session (Cookie `__Host-dp_mon`, nach Passwort und TOTP) → sonst
   `401 {"ok": false, "code": "unauthorized"}`. Kunden-API-Keys werden nie ausgewertet.
3. Benutzerdatei und Auth-Datenbank verfügbar → sonst 503.
4. Abfragebegrenzung pro Session (`monitoring.web.api_requests_per_minute`, Default 240) →
   sonst `429 {"code": "rate_limited"}`.

Weitere Eigenschaften:

- Nur `GET`; andere Methoden ergeben 405.
- Antworten tragen `Cache-Control: no-store` und die Security-Header der Oberfläche.
- Keine dieser Abfragen verlängert die Idle-Zeit der Session.
- Die Antworten lesen ausschließlich bereits gespeicherte Daten: Ollama und Stirling werden
  dabei nie kontaktiert.
- Monitoring-Requests fließen nicht in die Kundenstatistik ein.

## Parameter

Query-Parameter werden streng geprüft:

- höchstens 1 KiB und 12 Felder,
- jeder Name nur einmal,
- gültiges UTF-8.

Ein unbekannter Parametername oder ein ungültiger Wert ergibt
`400 {"ok": false, "code": "<grund>"}`. SQL wird ausschließlich mit gebundenen
Parametern gebaut. Werte kommen nur aus festen Registries oder aus den Client-IDs der
Konfiguration.

| Parameter | Werte | Endpunkte |
|---|---|---|
| `range` | `live` (15 min), `1h`, `24h`, `7d`, `30d`, `custom` | alle außer `meta` |
| `from`, `to` | UTC-Epoch-Millisekunden, nur mit `range=custom`; mindestens 1 min, innerhalb der Stunden-Rollup-Retention | wie `range` |
| `client` | konfigurierte Client-ID oder `unauthenticated` | `activity`, `performance` |
| `service` | Dienst aus der Registry (`documents`, `ai`, `forms`, `prompt_lab`, `assistant`, `system`, `other`) | `activity`, `clients`, `performance` |
| `route` | registriertes Route-Template | `performance` |
| `status` | `success`, `failed`, `client_error`, `server_error` | `activity` |
| `category` | `login`, `failure`, `lockout`, `session`, `denied`, `config` | `security` |
| `limit` | 1–100 (größere Werte werden auf 100 begrenzt) | `activity`, `security` |
| `cursor` | `next_cursor` der vorherigen Seite (`<ts_ms>.<id>`) | `activity`, `security` |

## Zeiträume, Quellen und Genauigkeit

Jede Antwort enthält `range`:

```json
{"key": "24h", "start_ms": 0, "end_ms": 0, "source": "minute", "bucket_ms": 900000,
 "effective_start_ms": 0, "approximate": true}
```

**Quellenwahl:**

| Zeitraum | Quelle | Bucket |
|---|---|---|
| ≤ 1 h, innerhalb der Rohdaten-Retention | `raw` | 10 s / 30 s |
| ≤ 24 h, innerhalb der Minuten-Retention | `minute` | 15 min |
| darüber | `hour` | 2 h bzw. 6 h |

- Höchstens 120 Punkte pro Reihe.
- Liegen mehr als 200.000 Rohzeilen im Zeitraum, wird auf Minuten-Rollups ausgewichen.
- **Eine Quelle pro Antwort:** Summen, Zeitreihen und Perzentile kommen immer aus
  derselben Quelle. Rohdaten und Rollups werden nie addiert.
- **Bucket-Ausrichtung:** Rollup-Zeiträume beginnen am Bucket-Anfang `effective_start_ms`.
- **Perzentile** (`percentiles.method`):
  - `exact`: Nearest Rank über Rohzeilen.
  - `histogram`: Schätzung aus Latenz-Histogrammen; die Oberfläche zeigt dann „≈ Schätzung“.
- **Lücken:** Ein Zeitbucket hat `count: 0` nur, wenn der Collector in ihm nachweislich lief
  (Heartbeats). Ohne Heartbeat ist der Wert `null` (Lücke). `coverage` ist der abgedeckte
  Anteil (0–1); `null` bedeutet unbekannt, etwa noch nicht geschrieben oder außerhalb der
  Datenqualitäts-Retention.
- **Quoten** sind `null`, wenn der Nenner 0 ist.
- **Zeit:** Alle Zeitstempel sind UTC-Epoch-Millisekunden. Die Oberfläche zeigt sie in der
  Zeitzone des Browsers.

## Endpunkte

| Endpunkt | Inhalt |
|---|---|
| `GET /overview` | `totals` (Anzahl, Erfolg/Fehler, Quoten, Ø, Min/Max, Bytes, Dokumente, Seiten, KI-Aufrufe, Tokens), `percentiles` (p50/p95/p99 + `method`), `series` (Anzahl, Erfolg, Fehler, Ø, p95, `coverage`), `clients_series`, `completeness`, `services`, `system` (letzte Messwerte mit `source`/`status`/`reason`), `system_series`, `build`, `uptime_ms`, `active_requests` |
| `GET /activity` | `active` (laufende Requests aller Worker, `stale` bei altem Snapshot), `items` (abgeschlossene Requests: Request-ID, Zeit, Client, Route-Template, Dienst, HTTP-Status, Fehlercode, Dauer, KI-Modell, Schritte), `next_cursor`, `data_until_ms` |
| `GET /clients` | pro Client und separat `unauthenticated`: Anfragen, Anteil, Erfolgs- und Fehlerquote, Ø, p95 (+ Methode), Dokumente, Seiten (+ `pages_known`), Bytes, KI-Aufrufe, Tokens, Nutzung nach Dienst, Vorperiode; `series`, `completeness` |
| `GET /performance` | `requests` (Gesamtdauer-Statistik), `steps` (pro Schritt: Anzahl, Fehlerquote, Ø/Median/p95/p99/Min/Max, `duration_kind` = `inclusive`, `exclusive_avg_ms` bei verschachtelten Schritten, `parent`, von Ollama gemeldete Teilzeiten und Tokens/s), `step_series`, `nesting`, `completeness`. Ein Client-Filter ist nur innerhalb der Rohdaten-Retention möglich (`400 client_filter_requires_raw_retention`). |
| `GET /server` | `latest` (Host, Container, Prozess, Dateisystem, Netz-Namespace, GPU – mit Herkunft, Status `ok`/`stale`/`unavailable`/`not_applicable` und Grund), `series`, `services` |
| `GET /security` | `counts`, `counts_by_category`, `timeline`, `events` (Audit, nach Zeit absteigend, paginiert), `sessions` (eigene Sessions mit 12-stelliger Referenz) |
| `GET /ollama` | Phase M5, siehe unten |
| `GET /stirling` | Phase M5, siehe unten |
| `GET /meta` | Bereiche, Poll-Intervalle, Retention, älteste Daten je Quelle, Build, bekannte Clients, Dienste, Routen |
| `GET /session`, `GET /status` | aus M3, unverändert |

`POST /monitor/session/activity` (CSRF und Origin, Antwort 204) markiert eine
**ausdrückliche Benutzeraktion**, etwa einen Filterwechsel. Die Wirkung ist begrenzt:

- höchstens alle 30 s,
- nie über den absoluten Session-Ablauf hinaus,
- wird nie durch Timer aufgerufen.

Der Server kann nicht beweisen, dass ein Mensch die Aktion ausgelöst hat. Er akzeptiert sie
nur von der angemeldeten Seite (sessiongebundenes Token, gleicher Origin).

**Fehlercodes:**

| HTTP | Code |
|---|---|
| 400 | `invalid_range`, `invalid_from`, `invalid_to`, `invalid_client`, `invalid_service`, `invalid_route`, `invalid_status`, `invalid_model`, `invalid_operation`, `invalid_category`, `invalid_cursor`, `invalid_limit`, `unknown_parameter`, `range_outside_raw_retention`, `client_filter_requires_raw_retention`, `invalid_query` |
| 401 | `unauthorized` |
| 429 | `rate_limited` |
| 503 | `telemetry_unavailable` – Telemetrie-Datenbank nicht lesbar; Anmeldung und Kunden-API sind nicht betroffen |

## Ollama und Stirling PDF (Phase M5)

Beide Endpunkte lesen ausschließlich gespeicherte Daten. Kein Browser-Request löst einen
Aufruf an Ollama oder Stirling aus – also auch keine Generierung, kein Modellladen und keine
PDF-Verarbeitung. Gezählt wird nur, was DocPipe selbst beobachtet hat (`scope:
"docpipe_observed"`): Andere Anwendungen, die Ollama oder Stirling direkt nutzen, erscheinen
nicht.

**Quelle:** dieselbe Regel wie bei den übrigen Bereichen (Rohdaten bis 1 h, Minuten-Rollups
bis 24 h, darüber Stunden-Rollups; über 200.000 passenden Rohzeilen Minuten-Rollups). Ein
Client-Filter braucht Rohdaten und nutzt sie innerhalb der Rohdaten-Retention auch für längere
Zeiträume, begrenzt durch das Abfragebudget (`400 client_filter_requires_raw_retention`); `by_client` ist bei
Rollup-Quelle `null`.

### `GET /ollama`

Parameter: `range`/`from`/`to`, `model` (konfigurierte Modelle), `client`, `operation`
(`analyze`, `assistant`, `prompt_lab`, `warmup`, `other`), `status` (`success`, `failed`).

| Feld | Inhalt |
|---|---|
| `health` | Zustand aus den gespeicherten Statusprüfungen: Version, letzte Prüfung, letzter Erfolg |
| `probes` | Statusverlauf je Bucket (`ok`, `failed`, `latency_ms`), `status_counts`, `failures` (`timeout`, `connection_error`, `http_error`, `invalid_response`), `recent` (letzte 20 Prüfungen) |
| `models` | letzter Modellstand (höchstens 7 Tage alt): `installed`, `loaded` (Name, Familie, Größe, Quantisierung, Parameter, Kontext, `size_vram_bytes`, `expires_ms`), `measured_ms`, `stale`, `reported_vram_bytes` |
| `active` | laufende KI-Aufrufe, die DocPipe nachweislich ausführt: Request-ID, Client, Modell, Aufrufart, Start, Alter, `state` (`running`, bei veraltetem Snapshot eines anderen Workers `unknown`), `stale` |
| `calls` | KI-Aufrufe ohne Warm-ups: Anzahl, Erfolg/Fehler, Quoten, Ø/Median/p95/p99/Min/Max (von DocPipe gemessen, `percentile_method`), `outcomes`, `ollama` (von Ollama gemeldete Ø-Ladezeit, Prompt-Verarbeitung, Generierung), `tokens`, `throughput` |
| `warmups` | Warm-ups, getrennt von `calls` |
| `by_model`, `by_operation`, `by_client` | dieselben Kennzahlen je Gruppe |
| `series` | je Bucket: Erfolg/Fehler, Ø, p95, Tokens, Ladezeit, Tokens/s, Fehler nach Ergebnis, `coverage` |
| `gpu` | `docpipe_host` (GPU-Werte des DocPipe-Hosts mit Status und Grund) und `ollama_location` (`local`, `remote`, `unknown`; die Adresse selbst wird nie ausgegeben) |
| `completeness` | wie bei den anderen Bereichen |

**Abgrenzungen:**

- **Installiert, geladen und aktiv sind verschiedene Dinge.** Ein geladenes Modell
  (`/api/ps`) bedeutet nicht, dass etwas verarbeitet wird. Aktiv sind nur Einträge in
  `active`.
- **`size_vram_bytes`** ist die von Ollama gemeldete Speicherbelegung, keine GPU-Auslastung.
- **`expires_ms`** ist die Ablaufzeit laut letzter Messung. Ist sie erreicht, ist die
  Entladung damit nicht bestätigt; erst die nächste Messung zeigt den Stand.

**Durchsatz (`throughput`):** gewichtet, `Summe(Tokens) / Summe(Sekunden)`. Es zählen nur
Aufrufe, bei denen die Token-Anzahl und die zugehörige von Ollama gemeldete Dauer (> 0) beide
bekannt sind – `generation_tokens_per_s` aus Ausgabe-Tokens und Generierungsdauer,
`prompt_tokens_per_s` aus Eingabe-Tokens und Prompt-Verarbeitungsdauer. Das ist die
Geschwindigkeit, während Ollama an DocPipe-Aufrufen arbeitete, **nicht** der Durchsatz des
Servers über die Zeit einschließlich Leerlauf. `generation_pairs`/`prompt_pairs` nennen die
Anzahl der Paare. Rollup-Buckets aus der Zeit vor Schema v3 haben keine Paarsummen:
`calls_without_pair_data` zählt sie, `partial: true` markiert den Wert als nur teilweise
verfügbar. Sie werden nie als 0 gewertet.

### `GET /stirling`

Parameter: `range`/`from`/`to`, `client`, `operation` (`extract_text`, `ocr`, `convert`),
`status` (`success`, `failed`).

| Feld | Inhalt |
|---|---|
| `health`, `probes`, `completeness` | wie bei `/ollama` |
| `total` | alle Stirling-Operationen: Anzahl, Erfolg/Fehler, Quoten, Ø/Median/p95/p99/Min/Max, `outcomes` |
| `by_operation` | dieselben Kennzahlen je Operation |
| `by_route` | aufrufende DocPipe-Endpunkte (z. B. OCR aus der Formularanalyse) |
| `by_client` | je Client (nur Rohdaten) |
| `series` | `t`, `coverage` und je Operation `count`, `failed`, `avg_ms` |

Gezählt werden nur echte Stirling-Aufrufe (Schritte `stirling.extract_text`, `stirling.ocr`,
`stirling.convert`). DocPipes eigene Formularanalyse und -befüllung (`forms.inspect`,
`forms.fill`) laufen in DocPipe und sind keine Stirling-Nutzung.

### Schema v3 der Telemetrie-Datenbank

Die Migration v3 ergänzt `rollup_steps` um sechs Paarsummen (`eval_pair_tokens`,
`eval_pair_ms`, `eval_pairs`, `prompt_pair_tokens`, `prompt_pair_ms`, `prompt_pairs`) und legt
die Snapshot-Tabelle `live_ai_steps` an. Dabei gilt:

- **Bestehende Daten bleiben unverändert.** Die Auth-Datenbank wird nicht berührt.
- **Nachberechnung nur für vollständige Buckets:** Bestehende Buckets werden nur aus Rohzeilen
  nachberechnet, wenn deren Anzahl exakt mit dem Rollup übereinstimmt. Die Nachberechnung ist
  auf 2 Mio. Rohzeilen begrenzt.
- **Unvollständige Buckets bleiben `NULL`.** Spätere Additionen halten sie bei `NULL`, sodass
  nie eine Teilsumme als vollständig erscheint.
- **Mehrere Worker:** Die Migration läuft unter `BEGIN IMMEDIATE` mit erneuter Versionsprüfung
  und damit genau einmal.

