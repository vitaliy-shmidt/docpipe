# Monitoring Dashboard – Architektur und Entwicklungsplan

Status (26.11.2026, Branch `feature/monitoring-dashboard`, Basis `origin/main` = `0c05f96`):

| Phase | Stand |
|---|---|
| M1 Bestandsanalyse und Konzept | **freigegeben** (26.11.2026), Entscheidungen in §11 |
| M2 Monitoring Foundation | **abgenommen** (26.11.2026), §13 |
| M3 Secure Monitoring Access | **abgenommen** (26.11.2026), §14 |
| M4 Dashboard und Live-Daten | **implementiert und lokal getestet** (§15, API: [monitoring-api.md](monitoring-api.md)), Draft-PR #6 für die GitHub-CI, nicht in `main`, nicht deployt |
| M5–M7 | nicht begonnen – jeweils eigene Freigabe |
Issue: [#3](https://github.com/vitaliy-shmidt/docpipe/issues/3).
Bezüge: [#2](https://github.com/vitaliy-shmidt/docpipe/issues/2) (AI Connectors),
[#4](https://github.com/vitaliy-shmidt/docpipe/issues/4) (Document Evidence & Source Mapping).

Dieses Repository ist öffentlich. Hostnamen, Serverpfade, Containernamen,
Zugangsdaten und Details zur konkreten Produktionsabsicherung stehen deshalb
bewusst **nicht** in diesem Dokument (gleiche Regel wie in
[performance-roadmap.md](../performance-roadmap.md)).

Kennzeichnung in diesem Dokument:

- **[V]** verifiziert – im Code dieses Repos oder im Quellcode des Upstream-Projekts geprüft.
- **[A]** Annahme – ohne Zugriff auf den Produktionsserver nicht überprüfbar.
- **[E]** Empfehlung – Konzeptentscheidung, Freigabe ausstehend.

---

## 1. Ist-Zustand

### 1.1 Repository-Stand [V]

| Punkt | Befund |
|---|---|
| `origin/main` | `0c05f96` (Merge PR #5, `fix/forms-pdf-name-encoding`) – identischer Tree wie `6004e02`. |
| Arbeitsbranch | `feature/monitoring-dashboard`, abgezweigt von `origin/main`, ohne lokale fremde Änderungen. |
| Lokale Altbranches | `feature/forms-inspect`, `feature/pdf4fill-capability-audit`, `fix/forms-pdf-name-encoding` – unverändert. |
| Version | `system/__init__.py`: `VERSION = "0.1.0"` (statisch, keine Build-/Commit-Information). |
| CI | GitHub Actions: `ruff check system tests` + `pytest` (Python 3.12). |
| Produktions-Compose | `compose.production.yaml` ist **nicht** im Repository; im Repo nur `docker-compose.yml` (lokale Convenience). |
| Lokaler Produktionspatch | `"think": False` in `system/services/ollama.py` ist **nicht** im Repo; der Payload in `OllamaClient._call` enthält kein `think`. Bereits in `docs/staging-deployment.md` als zu erhaltender Hotfix vermerkt. |

### 1.2 Architektur [V]

```text
Client (HubDix, PDF4Fill)
   │  Authorization: Bearer <client-key>
   ▼
Reverse Proxy (HTTPS-Terminierung)            [A] Produkt/Konfiguration unbekannt
   ▼
DocPipe – FastAPI, 1 Uvicorn-Prozess          Dockerfile: CMD uvicorn ... (keine --workers)
   │  HTTP-Middleware: request_id (uuid4), Access-Log-Zeile
   │  Routen = sync `def` → Starlette-Threadpool (anyio, Default 40 Threads)
   │
   ├─ StirlingClient (httpx, sync, follow_redirects=False)
   │     /api/v1/convert/pdf/text, /api/v1/misc/ocr-pdf,
   │     /api/v1/convert/file/pdf, /api/v1/convert/img/pdf
   ├─ OllamaClient (httpx, sync, follow_redirects=False)
   │     POST /api/generate (stream=false, format=<schema>, keep_alive)
   └─ In-Process: pikepdf/pdfminer/reportlab/Pillow (forms inspect/fill)
```

- **Keine Datenbank, kein Session-State, keine Hintergrundprozesse, keine Queue.**
  Konfiguration wird einmal beim Start aus YAML geladen (`system/config.py`).
- Persistenter Zustand existiert nur für Prompt Lab (`runtime-prompts/`, Dateisystem).
- Laut `docs/performance-roadmap.md` (gemessene Staging-Topologie): DocPipe mit
  Host-Networking auf `127.0.0.1:8000`, Stirling als Container auf Loopback,
  **Ollama als Host-Dienst** auf Loopback, keine GPU für Ollama verfügbar.
  Ob die Produktion identisch aufgebaut ist: **[A]**.

### 1.3 Authentifizierung und Clients [V]

- `system/auth.py`: `Authorization: Bearer <key>` → `Settings.find_client_by_api_key()`
  → `ClientConfig` aus der YAML-Datei. `request.state.client_id` wird gesetzt.
- Dienst-Flags pro Client: `documents`, `ai`, `prompt_lab`, `assistant`, `forms`.
- Es gibt **kein Mandanten- oder Projektmodell** über `client_id` hinaus. „Projekt“
  und „API-Client“ sind im Monitoring daher dasselbe Merkmal (`client_id`).
  Mandanten-Auswertungen sind ohne Erweiterung der Identität nicht möglich und
  dürfen nicht aus Request-Bodies abgeleitet werden.
- Die zwei produktiven Clients (HubDix: documents/ai/prompt_lab/assistant;
  PDF4Fill: documents/forms) stammen aus der Aufgabenstellung **[A]** – die Server-
  Konfiguration ist nicht einsehbar.

### 1.4 Endpunkte [V]

| Route (normalisiert) | Dienst | Upstream |
|---|---|---|
| `GET /api/v1/health` | – (unauthentifiziert) | keiner |
| `GET /api/v1/capabilities` | – | keiner |
| `POST /api/v1/documents/extract-text` | documents | Stirling text (+ OCR + text) |
| `POST /api/v1/documents/convert-to-pdf` | documents | Stirling convert |
| `POST /api/v1/documents/analyze` | ai | Ollama (1..n Calls) |
| `POST /api/v1/documents/warmup` | ai | Ollama (leerer Prompt) |
| `POST /api/v1/documents/forms/inspect` | forms | in-process (+ Stirling OCR) |
| `POST /api/v1/documents/forms/fill` | forms | in-process |
| `GET/POST /api/v1/lab/...` | prompt_lab | Dateisystem, `/lab/analyze` Ollama |
| `POST /api/v1/assistant/query`, `/assistant/warmup` | assistant | Ollama |

### 1.5 Bereits vorhandene Monitoring-Möglichkeiten [V]

| Quelle | Inhalt | Bewertung |
|---|---|---|
| `GET /api/v1/health` | `{ok, service, version}`; kontaktiert keine Upstreams. | Liveness, keine Readiness. |
| Docker `HEALTHCHECK` | ruft `/api/v1/health` alle 30 s. | Nur Prozess-Lebendigkeit. |
| Access-Log (Middleware, `main.py`) | `request_id client_id method path status duration_ms` auf stdout. | `path` ist der **rohe** Pfad (nicht normalisiert). Bei unbehandelten Exceptions wird die Zeile nicht geschrieben (Exception verlässt `call_next`). |
| `extract_text`-Log | `input_bytes`, Textlängen, `ocr_attempted`, `extraction_method`, `duration_ms`. | Keine Trennung Stirling-Text vs. OCR-Dauer. |
| `convert_to_pdf`-Log | `source_format`, Bytes in/out, `status`, `duration_ms`. | Gut verwendbar. |
| `forms_inspect`-/`forms_fill`-Log | Seiten, Feldanzahlen, OCR-Status/-Seiten, Flags, `status`, `duration_ms`. | Gut verwendbar, keine Phasenzeiten. |
| `analyze`-Log (`docpipe.ai`) | Modus, Profil, Modell, Prompt-Version, Zeichenzahlen, Strategie, Normalisierungs-/Optimierungszeit, `ollama_duration_ms`, Repair, `prompt_eval_count`, `eval_count`, Gesamtzeit. | Sehr gute Basis. |
| `assistant`-Log | Modus, Regel, Modell, Zeichenzahlen, `ollama_duration_ms`, Gesamtzeit. | Ohne Token-Felder. |
| Warm-up-Log | `ollama_duration_ms` + Ollama-Metriken. | – |
| `OllamaClient` `timing`-Dict | pro Call `duration_ms`, `load_ms`, `prompt_eval_ms`, `eval_ms`, `prompt_eval_count`, `eval_count` (primary/repair getrennt). | **Alle Ollama-Response-Metriken sind bereits erfasst** – nur nicht persistiert. |

Es gibt **keine** Metrik-Endpunkte, keine Zähler, keine Persistenz von Messwerten.
Logs gehen ausschließlich auf stdout (`logging.basicConfig`, Format `%(message)s`).

### 1.6 Ollama – tatsächlich verfügbare Metriken

Geprüft gegen `api/types.go` im Ollama-Repository (Release v0.40.18) **[V]**;
die in Produktion laufende Ollama-Version ist unbekannt **[A]** – ältere Versionen
liefern einzelne Felder (z. B. `context_length` in `/api/ps`) nicht. Jedes Feld
wird daher optional gelesen.

| Endpunkt | Felder | Kosten |
|---|---|---|
| `GET /api/version` | `version` | trivial, keine Inferenz |
| `GET /api/tags` | je Modell `name`, `model`, `size`, `digest`, `modified_at`, `details.{format,family,parameter_size,quantization_level}`, optional `capabilities` | trivial |
| `GET /api/ps` | je **geladenem** Modell `name`, `size`, `size_vram`, `expires_at` (→ Keep-alive-Restzeit), `context_length`, `details.quantization_level` | trivial |
| `/api/generate`-Response (bereits genutzt) | `total_duration`, `load_duration`, `prompt_eval_duration`, `eval_duration` (ns), `prompt_eval_count`, `eval_count`, neuere Versionen `prompt_eval_cached_count` | **keine Zusatzlast** – fällt bei echten Requests ohnehin an |

Abgeleitet (von DocPipe berechnet, so zu kennzeichnen):
Tokens/s Generierung = `eval_count / eval_duration`, Prompt-Tokens/s =
`prompt_eval_count / prompt_eval_duration`, Modellladezeit = `load_duration`
(> ~100 ms ≙ Kaltstart; Schwelle konfigurierbar, als Heuristik gekennzeichnet).

**Nicht verfügbar** ohne zusätzliche Rechte: Ollama-Prozess-RAM/CPU (fremder
PID-Namespace / Host-Dienst), GPU-Auslastung/Temperatur, Ollama-eigene
Request-Zähler (Ollama hat keine Metrik-API), Anfragen anderer Anwendungen an
Ollama. `size_vram` ist die einzige VRAM-Angabe und stammt **von Ollama selbst**.
OOM-Situationen sind nur indirekt erkennbar (HTTP 500 von `/api/generate`; die
Fehlermeldung wird bewusst nicht ausgewertet) – als „technischer Fehler“, nicht
als „Speichermangel“ ausweisen.

Unterscheidung in der UI (verbindlich): *Von Ollama gemeldet* (`/api/*`,
Response-Metadaten) · *Von DocPipe gemessen* (Wandzeit, Fehlercodes, Anzahl) ·
*Betriebssystem* · *Nicht verfügbar*. DocPipe-KI-Anfragen werden immer als
„über DocPipe beobachtet“ beschriftet, nie als Gesamt-Ollama-Last.

### 1.7 Stirling PDF v3.2.2 – tatsächlich verfügbare Metriken

Geprüft im Quellcode-Tag `v3.2.2` (Commit `684608d`) **[V]**:

| Endpunkt | Inhalt | Zugriff |
|---|---|---|
| `GET /api/v1/info/status` | `{"status":"UP","version":"<x>"}` | öffentlich (in `RequestUriUtils` explizit ohne Auth) |
| `GET /api/v1/info/health` | identisch zu `/status` | wie oben |
| `GET /api/v1/info/uptime` | formatierte Laufzeit | `/api/v1/info*` als statische Ressource behandelt |
| `GET /api/v1/info/load*`, `/requests*` | Seiten-/Request-Zähler | **nur wenn `metrics.enabled`** in Stirling, sonst 403 |
| `/actuator/health` | Spring-Health | als öffentlich gelistet; ob Actuator exponiert ist, hängt von der Stirling-Konfiguration ab **[A]** |

Empfehlung **[E]**: ausschließlich `GET /api/v1/info/status` (Status + Version)
zyklisch abfragen. Keine PDF-Operation als Health-Check. Die Stirling-eigenen
Zähler werden nicht verwendet (Konfigurationsabhängigkeit, zählen auch fremde
Nutzer). Stirling-Last wird **aus DocPipe-Sicht** gemessen: Anzahl, Dauer,
Fehlercode je Operation (`extract_text`, `ocr`, `convert_office`, `convert_image`).
Container-CPU/RAM von Stirling: **nicht verfügbar** (nur über Docker-Socket).

### 1.8 Log-Quellen

| Quelle | Ort | Sicher übernehmbar? |
|---|---|---|
| DocPipe stdout | Container-Log | **Nein, nicht roh**: Tracebacks (`logger.exception`) können Exception-Texte mit Dokument-/Bibliotheksinhalten enthalten; `path` ist roh. |
| DocPipe strukturierte Ereignisse (neu, M2) | Monitoring-DB | **Ja** – ausschließlich Enum-Werte und Zahlen. |
| Ollama | Host-Dienst (journald) [A] | **Nein** – aus dem Container nicht ohne Host-Mounts erreichbar; enthält ggf. Prompt-Fragmente. |
| Stirling | Container-Log / Volume [A] | **Nein** – nur über Docker-Socket; kann Dateinamen enthalten. |

Entscheidung **[E]**: Der Log-Viewer zeigt in M6 **nur strukturierte
DocPipe-Ereignisse** aus der eigenen Monitoring-DB. Native Logs von Ollama und
Stirling werden nicht eingebunden; die UI dokumentiert diese Einschränkung.

> **Überholt (M7):** M6 entfällt, es gibt keinen Log-Viewer. Die strukturierten
> Ereignisse und das Security-Audit bleiben erhalten und erscheinen weiterhin in den
> jeweiligen Bereichen, siehe §17.1.

### 1.9 Docker-Einschränkungen [V/A]

Ein unprivilegierter Container (UID 1000, kein Docker-Socket) sieht:

| Messwert | Quelle im Container | Was es wirklich ist |
|---|---|---|
| CPU gesamt, Load | `/proc/stat`, `/proc/loadavg` (nicht namespaced) | **Host**-Werte |
| RAM gesamt/frei | `/proc/meminfo` (nicht namespaced) | **Host**-Werte, ignoriert Container-Limits |
| Container-RAM/CPU | cgroup v2: `/sys/fs/cgroup/memory.current`, `memory.max`, `cpu.stat`, `cpu.max` | **Container** (DocPipe) – sofern cgroup v2 gemountet [A] |
| Prozesse | `/proc/<pid>` | nur DocPipe-Prozesse (eigener PID-Namespace) |
| Netzwerk | `/proc/net/dev` | bei Host-Networking: **Host-Interfaces** [A]; sonst Container-veth |
| Disk | `statvfs` auf `/` und Datenvolume | Dateisystem des jeweiligen Mounts (Overlay → Docker-Root des Hosts) |
| GPU | NVML benötigt Device + NVIDIA-Toolkit | **nicht verfügbar**; Staging hat keine GPU für Ollama |
| Stirling/Ollama-Ressourcen | – | **nicht verfügbar** |

Konsequenz: Jede Systemkachel trägt ein Herkunftslabel („Host via /proc“,
„DocPipe-Container via cgroup“, „DocPipe-Prozess“, „nicht verfügbar“).
Ausgeschlossen sind: privilegierte Container, Docker-Socket, `pid: host`,
Host-`/proc`-Mounts, zusätzliche Capabilities.

---

## 2. Zielarchitektur [E]

### 2.1 Grundprinzipien

1. **Standardmäßig aus.** Ohne `monitoring.enabled: true` werden weder Routen
   registriert noch Middleware/Threads gestartet noch Dateien angelegt. Das
   Verhalten aller bestehenden Endpunkte ist bytegleich zu heute.
2. **Fail-open für DocPipe, fail-closed für das Dashboard.** Jeder Fehler im
   Monitoring wird geschluckt und gezählt; kein Kunden-Request scheitert oder
   wird langsamer, weil das Monitoring defekt ist. Das Dashboard selbst
   verweigert bei Auth-/Konfigurationsfehlern den Zugriff.
3. **Read-only.** Keine Route ändert DocPipe-, Ollama-, Stirling- oder
   Client-Zustand. Einzige Schreibzugriffe: eigene Monitoring-DB.
4. **Nur gemessene Werte.** Fehlender Wert = `null` + Grund
   (`unavailable`/`not_applicable`/`stale`), nie `0` als Platzhalter.
5. **Keine Inhalte.** Telemetrie besteht nur aus Enums, IDs aus der
   Konfiguration, Zahlen und Zeitstempeln (Whitelist-Prinzip, siehe §5).
6. **Keine neue Infrastruktur.** Kein Prometheus/Grafana/Redis; integriert in
   FastAPI, SQLite-Datei in einem Volume.

### 2.2 Komponenten

```text
system/monitoring/               (neues Paket)
  settings.py      MonitoringSettings (geparst in config.py, Default disabled)
  context.py       RequestMetrics + contextvar (pro Request, mutable)
  middleware.py    reine ASGI-Middleware: Start/Ende, Route-Template, Bytes, Status
  recorder.py      Thread-sichere, begrenzte In-Memory-Puffer + Live-Zustand
  store.py         SQLite (WAL), Schema-Version, Batch-Writes, Retention, Rollups
  collector.py     1 Hintergrund-Thread: Flush, System-Samples, Probes, Cleanup
  system_metrics.py  /proc, cgroup v2, statvfs (psutil optional)
  probes.py        Ollama /api/version|tags|ps, Stirling /api/v1/info/status
  sanitize.py      Whitelists, Längen-/Zeichenbegrenzung, Enum-Mapping
  security/        auth.py (Argon2id, TOTP), sessions.py, csrf.py, ratelimit.py,
                   audit.py, access.py (IP-Allowlist, Trusted Proxies)
  web.py           Browser-Routen /monitor/*
  api.py           JSON-API /monitor/api/v1/*
  templates/       Jinja2
  static/          eigene JS/CSS + vendor/ (Bootstrap, Bootstrap Icons, Chart.js)
system/tools/monitor_user.py   CLI: Passwort-Hash + TOTP-Secret erzeugen
tests/test_monitoring_*.py
```

Passt zur bestehenden Struktur (`system/ai`, `system/forms`, `system/assistant`
sind gleichartige Pakete; `system/tools/` existiert; Dokumentation unter
`docs/architecture/`). `Dockerfile` kopiert `system/` vollständig – Templates und
statische Dateien sind ohne Dockerfile-Änderung im Image.

Neue Abhängigkeiten (minimal): `jinja2`, `argon2-cffi`; optional `psutil`
(Alternative: direkte `/proc`-/cgroup-Reader, ~150 Zeilen, keine native
Abhängigkeit). TOTP (RFC 6238) wird mit `hmac`/`hashlib` umgesetzt (~30 Zeilen,
gegen RFC-Testvektoren getestet) – keine weitere Bibliothek. Frontend-Bibliotheken
werden mit Lizenzdateien **lokal** ausgeliefert, kein CDN.

### 2.3 Datenfluss

```text
Request ──► MonitoringMiddleware (ASGI) ──► bestehende HTTP-Middleware ──► Route (Threadpool)
              │ start: request_id, t0, active+=1           │
              │                                             ├─ auth: client_id → RequestMetrics
              │                                             ├─ Stirling-Call: step(stirling.ocr, ms, code)
              │                                             ├─ Ollama: timing-Dict → step(ai.*, tokens)
              │                                             └─ forms: step(forms.inspect.layout, ms)
              │ end (finally): route template, status, error_code, bytes, dauer
              ▼
        Recorder (Lock, deque maxlen=N)  ── Drop + Zähler bei Überlauf
              ▼  alle 5 s (Collector-Thread)
        SQLite WAL: request_events, request_steps, live_snapshot
              ▼  jede Minute / Stunde
        Rollups (Minute, Stunde) inkl. Latenz-Histogramm → Retention-Cleanup

Browser ──► /monitor (Session-Cookie) ──► /monitor/api/v1/* (Polling 5–15 s) ──► SQLite (read)
```

**Request-Kontext.** Die ASGI-Middleware legt ein `RequestMetrics`-Objekt in
eine `contextvar`. Starlette führt sync-Routen über `anyio.to_thread.run_sync`
aus, das den Kontext kopiert – das *Objekt* ist dasselbe, Änderungen aus dem
Threadpool sind in der Middleware sichtbar. (In M2 per Test abzusichern.)

**Instrumentierungspunkte (minimal-invasiv):**

| Punkt | Änderung |
|---|---|
| `system/auth.py` `get_authenticated_client` | `client_id` zusätzlich in `RequestMetrics` (nur bei erfolgreicher Auth). |
| `system/services/stirling.py` `_post` | Dauer + Ergebniscode pro Operation (Pfad → Step-Name). |
| Routen analyze/assistant/warmup/lab | bestehendes `timing`/`observed`-Dict an `RequestMetrics` übergeben. |
| `system/services/ollama.py` | **keine Änderung** (siehe Risiko R1, lokaler `think`-Patch). |
| `system/forms/inspect.py`, `fill.py` | optionale Phasen-Timer (Parse, Layout, Detect, OCR, Render). |
| `system/main.py` | Middleware/Router nur bei `monitoring.enabled`; Collector im `lifespan`. |

**Mehrere Worker.** Heute läuft ein Uvicorn-Prozess **[V]**; die Produktion kann
`--workers` setzen **[A]**. Das Design ist mehrprozessfähig:
jeder Prozess hat eine `instance_id` (uuid beim Start), puffert lokal und
schreibt per Batch in dieselbe SQLite-Datei (WAL, `busy_timeout`).
Live-Daten (aktive Requests) schreibt jeder Prozess alle 2–5 s als Snapshot
in `live_snapshot`; das Dashboard liest die Summe aller nicht veralteten
Snapshots. Periodische Probes (Ollama/Stirling) werden über eine Lease-Zeile in
SQLite auf **einen** Prozess beschränkt.

### 2.4 Live-Aktualisierung: Vergleich

| Kriterium | REST-Polling | SSE | WebSocket |
|---|---|---|---|
| Aufwand | gering, normale JSON-Routen | mittel (Streaming, Reconnect, Heartbeat) | hoch (Protokoll, eigener Auth-Pfad) |
| Sicherheit | gleiche Session-/CSRF-Prüfung wie jede GET-Route | Cookie-Auth ok, Session-Ablauf während offener Verbindung extra behandeln | eigener Handshake, Origin-Prüfung, CSWSH-Risiko |
| Ressourcen | kurzlebig, keine Dauerverbindungen | 1 offene Verbindung je Tab, Proxy-Timeouts/Buffering | wie SSE, plus Proxy-Upgrade-Konfiguration |
| Passung DocPipe | ideal (sync-Routen, Threadpool) | Streaming kollidiert mit Threadpool-Modell | nicht nötig (kein Rückkanal) |

Empfehlung **[E]**: **REST-Polling**, 5 s (Live Activity) bis 15 s (Übersicht),
pausiert bei `document.visibilityState = hidden` und durch den Nutzer.
Begrenzung: max. Sessions pro Benutzer (z. B. 3), serverseitiges Mindestintervall
pro Session (Requests < 2 s → 429). SSE kann später für den Log-Live-Modus
nachgerüstet werden, falls Polling nicht reicht.

---

## 3. Sicherheitskonzept [E]

### 3.1 Aktivierung und Erreichbarkeit

- `monitoring.enabled: false` (Default) → `/monitor*` antwortet wie jede unbekannte
  Route (404, bestehendes Fehlerformat). Kein Hinweis auf die Existenz.
- `monitoring.dashboard.enabled` separat schaltbar: Erfassung kann ohne UI laufen.
- Start verweigern (`ConfigError`), wenn `enabled: true` aber: kein Benutzer, kein
  `session_secret` (≥ 32 Byte), `require_https` aktiv ohne Trusted-Proxy-Konfiguration,
  oder ein Benutzer ohne TOTP (sofern `require_mfa: true`, Default).
- Empfohlene Erreichbarkeit (Defense in Depth): `/monitor` am Reverse Proxy nur
  für VPN/Allowlist freigeben **und** zusätzlich die App-interne IP-Allowlist.

### 3.2 Monitoring-Benutzer

- Definiert in der Server-Konfiguration (eigener Abschnitt `monitoring.users`,
  optional ausgelagerte Datei mit Rechten 0600), **nicht** über das Dashboard.
  Keine Benutzerverwaltung in der UI.
- Passwort: Argon2id (`argon2-cffi`, Parameter ≥ OWASP-Empfehlung, z. B.
  m=64 MiB, t=3, p=1), erzeugt mit `python -m system.tools.monitor_user`.
- 2FA: TOTP (RFC 6238, SHA-1, 30 s, 6 Stellen, ±1 Zeitfenster), Wiederverwendung
  eines Codes innerhalb seines Fensters wird abgelehnt (letzter genutzter Zähler
  pro Benutzer in der DB).
- Kunden-API-Keys: Der `Authorization`-Header wird auf `/monitor*` **ignoriert**
  und nie ausgewertet; es gibt keinen Codepfad von `ClientConfig` zu einer
  Monitoring-Berechtigung. Getestet mit jedem konfigurierten Client-Key.

### 3.3 Sessions, Cookies, CSRF

- Session-ID: 256 Bit aus `secrets`; in der DB nur `SHA-256(id)` gespeichert.
- Cookie `__Host-dp_mon`: `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`, ohne `Domain`.
- Idle-Timeout 15 min, absolut 8 h (konfigurierbar); Rotation der Session-ID beim
  Login; Logout invalidiert serverseitig.
- **Polling verlängert keine unbeaufsichtigte Session:** Die Regel ist serverseitig
  und hängt nicht von Client-Angaben ab. Nur Seitenaufrufe (HTML-Navigation) zählen als
  Aktivität. Kein Aufruf unter `/monitor/api/*` verlängert die Idle-Zeit, unabhängig von
  Headern. Ein offener, unbeobachteter Tab läuft also nach dem Idle-Timeout (Default
  15 min) ab. Das absolute Limit (8 h) gilt immer (umgesetzt in M3, §14.5).
- CSRF: Einzige zustandsändernde Routen sind `POST /monitor/login` und
  `POST /monitor/logout` → Synchronizer-Token + Prüfung von `Origin`/`Sec-Fetch-Site`.
  Alle API-Routen sind `GET` und seiteneffektfrei; andere Methoden → 405.

### 3.4 Login-Schutz und Audit

- Rate-Limit in SQLite (mehrprozessfähig): pro IP und pro Benutzername, z. B.
  5 Fehlversuche / 15 min → exponentielles Backoff, danach temporäre Sperre.
  Einheitliche Fehlermeldung, konstante Laufzeit (Argon2-Verify auch für
  unbekannte Benutzer gegen Dummy-Hash).
- Audit-Log (`monitor_audit`): Login ok/fehlgeschlagen/gesperrt, TOTP-Fehler, Logout,
  Session-Ablauf, Zugriff verweigert (IP). Felder: Zeit, Ereignis, Benutzername
  (nur wenn existent, sonst `unknown`), Client-IP, gekürzter User-Agent-Hash.
  Nie Passwörter, TOTP-Codes, Cookies. Aufbewahrung Default 90 Tage.
  Client-IPs sind personenbezogene Daten → nur im Audit-Log, nicht bei API-Requests.

### 3.5 Browser-Härtung

- CSP: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;
  connect-src 'self'; font-src 'self'; frame-ancestors 'none'; form-action 'self';
  base-uri 'none'; object-src 'none'`. Kein Inline-JS/-CSS; Daten nur per JSON-Fetch.
- Weitere Header: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store`, `Permissions-Policy` restriktiv,
  `Strict-Transport-Security` (nur wenn HTTPS bestätigt), `Cross-Origin-Opener-Policy: same-origin`.
- XSS: Jinja2-Autoescape; im JS ausschließlich `textContent`, nie `innerHTML` mit Daten.

### 3.6 HTTPS, Reverse Proxy, IP-Zugriff

- **Trusted-Proxy-Verarbeitung (verbindlich, genau eine Auswertungsstelle):**
  1. Uvicorn darf Forwarded-Header **nicht** selbst umschreiben. Monitoring wird nur
     unterstützt, wenn Uvicorn mit `--no-proxy-headers` (oder leerem
     `--forwarded-allow-ips`) läuft. Andernfalls ist `scope["client"]` bereits durch
     `X-Forwarded-For` verändert. M3 prüft das beim Start, soweit erkennbar, und
     dokumentiert die Deploy-Anforderung. Bei Widerspruch wird der Zugang verweigert.
  2. Ausgangspunkt ist die TCP-Gegenstelle `scope["client"][0]`.
  3. Liegt sie **nicht** in `monitoring.trusted_proxies` (CIDR-Liste, Default leer),
     ist sie die Client-IP. Alle `Forwarded`- und `X-Forwarded-*`-Header werden dann ignoriert.
  4. Liegt sie in der Liste, wird **genau ein** Header ausgewertet:
     `X-Forwarded-For`. Mehrere Header-Zeilen werden zusammengeführt. Die Liste wird
     von rechts nach links gelesen, vertrauenswürdige Proxy-Adressen werden
     übersprungen, der erste nicht vertrauenswürdige Eintrag ist die Client-IP.
     Syntaktisch ungültige Einträge oder eine Liste nur aus Proxys führen zu
     **Zugriff verweigert** (fail closed). `Forwarded` (RFC 7239) und `X-Real-IP`
     werden nie ausgewertet, damit es keine konkurrierenden Quellen gibt.
  5. `X-Forwarded-Proto` zählt nur unter Bedingung 4 und nur der rechteste Wert.
     `https` ist Voraussetzung für Login und Session (`require_https`).
- **Fail closed (M3):** Monitoring-Authentifizierung, Session-, Rate-Limit- und
  Allowlist-Prüfungen verweigern den Zugriff, sobald die nötigen Sicherheitsdaten
  fehlen oder nicht lesbar sind (Benutzerdatei, Session-Store, Rate-Limit-Zähler,
  Konfiguration). Der Monitoring-Store darf für die *Messdaten* fail-open sein
  (kein Einfluss auf die API), für die *Sicherheitsdaten* nie.
- `require_https: true` (Default): Login und Session nur, wenn die Verbindung über
  einen vertrauenswürdigen Proxy als HTTPS gemeldet ist.
- `allowed_networks` (optional): CIDR-Allowlist, gilt für **alle** `/monitor*`-Routen
  inkl. statischer Dateien und Login.

### 3.7 Bedrohungsanalyse (STRIDE, Auszug)

| # | Bedrohung | Maßnahme |
|---|---|---|
| T1 | Kunde nutzt API-Key für Monitoring (Elevation) | getrennte Auth, Header ignoriert, Test je Client-Key |
| T2 | Brute-Force/Credential Stuffing | Argon2id, TOTP, Rate-Limit pro IP+User, Sperre, Audit |
| T3 | Session-Diebstahl (XSS, Netz) | HttpOnly/Secure/`__Host-`, CSP ohne Inline, HTTPS-Pflicht, kurze Timeouts |
| T4 | CSRF | nur 2 POST-Routen mit Token + Origin; Rest GET ohne Seiteneffekt |
| T5 | IP-Spoofing über `X-Forwarded-For` | nur Trusted-Proxy-CIDRs, rechtester Eintrag |
| T6 | Informationsabfluss (Dokumente, Prompts, Keys) über Telemetrie | Whitelist-Schema, keine Freitexte, Secret-Scanner-Tests |
| T7 | Log-Injection (Zeilenumbrüche, Steuerzeichen) | nur Enums/Route-Templates; Längen-/Zeichenfilter; JSON-API |
| T8 | Cross-Client-Einsicht | nur Betreiber haben Zugriff; keine Client-Sicht im Dashboard |
| T9 | DoS über Dashboard / hochkardinale Pfade (Scanner) | unbekannte Routen → `<unmatched>`; Polling-Limit; begrenzte Puffer; Abfrage-Limits |
| T10 | Monitoring-Defekt legt DocPipe lahm | try/except um jede Erfassung, bounded queues, Tests mit defektem Store |
| T11 | Monitoring als Steuerkanal | keine schreibenden Endpunkte; Test: alle Routen GET bzw. Login/Logout |
| T12 | SSRF über Probes | Probe-URLs nur aus bestehender Server-Config (`ollama.base_url`, `stirling.base_url`), `follow_redirects=False`, kurze Timeouts |
| T13 | Lieferkette Frontend | Bibliotheken lokal, Versionen + SHA-256 in `vendor/README`, Lizenzdateien |
| T14 | Repudiation von Monitoring-Zugriffen | Audit-Log, nicht über UI löschbar |

### 3.8 Mandanten-/Client-Isolation

- `client_id` stammt ausschließlich aus `get_authenticated_client` (Konfiguration).
  Unauthentifizierte/abgelehnte Requests werden als `client_id = null` gezählt
  (eigene Kategorie „nicht authentifiziert“), nie mit Body-/Header-Werten.
- Mandanten werden nicht ausgewertet, solange die Identität keinen Mandanten kennt.

---

## 4. Datenmodell und Speicher [E]

### 4.1 Technologie

SQLite (stdlib `sqlite3`), WAL, `synchronous=NORMAL`, `busy_timeout=2000`,
eine Datei in eigenem Volume (z. B. `/app/monitoring-data/monitor.db`, nicht im
Prompt-Volume). Schema-Version per `PRAGMA user_version`, Migrationen
vorwärts-only beim Start. Geeignet, weil: ein Host, wenige Writer, Batch-Writes,
Lesezugriffe nur durch den Betreiber. Bei späterem Bedarf (mehrere Hosts) ist der
Store austauschbar (Interface in `store.py`).

### 4.2 Tabellen

| Tabelle | Inhalt | Retention (Default) |
|---|---|---|
| `request_events` | `request_id`, `ts_start`, `duration_ms`, `instance_id`, `client_id?`, `route` (Template), `method`, `service`, `status_code`, `error_code?`, `bytes_in`, `bytes_out`, `documents?`, `pages?`, `ai_provider?`, `ai_model?`, `ai_mode?`, `tokens_in?`, `tokens_out?` | 7 Tage |
| `request_steps` | `request_id`, `step` (Enum), `seq`, `duration_ms`, `outcome` (Enum), optionale Zahlen (`load_ms`, `prompt_eval_ms`, `eval_ms`, Tokens) | 7 Tage |
| `rollup_minute` | Bucket × (client, route, service, status_class, error_code, model): count, errors, sum/min/max ms, Histogramm (fixe log-Buckets), Bytes, Tokens | 14 Tage |
| `rollup_hour` | wie oben, stündlich | 400 Tage |
| `system_samples` | ts, Herkunft (`host`/`container`/`process`), CPU %, RAM, Disk, Netz-Δ, Threadpool-Belegung | 48 h roh, danach Minuten-Rollup 30 Tage |
| `service_probes` | ts, Dienst, erreichbar, Latenz, Version, Status-Enum | 30 Tage |
| `ollama_models` | Snapshot `/api/tags` + `/api/ps` (Name, Größe, VRAM, Quantisierung, `expires_at`) | 7 Tage |
| `events` | strukturierte technische Ereignisse (Severity, Code, Dienst, `request_id?`, `client_id?`) | 30 Tage, max. Zeilen |
| `live_snapshot` | je `instance_id`: aktive Requests (ID, Route, Client, Start), Zeitstempel | überschrieben |
| `monitor_sessions`, `monitor_login_attempts`, `monitor_audit`, `monitor_totp_state`, `monitor_lease` | Sicherheit/Koordination | Sessions bis Ablauf, Audit 90 Tage |

Alle Retentions konfigurierbar; Cleanup stündlich in kleinen Batches
(`DELETE … LIMIT`), `PRAGMA incremental_vacuum`; Obergrenze für die DB-Größe
(Default 512 MiB) – bei Überschreitung zuerst Rohdaten früher löschen,
Ereignis `monitoring_storage_pressure`.

### 4.3 Kardinalität und Genauigkeit

- Alle Dimensionen sind begrenzt: `client_id` (Config), `route` (registrierte
  FastAPI-Routen, sonst `<unmatched>`), `error_code` (`STATUS_BY_CODE` + `unknown`),
  `model`/`mode` (Config/Registry).
- Perzentile: exakt aus `request_events` für Zeiträume innerhalb der Rohdaten-Retention;
  darüber aus Histogramm-Rollups (in der UI als „≈ aus Histogramm“ gekennzeichnet).
- `documents`: 1 je erfolgreichem Dokument-Endpunkt (extract-text, convert, forms).
  `pages`: nur wo DocPipe die Seitenzahl ohnehin kennt (forms inspect/fill);
  extract-text/convert → `null` („nicht ermittelt“). Keine Zusatzverarbeitung nur für
  Statistik.
- Bytes: gezählt in der ASGI-Middleware aus `receive`/`send` (nicht aus dem
  Client-Header `Content-Length`).

### 4.4 Ressourcen-Budget

- In-Memory-Puffer: `deque(maxlen=10 000)` Events (≈ < 5 MiB); bei Überlauf wird
  verworfen und `monitoring_dropped_events` gezählt – nie blockiert.
- Ein Collector-Thread pro Prozess (Daemon, im `lifespan` gestartet/gestoppt,
  Stop-Event, Join mit Timeout). Keine weiteren Threads, keine Subprozesse.
- System-Samples alle 15 s, Probes alle 30 s, Flush alle 5 s – konfigurierbar mit
  Mindestwerten.
- Ziel: < 1 ms Zusatzlatenz p99 pro Request, < 1 % CPU im Leerlauf, < 30 MiB RSS-Zuwachs.
  Wird in M7 gemessen.

---

## 5. Sanitizing und Datenschutz [E]

Whitelist statt Blacklist: In die Monitoring-DB gelangen nur Felder aus dem
festen Schema (§4.2). Zusätzlich:

- **Nie** `DocPipeError.message` speichern – einige Messages enthalten
  Request-Eingaben (z. B. `unknown_mode` mit dem übergebenen Modusnamen). Nur `code`.
- Nie Exception-Texte, Tracebacks, Upstream-Bodies, Header, Query-Strings,
  Dateinamen, Feldnamen/-werte, Passwörter, Prompts, Antworten, Kontextdaten.
- Route = FastAPI-Template (`/api/v1/lab/prompts/{mode}`), nie der rohe Pfad.
- String-Dimensionen: Regex `^[A-Za-z0-9_.:\-/{}]{1,128}$`, sonst `invalid`.
- Tests: Fixture mit „Kanarienvögeln“ (fiktiver API-Key, Passwort, Dateiname,
  Dokumenttext, Prompt) durch alle Endpunkte schicken, danach DB-Dump und alle
  API-Antworten auf Vorkommen prüfen.
- Document Source Tracking (#4): Seiten-/Fundstellen-Referenzen werden nicht
  telemetriert; das Monitoring ändert keine Response-Schemas.

---

## 6. API-Konzept [E]

Alle unter `/monitor/api/v1/`, nur `GET`, Session-Pflicht, JSON,
`Cache-Control: no-store`. Zeitraum über `range=live|1h|24h|7d|30d` oder
`from`/`to` (ISO 8601, max. 400 Tage, Server begrenzt Auflösung).

| Endpunkt | Inhalt |
|---|---|
| `GET /overview` | Gesamtstatus, Version/Build, Uptime, Zähler, Quoten, Ø/Median/p95/p99, Systemkurz, Dienststatus, Top-Fehlercodes, Zeitreihe |
| `GET /activity?limit&cursor` | aktive + letzte Requests (max. 200 pro Seite) |
| `GET /clients?group_by=client\|service\|route\|model` | Nutzung, Anteile, Fehlerquote, Tokens |
| `GET /performance?route&client` | Step-Aufschlüsselung (Ø/p95 je Step), Zeitreihe |
| `GET /server` | Samples mit Herkunftslabel, `unavailable`-Gründe |
| `GET /ollama` | Probe, Version, Modelle (installiert/geladen), DocPipe-KI-Statistik, Tokens/s |
| `GET /stirling` | Probe, Version, Operationen, Fehler, Timeouts |
| ~~`GET /events?severity&service&client&q&from&to&cursor`~~ | entfällt mit M6 (§17.1) |
| `GET /security` | eigene Audit-Einträge (Login-Historie), aktive eigene Sessions |
| `GET /meta` | Feature-Flags, welche Bereiche implementiert sind, Collector-Status |

Antwortformat je Messwert: `{"value": …, "source": "ollama|docpipe|host|container|process",
"status": "ok|stale|unavailable|not_applicable", "measured_at": "…"}`.
`stale` wenn älter als 3 × Erfassungsintervall.

Browser-Routen: `GET /monitor` (Shell), `GET|POST /monitor/login`,
`POST /monitor/logout`, `GET /monitor/static/*` (nur bei `enabled`, IP-Allowlist gilt).
Requests auf `/monitor*` fließen **nicht** in die Kundenstatistik ein.

---

## 7. UI-Konzept [E]

- Server-gerenderte Shell (Jinja2) + Vanilla-JS-Module, Bootstrap 5.3 (CSS/JS lokal),
  Bootstrap Icons (lokal, SVG-Sprite), Chart.js 4 (lokal). Kein Build-Schritt,
  keine Node-Toolchain im Image.
- Theme: `data-bs-theme` light/dark, folgt `prefers-color-scheme`, Umschalter
  (gespeichert in `localStorage`). Sprache: DE/EN über JSON-Wörterbücher,
  `Accept-Language` als Default, Umschalter.
- Layout: Sidebar-Navigation (≥ lg), Offcanvas auf Tablet/Smartphone; Kacheln im
  Grid (1/2/4 Spalten); Tabellen horizontal scrollbar in eigenem Container.
- Kopfzeile: Gesamtstatus-Badge, Zeitraumfilter, „Zuletzt aktualisiert hh:mm:ss“,
  Live/Pause-Schalter, Abmelden.
- Statussprache einheitlich: OK (grün), Eingeschränkt (gelb), Ausfall (rot),
  Veraltet (grau, gestrichelt), Nicht verfügbar (grau, Icon + Begründungs-Tooltip).
- Herkunfts-Chip an jeder Kennzahl (Ollama · DocPipe · Host · Container).
- **Nicht implementierte Bereiche** erscheinen in der Navigation deaktiviert mit
  „geplant (Mx)“ und zeigen keine Platzhalterdiagramme. „AI Connectors“ bleibt bis
  #2 deaktiviert.

Bereiche: Overview · Live Activity · Projects & Clients · Performance · Server ·
Ollama · Stirling PDF · AI Connectors (deaktiviert, Issue #2) · Security (Logs entfällt, §17.1)
(eigene Anmeldungen/Sessions, Audit, Konfigurationsstatus read-only).

---

## 8. Erweiterbarkeit für AI Connectors (#2) [E]

`request_events`/`request_steps` enthalten bereits `ai_provider`, `ai_model`,
`tokens_in`, `tokens_out`. In der Migration für #2 kommen nullable Spalten hinzu:
`connector_id`, `billing_mode` (`managed|byok|unknown`), `quota_units`,
`cost_estimated`, `cost_actual`, `cost_currency`, `cost_source`
(`provider_reported|estimated|unknown`). Ollama → `provider = "ollama"`,
`billing_mode = "managed"`, Kosten `null`. BYOK-Credentials werden nie erfasst.
Kein Routing, keine Modellwahl im Monitoring.

---

## 9. Entwicklungsphasen und Abnahmekriterien

Jede Phase: eigener Commit-Satz auf diesem Branch, CI grün, Freigabe vor der
nächsten Phase. Kein Merge in `main` vor M7-Freigabe.

### M1 – Bestandsanalyse und Sicherheitskonzept (dieses Dokument)
Abnahme: Dokument vollständig; verifizierte Fakten, Annahmen und Empfehlungen
getrennt; offene Entscheidungen (§11) beantwortet; Issue #3 aktualisiert.

### M2 – Monitoring Foundation (umgesetzt, siehe §13)
Umfang: `MonitoringSettings` (Default aus), ASGI-Middleware, `RequestMetrics`,
Recorder, SQLite-Store mit Migrationen, Retention, Rollups, Step-Erfassung
(Stirling, Ollama-Timing aus Routen, forms-Phasen), System-Sampler, Probes,
CLI-freie Erfassung ohne UI.
Abnahme:
- Bei `enabled: false`: keine Datei, kein Thread, keine Route; komplette
  bestehende Testsuite unverändert grün; Antworten bytegleich (Snapshot-Tests).
- Parallel-Test (≥ 50 gleichzeitige Requests, mehrere Threads): Zähler exakt.
- Defekter Store (Exception, gesperrte DB, volle Platte simuliert): alle API-Requests
  weiterhin erfolgreich, Fehler gezählt.
- Secret-/Inhalts-Kanarienvogel-Test: 0 Treffer in DB.
- Retention/Rollup-Tests mit Fake-Uhr; Perzentil-Genauigkeit gegen Referenz.
- Overhead-Messung lokal dokumentiert.

### M3 – Secure Monitoring Access
Umfang: Benutzer-Config, CLI `monitor_user`, Argon2id, TOTP, Sessions, CSRF,
Rate-Limit, Audit, IP-Allowlist, Trusted Proxies, Security-Header, leere
Dashboard-Shell.
Abnahme: Tests für jeden Punkt in §3 und §12 (Auth-Bypass-Matrix: jede
`/monitor*`-Route ohne Session, mit abgelaufener Session, mit jedem Kunden-Key,
mit gefälschtem `X-Forwarded-For`, falscher Origin); TOTP gegen RFC-Testvektoren;
Konfigurationsfehler verhindern den Start.

### M4 – Dashboard und Live-Daten
Umfang: Overview, Live Activity, Projects & Clients, Performance, Server,
historische Diagramme, Zeitraumfilter, DE/EN, Light/Dark, responsive.
Abnahme: alle Kennzahlen stammen aus der API (keine Demo-Daten im Code außer
Test-Fixtures); `unavailable`/`stale` sichtbar; Bedienung auf 375 px, 768 px,
1440 px geprüft; CSP-Verstöße = 0 (Browser-Konsole); Polling pausiert im
Hintergrund-Tab.

### M5 – Ollama und Stirling Monitoring
Umfang: Probes und Modellübersicht, KI-Performance (Ladezeit, Prompt-/Eval-Zeit,
Tokens/s, Fehlerarten), Stirling-Operationen.
Abnahme: keine Generierungs- oder PDF-Calls durch das Monitoring (Test mit
Mock, der jeden `POST` als Fehler wertet); Ollama/Stirling offline/langsam/
fehlerhafte JSON → korrekte Anzeige; ältere Ollama-Antworten ohne neue Felder.

### M6 – Live-Logs (entfällt, Entscheidung in M7, §17.1)
Umfang: strukturierte Ereignisse, Viewer mit Suche (nur über Enum-/ID-Felder),
Filter, Pagination, Live/Pause, Retention.
Abnahme: Log-Injection-Tests (CR/LF, ANSI, Unicode-Steuerzeichen, Überlänge);
Kanarienvogel-Test; Abfragelimits; keine nativen Container-Logs.

### M7 – Abschluss und Release-Vorbereitung
Umfang: Volltests, Security-Review, Last-/Overhead-Test, Regressionstest
HubDix/PDF4Fill (gemockt), Dokumentation (README, config.example, Betriebsanleitung),
Pull Request, CI, Deployment-/Rollback-Plan.
Abnahme: CI grün; Overhead-Ziele erfüllt oder begründet; Freigabe des Betreibers.

---

## 10. Tests (Übersicht)

| Bereich | Art |
|---|---|
| HubDix-API, PDF4Fill-API, forms inspect/detect/fill, Ollama-/Stirling-Integration | bestehende Suite + Lauf mit `monitoring.enabled: true` (gleiche Ergebnisse) |
| Monitoring deaktiviert | 404 auf `/monitor*`, keine Seiteneffekte |
| Nicht authentifiziert / Kunden-Keys / Sessions | Auth-Matrix, Cookie-Attribute, Timeouts, Rotation |
| Isolation | `client_id` nur aus Auth; Body-Manipulation ohne Wirkung |
| Geheimnis-/Inhaltsbereinigung | Kanarienvogel-Tests über DB und API |
| Log-Injection | Steuerzeichen, Überlänge, unbekannte Routen |
| Genauigkeit / Parallelität | exakte Zähler, Perzentile, Multi-Thread, simulierte Mehrfach-Instanz |
| Retention / Cleanup | Fake-Uhr, DB-Größenlimit |
| Dienste nicht erreichbar / GPU nicht vorhanden | Mocks (httpx `MockTransport`), fehlende cgroup-Dateien |
| Monitoring-Ausfall | Store/Collector-Exceptions, Thread-Tod, gesperrte DB |
| Ressourcenverbrauch | Mikro-Benchmark Middleware, Speicher bei Pufferüberlauf |

Alle externen Dienste gemockt, keine echten Dokumente, keine Produktions-Credentials.

---

## 11. Risiken und offene Entscheidungen

### Risiken

| # | Risiko | Umgang |
|---|---|---|
| R1 | Lokaler `think: False`-Patch in `ollama.py` geht beim nächsten Deployment verloren oder kollidiert. | Monitoring ändert `ollama.py` nicht. **Empfehlung:** Patch vor dem nächsten Release als konfigurierbare Option (`ollama.think`) ins Repo übernehmen – separate, freizugebende Änderung. |
| R2 | Produktion weicht vom Repo ab (Compose, Worker-Zahl, Proxy). | Annahmen hier markiert; M7 enthält eine Checkliste, die der Betreiber auf dem Server prüft. |
| R3 | SQLite-Locking bei mehreren Workern. | WAL, Batch-Writes, `busy_timeout`, Fehler → Drop statt Block; Last-Test in M7. |
| R4 | Fehlinterpretation von Host- vs. Container-Werten. | Herkunftslabel Pflicht, Tests für fehlende cgroup-Dateien. |
| R5 | Neuer Angriffsvektor durch Login-Oberfläche auf öffentlichem Host. | Default aus, Reverse-Proxy-Sperre + App-Allowlist empfohlen, MFA Pflicht. |
| R6 | Persistentes Volume fehlt → Daten nach Redeploy weg / Start schlägt fehl. | Start mit klarer Fehlermeldung nur wenn `enabled`; Volume in Deploy-Doku. |
| R7 | Ollama-API-Felder variieren je Version. | alle Felder optional, Tests mit alten/neuen Antworten. |
| R8 | Wachsende Komplexität eines bisher zustandslosen Dienstes. | eigenes Paket, Feature-Flag, klare Schnittstellen, kein Einfluss bei `enabled: false`. |

### Entscheidungen des Betreibers (Freigabe M1, 26.11.2026)

| # | Thema | Entscheidung | Umsetzung |
|---|---|---|---|
| 1 | Erreichbarkeit | VPN bzw. IP-Allowlist ist die bevorzugte Betriebsart. Öffentlicher HTTPS-Zugang mit verpflichtender MFA nur, wenn ausdrücklich konfiguriert. Nie öffentlich als Standard. | M3: `monitoring.access.mode: allowlist` (Default) oder `public_mfa`; im Modus `allowlist` ohne Allowlist verweigert der Start. |
| 2 | Benutzerquelle | Separate, geschützte Benutzerdatei mit restriktiven Rechten. | M3: eigene Datei (z. B. `monitoring-users.yaml`), Start verweigert bei Rechten weiter als 0600 oder fremdem Eigentümer. |
| 3 | Servermetriken | Eigene schlanke `/proc`- und cgroup-Reader; GPU optional erweiterbar. | **M2 umgesetzt** (`system_metrics.py`, `GpuProvider`-Schnittstelle, Default „nicht konfiguriert“). Kein psutil. |
| 4 | Retention | Vorgeschlagene Defaults bestätigt; alle Zeiten und Grenzen konfigurierbar. | **M2 umgesetzt** (`monitoring.retention.*`, `monitoring.storage.*`). |
| 5 | Ollama-`think`-Patch | Eigene Entwicklungsaufgabe: abwärtskompatible Konfigurationsoption, **nicht in M2**. Produktivpatch bleibt bis dahin erhalten. | M2 ändert `system/services/ollama.py` nicht (Messung über einen Wrapper, §13.2). |
| 6 | Build-Informationen | Commit und Image-Tag unterstützen; lokale Anpassungen kennzeichnen; keine falsche Zuordnung zu einem unveränderten Commit. | **M2 umgesetzt** (`buildinfo.py`): „clean“ nur bei ausdrücklichem `DOCPIPE_BUILD_LOCAL_CHANGES=false`, sonst „modified“ oder „unknown“. |
| 7 | PDF-Seitenzahl | Nur bereits verfügbare Informationen; keine zusätzliche PDF-Verarbeitung. | **M2 umgesetzt:** Seiten nur bei forms inspect/fill; extract-text/convert bleiben `NULL` („nicht ermittelt“). |
| 8 | UI-Sprache | Browser-Sprache, DE/EN mit manuellem Wechsel, Englisch als Fallback. | M4. |

---

## 12. Deployment- und Rollback-Anforderungen

- Ein Image mit Monitoring-Code ist bei `enabled: false` funktional identisch zum
  Vorgänger → Deployment und Aktivierung sind **getrennte** Schritte.
- Deployment-Voraussetzungen: lokaler `think`-Patch erhalten (R1), Image-Tag
  dokumentiert, Rollback-Image vorhanden.
- Aktivierung (später, eigene Freigabe): Volume für `monitoring-data` (Schreibrecht
  UID 1000), Benutzerdatei, `session_secret`, Trusted Proxies, Reverse-Proxy-Regel
  für `/monitor`, Neustart (kurze Unterbrechung für HubDix/PDF4Fill einplanen).
- Smoke-Test nach Aktivierung: HubDix-/PDF4Fill-Regression wie in
  `docs/staging-deployment.md`, `/monitor` ohne Session → Redirect/401, Login mit MFA,
  keine Kundeninhalte in der DB (Stichprobe).
- Rollback: `monitoring.enabled: false` + Neustart (sofort, ohne Image-Wechsel);
  vollständig: vorheriges Image. Die Monitoring-DB ist reine Telemetrie und kann
  gelöscht werden; keine Migration bestehender Daten.

---

## 13. Umsetzung Phase M2 – Monitoring Foundation

Dieser Abschnitt beschreibt den tatsächlich implementierten Stand. Wo die
Umsetzung vom Konzept (§2–§4) abweicht, steht es hier.

### 13.1 Aufbau

```text
system/monitoring/
  settings.py        `monitoring:`-Abschnitt, Default disabled, Grenzen je Wert
  registry.py        feste Registries: Services, Steps, Fehlercodes, Events, Quellen
  buildinfo.py       Build-Infos aus Env, nie geraten
  context.py         RequestMetrics + contextvar + No-op-Hooks (note_error_code, note_pages, step)
  middleware.py      reine ASGI-Middleware, Pass-through solange app.state.monitoring None ist
  proxies.py         Mess-Wrapper um StirlingClient/OllamaClient (nur wenn aktiviert)
  recorder.py        thread-sichere, begrenzte Puffer, Verlust-/Qualitätszähler pro Minute
  store.py           SQLite: Schema v1, Migrationen, Batch-Writes, Rollups, Retention, Abfragen
  stats.py           Histogramm-Buckets, exakte und Histogramm-Perzentile
  system_metrics.py  /proc- und cgroup-v2-Reader, GPU-Provider-Schnittstelle
  probes.py          GET-only Status-Probes Ollama/Stirling
  monitor.py         Lifecycle, Collector-Thread, Fehlerpolitik
system/tools/monitoring_overhead.py   reproduzierbare Overhead-Messung
```

Integration in bestehenden Code (vollständig):

| Datei | Änderung |
|---|---|
| `system/config.py` | `Settings.monitoring` (Default `MonitoringSettings()`, disabled), Parsing von `monitoring:` |
| `system/main.py` | Monitor und Wrapper nur bei `enabled` im `lifespan`; `MonitoringMiddleware` als äußerste Middleware; Logging-Middleware übernimmt die Request-ID des Monitorings (sonst wie bisher `uuid4`); Fehler-Handler melden den Fehlercode an den Request-Kontext |
| `system/routes/forms.py` | `step("forms.inspect")` / `step("forms.fill")` und die bereits berechnete Seitenzahl |
| `config.example.yaml`, `.gitignore` | dokumentierter Abschnitt, `monitoring-data/` |

**Nicht geändert:** `system/services/ollama.py`, `system/services/stirling.py`,
`system/auth.py`, alle Antwortschemas, `Dockerfile`, `docker-compose.yml`,
`requirements*.txt` (keine neue Abhängigkeit).

### 13.2 Abweichungen vom Konzept

- **Wrapper statt Änderungen in den Clients:** Stirling- und Ollama-Aufrufe werden von
  `MonitoredStirlingClient` und `MonitoredOllamaClient` gemessen. Diese legt `main.py`
  nur bei aktiviertem Monitoring um die bestehenden Objekte. Argumente, Rückgabewerte
  und Exceptions bleiben unverändert. Ollama-Werte stammen aus dem `timing`-Dict, das
  der Client ohnehin füllt (`load`/`prompt_eval`/`eval`-Dauer, Token-Zahlen). So bleibt
  der lokale Produktionspatch in `ollama.py` konfliktfrei.
- **Client-Zuordnung ohne Änderung an `auth.py`:** Die Middleware liest nach Abschluss
  des Requests `request.state.client_id`. Diesen Wert setzt nur
  `get_authenticated_client` nach erfolgreicher Key-Prüfung. Er wird zusätzlich gegen
  die konfigurierten Client-IDs geprüft. Body- oder Header-Werte werden nie verwendet.
- **Route-Template:** Es ist das Template der tatsächlich gerouteten FastAPI-Route
  (`scope["route"].path`). Für die Live-Liste wird es vorab über die registrierten
  Routen bestimmt. Nicht registrierte Pfade werden als `<unmatched>` gespeichert.
- **Flush-Intervall maximal 30 s**, damit jede Minute, in der der Collector lebt,
  mindestens einen Heartbeat hat (Grundlage der Lückenerkennung, §13.4).
- Auth-Tabellen liegen seit M3 in einer **separaten** Datenbank (`monitor-auth.db`, §14.8)
  statt als Migration v2 der Telemetrie. Das Telemetrie-Schema bleibt v1.

### 13.3 Datenbankschema (Version 1)

Alle Zeitstempel sind UTC-Epoch-Millisekunden; die Darstellung in Browser-Ortszeit
folgt in M4. Dauern werden mit `time.perf_counter()` gemessen (monoton). Text-Spalten
enthalten nur Registry-Werte oder Konfigurations-IDs.

| Tabelle | Zweck |
|---|---|
| `instances` | je Prozess: `instance_id`, PID, Start, letzter Heartbeat, Stop, Version, Build-Infos (`build_source_state` clean/modified/unknown) |
| `request_events` | je Request: Request-ID, Zeit, Dauer, Client, Route-Template, Methode, Service, HTTP-Status, Fehlercode, Bytes rein/raus, Dokumente, Seiten (nullable), KI-Provider/Modell, KI-Calls, Tokens (nullable = unbekannt) |
| `request_steps` | je Verarbeitungsschritt: Step, Ergebnis, Dauer (DocPipe), Ollama-Werte (Gesamt/Laden/Prompt/Generierung), Tokens, Repair |
| `rollup_requests` | Minute und Stunde × (Client, Route, Methode, Service, Status, Fehlercode, Modell): Anzahl, Summe/Min/Max Dauer, 21 Histogramm-Buckets, Bytes, Dokumente, Seiten, Tokens |
| `rollup_steps` | Minute und Stunde × (Route, Step, Ergebnis, Modell): wie oben plus Ollama-Summen |
| `system_samples` / `rollup_system` | Metrik, Quelle (host/container/process/filesystem/network_namespace/gpu/monitoring), Wert, Status, Grund |
| `service_probes`, `ollama_models` | Probe-Ergebnisse; installierte bzw. geladene Modelle mit Größe, VRAM, Quantisierung, Kontext, Ablauf |
| `events` | strukturierte Ereignisse (Severity, Code, Service, optional Client/Route/Fehlercode/Request-ID), Wiederholungen innerhalb von 60 s zusammengefasst |
| `data_quality` | je Minute und Instanz: Heartbeats, persistierte und verworfene Requests (Puffer/Store), verworfene Events, gekürzte Steps, Flush-, Task- und Hook-Fehler |
| `live_requests` | aktive Requests je Instanz (Snapshot bei jedem Flush, für mehrere Worker) |
| `leases` | prozessübergreifende Leases: nur ein Worker sampelt bzw. probt |

Rollups werden **in derselben Transaktion** wie die Rohzeilen additiv per UPSERT
fortgeschrieben. Sie sind dadurch exakt, auch bei mehreren Workern und bei
nachträglich geschriebenen Batches nach einem Store-Ausfall.

Speichergrenze: `PRAGMA max_page_count` begrenzt die Hauptdatei,
`journal_size_limit` und regelmäßige TRUNCATE-Checkpoints die WAL-Datei.
`temp_store=MEMORY` verhindert SQLite-Temp-Dateien. Die Größenprüfung summiert
db, `-wal`, `-shm` und `-journal`.

### 13.4 Datenvollständigkeit und Fehlerpolitik

| Situation | Verhalten | Sichtbar als |
|---|---|---|
| Puffer voll | neuer Datensatz wird verworfen, nie blockiert | `requests_dropped_buffer` je Minute, Event `buffer_overflow` (einmal je Minute) |
| Store nicht öffnbar / Schreibfehler / gesperrt | Batch bleibt im (begrenzten) Puffer und wird wiederholt; was nicht passt, wird verworfen. Nach 3 Fehlern in Folge wird die Verbindung neu geöffnet (alle 30 s) | `flush_failures`, `requests_dropped_store`, Events `store_unavailable`/`store_recovered` |
| Speicher (db + -wal + -shm) ≥ Grenze | Schreiben pausiert; zusätzlich begrenzt `max_page_count` die Hauptdatei in SQLite selbst | Event `storage_limit_reached` |
| ≥ 90 % der Grenze | älteste 10 % der Rohdaten werden vorzeitig gelöscht | Event `storage_pressure` |
| freier Platz < `min_free_disk_bytes` | Schreiben pausiert | Event `disk_space_low` |
| Collector-Task schlägt fehl | Task isoliert, Thread läuft weiter | `task_failures`, Event `collector_task_failed` (mit Task-Name) |
| Collector-Thread tot / Prozess hängt | keine Heartbeats | `collector_gap_minutes`, `health()["collector"] == "dead"` |
| Hook-Fehler im Request-Pfad | Request läuft unverändert weiter | `hook_failures` |

`MonitoringStore.completeness(start, end)` liefert für jeden Zeitraum `complete`
(nur `true`, wenn nichts verworfen wurde **und** keine Collector-Lücke besteht),
die Verlustzähler und `recorded_ratio`. Abfragen wie `request_stats()` liefern
diese Angabe immer mit. M4 darf unvollständige Zeiträume nicht als vollständig
darstellen. Verlustzähler, die wegen eines Store-Ausfalls noch nicht geschrieben
wurden, stehen in `Monitor.health()` (`unsaved_requests_dropped`).

Bei normalem Betrieb sind die Zähler exakt. Das wird mit 100 Requests über
50 parallele Threads getestet: 100 Datensätze, 100 verschiedene IDs, korrekte
Client-Aufteilung, `complete = true`.

Die Middleware zeichnet unbehandelte Exceptions als Status 500 /
`internal_error` mit Event `unhandled_exception` auf und reicht die Exception
unverändert weiter. Die Antwort an den Client ist identisch zu vorher.

### 13.5 Verfügbare Metriken (M2)

- **Requests:** Anzahl, Erfolg/Fehler (Status ≥ 400 bzw. ≥ 500), Fehlercodes, Dauer
  (Ø, p50/p95/p99), Bytes, Dokumente, Seiten (nur forms), je Client, Route, Service,
  Modell und Zeitraum.
  - Perzentile sind exakt innerhalb der Rohdaten-Retention (Nearest Rank).
  - Darüber hinaus sind sie Histogramm-Schätzungen, gekennzeichnet über
    `percentile_method`. Die Schätzung liegt nachweislich im Bucket des exakten Werts.
- **Steps:**
  - `stirling.extract_text`, `stirling.ocr`, `stirling.convert`: DocPipe-gemessene Dauer und Ergebnis.
  - `ai.generate`, `ai.warmup`: DocPipe-Dauer plus die von Ollama gemeldeten Werte
    `load`, `prompt_eval`, `eval` und Tokens. Tokens/s sind abgeleitet aus Ollama-Werten.
  - `forms.inspect`, `forms.fill`: Dauer der In-Process-Verarbeitung. Ein darin
    enthaltener OCR-Call steht zusätzlich als eigener Step.
- **System:**
  - Host: CPU %, Kerne, Load, RAM.
  - Container (cgroup v2): RAM aktuell/Limit, CPU-Kerne genutzt/Limit.
  - Prozess: RSS, CPU %, Threads, FDs.
  - Dateisysteme: Datenverzeichnis und `/`.
  - Netz des Netzwerk-Namespace.
  - GPU: nicht konfiguriert.
  - Monitoring: DB-Größe.
- **Dienste:**
  - Ollama: Version, installierte und geladene Modelle, VRAM (von Ollama gemeldet).
  - Stirling: Status und Version.
  - Je Probe: Latenz und Status (`ok`, `timeout`, `connection_error`, `http_error`,
    `invalid_response`, `not_configured`).
- **Nicht gemessen in M2:** Threadpool-Auslastung, Warteschlange (es gibt keine),
  Prozessmetriken weiterer Worker (nur der Lease-Halter sampelt), Mandanten,
  Client-ID aktiver Requests vor Request-Ende.

### 13.6 Sicherheitsmaßnahmen M2

- Bei `enabled: false` gibt es keine Datei, keinen Thread, keinen Wrapper, keine Probe und
  keine Route. Die Antworten sind bytegleich (Test vergleicht Status, Body und Header).
- Nur Registry-Werte und Konfigurations-IDs werden gespeichert. `DocPipeError.message`,
  Exception-Texte, Pfade, Query-Strings, Header, Dateinamen und Bodies nie. Die
  Request-ID wird serverseitig erzeugt, ein `X-Request-ID` des Clients wird ignoriert.
- Probes: nur `GET` auf `/api/version`, `/api/tags`, `/api/ps` und `/api/v1/info/status`,
  URLs ausschließlich aus der bestehenden Konfiguration. Keine Redirects, keine
  Env-Proxys, kein Stirling-API-Key, Antworten auf 1 MiB begrenzt, Modellnamen und
  Versionen per Regex gefiltert.
- Datenverzeichnis `0700`, Datenbank `0600` (POSIX).
- Keine Privilegien, kein Docker-Socket, keine Host-Mounts.

### 13.7 Performance (lokal gemessen)

Methode: `python -m system.tools.monitoring_overhead --requests 2000 --rounds 5`.
Synthetische Requests, Fake-Upstreams, temporäres Verzeichnis. Gemessen am
26.11.2026 auf dem Windows-Entwicklungsrechner (Python 3.12, FastAPI 0.165,
Starlette 1.10). Der Mikro-Benchmark misst `Monitor.begin()` + `end()` mit echtem
Route-Lookup und zwei Steps über 50.000 Iterationen. Der End-to-End-Vergleich
verwendet den TestClient und abwechselnde Runden mit und ohne Monitoring.

| Messung | Ergebnis |
|---|---|
| `begin()` + `end()` pro Request | 23–59 µs (4 Läufe) |
| End-to-End-Median ohne / mit Monitoring | 7,3–7,6 ms Basis; Differenz −0,05 bis +0,71 ms (4 Läufe). Das liegt in der Streuung des TestClients unter Windows und ist kein belastbarer Einzelwert. |
| Flush von 5000 gepufferten Requests (Collector-Thread, außerhalb des Request-Pfads) | 50–213 ms |

Die Werte gelten für diese Maschine. Auf dem Linux-Server müssen sie in M7 erneut
gemessen werden. Ein automatisierter Test sichert großzügige Grenzen ab:
< 500 µs pro Request und < 15 MiB für einen vollen Puffer mit 10.000 Einträgen.

### 13.8 Bekannte Einschränkungen und offene Punkte nach M2

- Probes laufen synchron im Collector-Thread, im ungünstigsten Fall 4 × Timeout
  (Default 3 s). Flushes werden dadurch nicht blockiert, weil das Store-Lock
  außerhalb der Netzwerkaufrufe liegt, aber verzögert.
- Verschachtelte Steps (forms.inspect mit stirling.ocr) werden nicht in Eigenzeit
  umgerechnet. M4 muss sie als verschachtelt darstellen.
- Eine Lease-Übernahme nach dem Absturz eines Workers dauert bis zu 3 Intervalle.
- Die Wirksamkeit auf dem Produktionsserver (cgroup v2 gemountet, Host-Networking,
  Volume-Rechte) ist **nicht verifiziert** **[A]**.
- Weitere Härtungspunkte außerhalb des Monitorings:
  [docs/security/hardening-backlog.md](../security/hardening-backlog.md).

---

## 14. Umsetzung Phase M3 – Secure Monitoring Access

Hier steht der tatsächlich implementierte Stand. Statistik- und
Performance-Dashboards gehören nicht dazu, sie folgen mit M4.

### 14.1 Aktivierung

| `monitoring.enabled` | `monitoring.web.enabled` | Ergebnis |
|---|---|---|
| `false` (Default) | – | keine Datenbank, Dateien, Threads, Upstream-Abfragen; `/monitor*` = normales 404 |
| `true` | `false` (Default) | Datenerfassung (M2), keine Monitoring-Route, keine Auth-Datenbank |
| `true` | `true` | Datenerfassung und Weboberfläche |
| `false` | `true` | **Startfehler** (ConfigError) |

Strukturell unsichere Konfigurationen verhindern den Start (ConfigError):

- `allowlist` mit leerer Liste oder mit `0.0.0.0/0` bzw. `::/0`,
- `require_https: false` außerhalb von reinem Loopback,
- `public_mfa` ohne HTTPS oder mit Allowlist,
- ein Trusted Proxy `0/0`,
- unbekannte Schlüssel und Werte außerhalb der Grenzen.

Laufzeitprobleme **sperren nur die Oberfläche** mit 503 und einem Audit-Eintrag
`security_config_error`; die DocPipe-API bleibt unberührt. Dazu zählen:

- Benutzerdatei fehlt, ist ungültig, hat zu weite Rechte oder einen Benutzer ohne TOTP,
- die Auth-Datenbank lässt sich nicht öffnen,
- Uvicorn schreibt Proxy-Header selbst um.

Lässt sich die Oberfläche gar nicht erst aufbauen, bleibt sie abwesend (404),
ist aber nie offen.

### 14.2 Routen

Die Oberfläche ist eine eigene kleine ASGI-Anwendung. `MonitoringMiddleware`
leitet `/monitor` und `/monitor/*` nur bei aktivierter Oberfläche dorthin weiter.
Diese Requests erreichen nie den FastAPI-Router der Kunden-API, zählen nicht als
API-Telemetrie und erscheinen nicht im OpenAPI-Schema.

| Route | Methode | Zugang | Zweck |
|---|---|---|---|
| `/monitor` | GET | Session | Dashboard-Shell (Seitenaufruf = Aktivität) |
| `/monitor/login` | GET/POST | Netzwerk | Schritt 1: Benutzername + Passwort |
| `/monitor/login/mfa` | GET/POST | temporärer MFA-Zustand | Schritt 2: TOTP-Code |
| `/monitor/logout` | POST | Session + CSRF | serverseitige Invalidierung |
| `/monitor/api/v1/session` | GET | Session | Benutzer, Idle-/Absolut-Ablauf (verlängert nichts) |
| `/monitor/api/v1/status` | GET | Session | Collector-/Store-Zustand (echte Werte aus `Monitor.health()`) |
| `/monitor/static/*` | GET | Netzwerk | nur gelistete lokale `.css`/`.js`-Dateien |

Die separate MFA-Route ist nur mit einem gültigen temporären Zustand erreichbar.
Dieser Zustand:

- entsteht erst nach korrektem Passwort,
- liegt als Cookie `__Host-dp_mon_mfa` vor, in der Datenbank nur als Hash,
- ist 5 min gültig und erlaubt höchstens 5 Versuche,
- existiert pro Benutzer nur einmal und wird nach Erfolg oder Ablauf gelöscht.

Eine Session gibt es erst nach beiden Faktoren.

Jede Anfrage durchläuft diese Prüfungen in fester Reihenfolge, jede schließt im Fehlerfall:

1. Netzwerkzugang
2. statische Datei
3. Sperrzustand
4. Benutzerdatei
5. Session

Ohne Session gibt es nur `303 → /monitor/login` (Seiten) oder
`401 {"ok":false,"code":"unauthorized"}` (API), keine technischen Daten.
Kunden-API-Keys werden auf diesen Routen nie ausgewertet.

### 14.3 Benutzer und Einrichtung

- **Benutzerdatei:** separat (`monitoring.web.users_file`, YAML, `version: 1`).
  - Benutzername, Argon2id-Hash (m = 64 MiB, t = 3, p = 1), TOTP-Secret (Base32, ≥ 128 bit), `enabled`.
  - Unter POSIX gilt: reguläre Datei, Eigentümer = Prozessbenutzer, keine Gruppen- oder Fremdrechte (höchstens 0600).
  - Die Datei wird bei Änderung neu geladen. Jede Änderung eines Benutzers (Passwort, MFA, Status) beendet dessen Sessions (Fingerprint).
- **Windows-Entwicklungsbetrieb:** POSIX-Rechte sind nicht prüfbar. Die Prüfung entfällt dort und ist ausgewiesen (`permissions_checked = false`). Produktion ist Linux.
- **CLI** `python -m system.tools.monitor_user`, Befehle `add`, `reset-password`, `reset-mfa`, `disable`, `enable`, `remove`, `revoke-sessions`, `list`:
  - Passwörter werden nur per `getpass` abgefragt, nie als Argument (es gibt keine Option dafür).
  - Das TOTP-Secret wird erzeugt und einmalig im Terminal angezeigt (Secret und `otpauth://`-URI). Gespeichert wird erst nach Eingabe eines gültigen Codes aus der App.
  - Die Datei wird atomar geschrieben, mit Rechten 0600.
  - Es gibt keine Standardpasswörter und keine Registrierung; die Oberfläche hat keine Benutzerverwaltung.
- **Wiederherstellung:** Der Betreiber führt `reset-password` bzw. `reset-mfa` auf dem Server aus. Alte Sessions enden dabei automatisch; `revoke-sessions` beendet Sessions sofort.

### 14.4 TOTP

- Verfahren: RFC 6238 mit HMAC-SHA1, 6 Stellen, 30 s – kompatibel mit üblichen Authenticator-Apps. Getestet mit den RFC-Testvektoren.
- Toleranz ±1 Zeitschritt (konfigurierbar 0..1). Codes werden in konstanter Zeit verglichen.
- **Replay-Schutz atomar:** Ein Code gilt nur, wenn sein Zeitschritt-Zähler größer ist als jeder bisher akzeptierte Zähler des Benutzers.
  - Umsetzung als UPSERT mit Bedingung in einer `BEGIN IMMEDIATE`-Transaktion, zusammen mit Session-Anlage und Audit.
  - Damit funktioniert der Schutz auch über mehrere Worker hinweg.
  - Gespeichert wird nur der Zähler, nie ein Code.
- MFA-Fehlversuche zählen in dieselben IP- und Benutzer-Limits wie Passwortfehler.

### 14.5 Sessions

- **Session-ID:** 256 bit aus `secrets`. In der Datenbank steht nur der SHA-256-Hash.
- **Rotation:** Bei jedem Login entsteht eine neue ID. Eine mitgesendete alte Session wird gelöscht; eine untergeschobene ID wird nie übernommen.
- **Cookie** `__Host-dp_mon`: `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`, ohne `Domain`, ohne `Max-Age`.
- **Timeouts:** Idle 15 min (5–60), absolut 8 h (1–12), höchstens 3 Sessions pro Benutzer (1–10; die älteste endet).
- **Aktivität wird serverseitig festgelegt, nicht durch Client-Angaben:**
  - Nur Seitenaufrufe (`GET /monitor`) zählen als Aktivität.
  - Kein Aufruf unter `/monitor/api/*` verlängert die Idle-Zeit, unabhängig von Headern. Das Dashboard-Polling setzt zwar `X-Monitor-Poll: 1`, der Server wertet ihn aber nicht aus.
  - Ein unbeaufsichtigter Tab läuft damit nach dem Idle-Timeout ab.
- **Logout:** serverseitige Löschung, Cookie wird gelöscht, Audit-Eintrag.
- **Abgelaufene Sessions** werden beim nächsten Zugriff oder beim periodischen Aufräumen gelöscht und mit Grund `idle`/`absolute` auditiert.

### 14.6 Login-Schutz und Rate-Limits

- **Pro IP:** 10 Fehler in 15 min, dann 15 min Sperre.
- **Pro Benutzername:** 5 Fehler in 15 min, dann 15 min Sperre, unabhängig von der IP.
  - Das gilt auch für unbekannte Namen, sodass die Sperre nichts über die Existenz eines Benutzers verrät.
  - Gespeichert werden nur HMAC-Hashes (Schlüssel zufällig in der Auth-Datenbank), nie eingetippte Namen.
- Die Zähler liegen in SQLite und werden atomar fortgeschrieben, damit sie über mehrere Worker gelten.
- **Einheitliche Antworten:** Falsches Passwort, unbekannter Benutzer, deaktivierter Benutzer, Sperre und fehlende Kapazität ergeben alle dieselbe Seite mit HTTP 401. Unbekannte Benutzer werden gegen einen Dummy-Hash geprüft.
- **Begrenzung von Argon2id** pro Prozess: höchstens 2 gleichzeitig und 30 pro Minute. Darüber hinaus läuft Argon2 gar nicht, und die Antwort ist die einheitliche Fehlermeldung. Gesperrte Subjekte lösen ebenfalls kein Argon2 aus. Argon2 und SQLite laufen im Threadpool, nie im Event-Loop der API.
- **Request-Grenzen:**
  - POST-Body höchstens 4 KiB, sonst 413.
  - Nur `application/x-www-form-urlencoded` mit höchstens 10 Feldern.
  - Falsche Methoden ergeben 405.

### 14.7 Browser-Sicherheit

- **CSRF:** Login und MFA nutzen ein Double-Submit-Token im `__Host-dp_mon_csrf`-Cookie. Logout nutzt ein an die Session gebundenes HMAC-Token.
  - Zusätzlich muss `Origin` exakt passen: `public_origin` oder Schema + Host.
  - Fehlt `Origin`, wird nur `Sec-Fetch-Site: same-origin` akzeptiert; `Origin: null` wird abgelehnt.
  - Login-CSRF von fremden Seiten wird damit abgewiesen.
- **Header auf jeder Antwort**, auch bei 403/404/503 und für statische Dateien:
  - CSP: `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: same-origin` – bewusst nicht `no-referrer`, weil Browser dann bei Formular-POSTs `Origin: null` senden und die Origin-Prüfung den Login blockieren würde (vom Browser-Test gefunden).
  - `X-Frame-Options: DENY`
  - restriktive `Permissions-Policy`
  - COOP und CORP `same-origin`
  - `Cache-Control: no-store` (statische Dateien: `private, max-age=3600`)
- **XSS:** Jinja2 mit Autoescape. Keine Inline-Skripte, keine Inline-Styles, keine Event-Handler-Attribute. Das JavaScript schreibt Daten nur per `textContent`.
- **Lokale Ressourcen:** Bootstrap 5.3.3 und eine Teilmenge von Bootstrap Icons 1.11.3 (serverseitig als Inline-SVG) werden lokal ausgeliefert. Kein CDN, kein Tracking. Versionen, Prüfsummen und Lizenzen stehen in `system/monitoring/web/static/vendor/README.md`.
- **Keine sensiblen Daten in URLs:** Fehlermeldungen werden direkt gerendert, nicht über Query-Parameter weitergereicht.

### 14.8 Sicherheitsdatenbank (`<data_dir>/monitor-auth.db`)

Die Sicherheitsdaten liegen bewusst **getrennt** von der Telemetrie-Datenbank:

- Telemetrie ist fail-open, Authentifizierung fail-closed.
- Retention und Pressure-Pruning der Telemetrie können Sessions, Rate-Limits und Audit nie berühren.
- Die Auth-Datenbank hat eine eigene Größengrenze (`auth_db_max_bytes`); ist sie voll, schlagen Logins geschlossen fehl.
- Eine Migration der M2-Telemetrie war nicht nötig, das Telemetrie-Schema bleibt v1, und vorhandene Daten bleiben unverändert.

Weitere Eigenschaften:

- Schema v1 mit `PRAGMA user_version` und Migrationsliste.
- Tabellen: `meta`, `sessions`, `pending_mfa`, `rate_limits`, `totp_last`, `audit`.
- WAL, `synchronous=FULL`, Rechte 0600.
- Jeder Zugriff hält ein Prozess-Lock (eine Verbindung pro Prozess); zwischen Workern serialisiert `BEGIN IMMEDIATE`.
- Jeder Datenbankfehler führt zu einer Ablehnung (503) und nie zu Zugriff.
- Gespeichert werden **nicht**: Passwörter, TOTP-Codes, Session-IDs im Klartext, Kunden-API-Keys, eingetippte Benutzernamen.

### 14.9 Audit

- **Ereignisse:**
  - Anmeldung: `login_success`, `login_failed`, `mfa_failed`, `totp_replay`, `lockout`, `login_throttled`
  - Session: `logout`, `session_expired`, `session_revoked`
  - Zugriff und Konfiguration: `access_denied`, `csrf_failed`, `security_config_error`
- **Felder:** Zeit, Ereignis, Benutzername (nur wenn in der Benutzerdatei vorhanden), Client-IP, Grund-Code, gekürzte Session-Referenz (12 Hex-Zeichen des Hashes), Wiederholungszähler.
- Wiederholte gleiche Ereignisse, etwa `access_denied` von derselben IP, werden innerhalb von 60 s zusammengefasst.
- **Aufbewahrung:** unabhängig von der Telemetrie, Default 90 Tage und höchstens 100.000 Zeilen.
- Die Audit-Daten sind in M3 nur in der Datenbank. Eine Anzeige im Bereich „Sicherheit“ folgt in M4.

### 14.10 Betrieb der Weboberfläche (Deployment-Anforderungen)

Die produktive Serverkonfiguration wurde **nicht** verändert. Für eine spätere,
eigens freizugebende Aktivierung gilt:

1. **Uvicorn ohne eigene Proxy-Header-Auswertung.**
   - Das `Dockerfile` startet jetzt mit `--no-proxy-headers`. Die Kunden-API nutzt weder Client-Adresse noch Schema; das ist geprüft, die API ist also nicht betroffen.
   - Überschreibt das Produktions-Compose den Startbefehl, muss es das Flag ebenfalls setzen, alternativ `UVICORN_PROXY_HEADERS=false`.
   - Erkennt DocPipe einen Uvicorn-CLI-Start ohne das Flag, bleibt die Oberfläche gesperrt (`uvicorn_proxy_headers`).
2. **Reverse Proxy:**
   - Er terminiert HTTPS.
   - Er setzt `X-Forwarded-For` selbst, indem er die echte Gegenstelle **anhängt** (z. B. nginx `proxy_add_x_forwarded_for`), und `X-Forwarded-Proto https`.
   - Er reicht den `Host`-Header unverändert durch oder es wird `public_origin` gesetzt.
   - Seine Adresse kommt in `monitoring.web.trusted_proxies`, z. B. `127.0.0.1/32` bei Host-Networking.
   - `/monitor` sollte zusätzlich am Proxy auf VPN bzw. Allowlist beschränkt sein (Defense in Depth).
3. **Volumes:** Das Datenverzeichnis ist schreibbar für UID 1000 (Telemetrie- und Auth-Datenbank). Die Benutzerdatei liegt außerhalb des Repositories, gehört dem Prozessbenutzer und hat Rechte 0600.
4. **Einrichtung:** `python -m system.tools.monitor_user --file <users_file> add <name>` im Container bzw. Image mit interaktivem Terminal ausführen.
5. **Smoke-Test nach Aktivierung:**
   - `/monitor` von außerhalb der Allowlist ergibt 403.
   - Login mit beiden Faktoren funktioniert.
   - Logout invalidiert die Session.
   - Die HubDix- und PDF4Fill-Regression ist unverändert.
6. **Rollback:** `monitoring.web.enabled: false` und Neustart. Die Auth-Datenbank kann gelöscht werden; sie enthält nur Sitzungs- und Sicherheitsdaten.

### 14.11 Tests

- **Neue Tests in M3 (unter Windows ausgeführt):** 7 POSIX-Rechte-Tests werden dort übersprungen und laufen unter Linux.
  - Konfiguration und Aktivierung
  - Benutzerdatei
  - Netzwerk und Proxy (IPv4, IPv6, IPv4-mapped)
  - TOTP mit RFC-Vektoren
  - Anmeldung und Sessions: Replay, parallele TOTP-Anfragen, Fixation, Idle- und Absolut-Timeout, Polling ohne Verlängerung
  - CSRF, Login-CSRF, Brute-Force und Rate-Limits über mehrere Worker, Argon2-Begrenzung
  - Fail-closed bei Auth-Store- und Benutzerdatei-Ausfall, Telemetrie-Ausfall ohne Einfluss auf die Anmeldung
  - alle bestehenden Kunden-API-Keys gegen alle geschützten Routen
  - Kanarienvögel in Audit, HTML, JSON, Logs und Datenbankdateien
  - Security-Header und CSP auf allen Antworten, keine Inline- oder externen Ressourcen
  - CLI
- **H1** (konstante Laufzeit beim API-Key-Vergleich) und **H2** (Access-Log ohne Rohpfade) haben eigene Testdateien.
- **Browser-Test** `tests/test_monitor_web_browser.py`, optional; ohne Playwright und Chromium wird er übersprungen:
  - echter Uvicorn-Server und Edge headless
  - Desktop/Deutsch/Hell und Smartphone/Englisch/Dunkel
  - Login, Fehlermeldung, MFA, Shell, mobile Navigation, Logout
  - Cookie-Attribute aus Browsersicht
  - keine CSP-Verstöße und keine Skriptfehler

### 14.12 Offene Punkte nach M3

- Ohne eigenes Gerät gibt es keinen Wiederherstellungscode. Die Wiederherstellung läuft bewusst nur über das Betreiber-CLI auf dem Server.
- Die Benutzersperre pro Name kann von außen ausgelöst werden (Lockout-DoS). Im Modus `allowlist` ist das auf freigegebene Netze beschränkt; in `public_mfa` ist es ein bekanntes Restrisiko zugunsten des Brute-Force-Schutzes.
- Sessions sind nicht an die IP gebunden (mobile Netze, VPN-Wechsel). Eine IP-Bindung wäre optional nachrüstbar.
- Die Erkennung der Uvicorn-Proxy-Header deckt CLI-Starts ab. Andere Launcher, etwa Gunicorn mit Uvicorn-Workern, sind durch die Deploy-Anforderung abgedeckt, nicht technisch erzwungen.
- Synchrone Probes im Collector-Thread (aus M2) bleiben ein dokumentiertes Performance-Risiko und Teil der M7-Lasttests.

---

## 15. Umsetzung Phase M4 – Dashboard und Live-Daten

Hier steht der tatsächlich implementierte Stand; die Details der API stehen in
[monitoring-api.md](monitoring-api.md).

### 15.1 Bereiche

| Bereich | Route | Inhalt |
|---|---|---|
| Übersicht | `/monitor` | Dienststatus (DocPipe, Collector, Ollama, Stirling); KPIs (Anfragen, aktiv, Erfolgs- und Fehlerquote, Ø, Median, P95, P99, Laufzeit); Zeitreihen (Anfragen, Antwortzeit, pro Client, Serverlast); Systemkacheln mit Herkunft; Build; Datenqualität |
| Live-Aktivität | `/monitor/activity` | laufende Requests (aller Worker, veraltete Snapshots markiert) und abgeschlossene Requests mit Schritten; Filter Client, Dienst, Status, Zeitraum; Cursor-Pagination |
| Projekte & Clients | `/monitor/clients` | Anteil, Verlauf pro Client, Vergleich mit dem Vorzeitraum, Tabelle mit allen Kennzahlen, Nutzung nach Dienst; nicht authentifizierte Requests separat |
| Performance | `/monitor/performance` | Request-Dauer (Ø, Median, P95, P99, Min/Max, Anzahl, Fehlerquote); pro Schritt dieselben Werte; Ollama-Teilzeiten (von Ollama gemeldet); Hinweis auf inklusive bzw. exklusive Dauer |
| Server | `/monitor/server` | Dienststatus, Kacheln (Host, Container, Prozess, Dateisystem, Netz-Namespace, GPU) mit Herkunft und Status; Verläufe CPU, Speicher, Netz, freier Plattenplatz |
| Sicherheit | `/monitor/security` | Audit-Kennzahlen nach Kategorie, Verlauf, eigene aktive Sessions, Ereignisliste (paginiert) – nur lesend |
| Ollama, Stirling PDF | – | deaktiviert, „geplant M5“; allgemeiner Status in Übersicht und Server |
| KI-Connectoren | – | deaktiviert, „geplant #2“ (der Menüpunkt Logs ist seit M7 entfernt) |

Es gibt keine Administration, keine Steuerung und keine Änderung von Konfigurationen.

### 15.2 Technik

- **Gleiche Architektur wie M3:** eigene ASGI-App, Jinja2, Bootstrap 5.3.3, Bootstrap-Icons-
  Teilmenge und **Chart.js 4.4.7**. Alles wird lokal ausgeliefert, npm-Integrität und SHA-256
  sind dokumentiert, Lizenzen liegen bei. Kein Build-Schritt, keine Node-Laufzeit im Image.
- **Datenschicht** `system/monitoring/web/dashboard.py`:
  - Lesende Verbindung (`mode=ro`, `query_only`). Im WAL-Modus blockiert sie den Collector
    nicht; getestet mit einem offenen Schreib-Lock.
  - Zeitbudget von 3 s pro Abfrage.
  - Ausschließlich validierte Filter.
- **Eine Quelle pro Antwort**, damit es keine Doppelzählung gibt: Rohdaten (≤ 1 h), Minuten-Rollups
  (≤ 24 h) oder Stunden-Rollups, mit höchstens 120 Punkten. Rollup-Zeiträume beginnen am
  Bucket-Anfang (`effective_start_ms`).
- **Lücke gegenüber echter Null:** Ein Bucket ist nur dann 0, wenn der Collector darin
  Heartbeats geschrieben hat; sonst `null`. Abdeckungsgrad pro Bucket; Minuten nach dem
  letzten Heartbeat gelten als „noch nicht geschrieben“.
- **Telemetrie-Migration v2** fügt nur Indizes hinzu; die Daten bleiben erhalten.
  - Neue Indizes auf `request_events`: `(service, ts_ms)` und `(request_id)`.
  - Neue Indizes auf `request_steps(step, ts_ms)`, `system_samples(metric, ts_ms)`,
    `service_probes(service, ts_ms)` und `events(service, last_ts_ms)`.
  - Die Abfragepläne der Kernabfragen werden per Test mit `EXPLAIN QUERY PLAN` geprüft: Index
    oder Primärschlüssel, kein Tabellenscan.
- **Dienststatus nur aus gespeicherten Daten:**
  - Ollama und Stirling: letzte Probe, letzte erfolgreiche Probe, „Eingeschränkt“ bei
    `upstream_error` in den letzten 15 min, „Veraltet“ bei alter Probe, „Nicht konfiguriert“
    bei abgeschalteter Probe.
  - Collector: `Monitor.health()` und das Alter des letzten Flush.
  - Kein Browser-Request löst eine Upstream-Abfrage aus (getestet).
- **Inklusive Schrittdauern:** `forms.inspect` enthält `stirling.ocr`. Die exklusive Dauer wird
  nur aus Rohdaten pro Request berechnet; bei Rollups ist sie „nicht verfügbar“. Die
  Oberfläche weist darauf hin, dass die Summe der Schritte nicht die Request-Dauer ist.

### 15.3 Live-Aktualisierung

- **Polling-Intervalle** kommen vom Server: Live-Aktivität alle 5 s, Übersicht und Server alle
  15 s, übrige Bereiche alle 30 s. Konfigurierbar über `monitoring.web.poll_*_seconds`.
- **Steuerung im Browser:**
  - Es läuft immer nur ein Request; ein neuer bricht den vorherigen ab (`AbortController`).
  - Bei Fehlern greift exponentielles Backoff bis 5 min.
  - Bei verborgenem Tab oder „Pause“ wird nicht abgefragt.
  - Bei 401 oder 403 wird sofort gestoppt (401 führt zum Login).
- **Darstellung:**
  - Bei Fehlern bleiben die letzten Daten sichtbar und werden als veraltet markiert
    (abgeschwächt, Hinweis, „Veraltet“ in der Statuszeile).
  - Diagramme werden in-place aktualisiert (`update('none')`); die Seite wird nicht neu gezeichnet.
- **Session-Schutz:**
  - Eine Begrenzung pro Session (Default 240 pro Minute) schützt vor außer Kontrolle geratenen Tabs.
  - Polling verlängert die Session nie.
  - Nur echte Bedienaktionen (Filterwechsel, „weitere laden“) senden höchstens alle 30 s den
    CSRF-geschützten `POST /monitor/session/activity`.
- **Animationen** sind kurz (250 ms) und entfallen bei `prefers-reduced-motion`.

### 15.4 Darstellung von Datenqualität und Unsicherheit

| Fall | Darstellung |
|---|---|
| echter Messwert 0 | „0“ |
| nicht verfügbar / nicht konfiguriert | „—“ mit Begründung (z. B. „GPU-Metriken werden nicht erfasst“, „Noch kein Messwert“) |
| veraltet | Status-Badge „Veraltet“; bei Verbindungsverlust abgeschwächte Werte und Hinweis |
| unvollständig | Badge „Unvollständig“ an KPIs und im Panel „Datenqualität“ (verworfene Datensätze, Collector-Lücken, Zeitraum vor den ersten Daten) |
| geschätzt | „≈ Schätzung“ (Perzentile aus Histogrammen); Datenquelle und Auflösung an jedem Diagramm |
| Nenner 0 | keine Prozentzahl („—“) |
| Build ohne Angaben | Commit und Image-Tag „nicht verfügbar“, Quellstand „nicht angegeben“; Hinweis, dass ein Commit allein kein unverändertes Image belegt |

### 15.5 Sicherheit

- Die M3-Architektur ist unverändert: Netzwerkprüfung, Session (Argon2id und TOTP), CSRF,
  strikte CSP ohne `unsafe-inline` und `unsafe-eval`. Chart.js funktioniert unter dieser CSP; der
  Browser-Test schlägt bei jedem CSP-Verstoß fehl.
- Daten werden im Browser nur per `textContent` bzw. als Canvas-Text ausgegeben. Ein Test stellt
  sicher, dass die Skripte kein `innerHTML`, `eval` oder `new Function` enthalten.
- Texte für das Skript werden als escaptes Datenattribut übergeben, nicht als Inline-Skript.
- Die Security-Ansicht zeigt nur Audit-Felder und eine 12-stellige Session-Referenz – keine
  Tokens, Hashes, Passwörter oder Codes. Es gibt keine Benutzerverwaltung, keine
  Entsperrung und kein Löschen.

### 15.6 Ergebnisse

- **API-Tests** (`tests/test_monitor_dashboard_api.py`):
  - Zugriffsmatrix inklusive Kunden-Keys, Validierung und SQL-Injection.
  - Leere Datenbank, Korrektheit gegen synthetische Daten (Zählungen, exakte Perzentile, keine
    Doppelzählung, UTC-Grenzen, Lücken gegenüber Nullen).
  - Clients, Schritte, Server, Security, Pagination, Rate-Limit, Aktivitätsroute.
  - Keine Upstream-Aufrufe, Telemetrie- und Auth-Store-Ausfall, Lesen unter Schreib-Lock,
    Parallelität, Abfragepläne.
  - 120.000 Requests über 7 Tage: alle Bereiche unter 2,5 s pro Abfrage (lokal).
- **Browser-Tests** (`tests/test_monitor_dashboard_browser.py`, Playwright mit Edge):
  - alle Seiten bei 1440, 768 und 375 px, Deutsch und Englisch, hell und dunkel, ohne
    horizontales Scrollen;
  - Zeitraum- und Client-Filter, Live/Pause, verborgener Tab, Netzwerkfehler, Sessionende,
    leere Installation, unvollständige Daten;
  - keine JS-Fehler und keine CSP-Verstöße.

### 15.7 Offene Punkte nach M4

- **Tokens/s** werden nur aus Rohdaten berechnet; für Rollup-Zeiträume ist der Wert „nicht
  verfügbar“, weil Rollups bekannte Tokens und Generierungszeiten nicht paarweise führen.
  In M5 durch Schema v3 behoben, siehe 16.3.
- **Prozessmetriken** stammen nur vom Worker, der den Sampler-Lease hält (wie in M2).
- **Exklusive Dauern** gibt es nur für die bekannte Verschachtelung `forms.inspect` ⊃
  `stirling.ocr`.
- **Lasttests unter Produktionsbedingungen** (mehrere Worker, große Datenbank auf dem Zielsystem,
  synchrone Probes im Collector) gehören zu M7.

## 16. Umsetzung Phase M5 – Ollama- und Stirling-PDF-Monitoring

Zwei weitere Bereiche: `/monitor/ollama` und `/monitor/stirling`. Ihre Daten-API
(`/monitor/api/v1/ollama`, `/stirling`) ist in [monitoring-api.md](monitoring-api.md)
beschrieben. Beide Bereiche bauen auf der M4-Abfrageschicht auf (Read-only-Verbindung,
Zeitbudget, Quellenwahl, Abdeckung) und nutzen dasselbe Polling. Neue Module:

- `system/monitoring/web/upstreams.py`,
- Seitenvorlagen `page_ollama.html` und `page_stirling.html`,
- Renderer in `dashboard.js`.

### 16.1 Was gezeigt wird – und was nicht

| Begriff | Quelle | Bedeutung |
|---|---|---|
| installiert | `/api/tags` (Statusprüfung) | Modell liegt auf dem Ollama-Server |
| geladen | `/api/ps` (Statusprüfung) | Modell ist im Speicher; **keine** Aussage über Verarbeitung |
| aktiver KI-Aufruf | DocPipe-Recorder + `live_ai_steps` | DocPipe führt gerade einen Ollama-Aufruf aus (`ai.generate`/`ai.warmup`) |
| VRAM (von Ollama gemeldet) | `size_vram` aus `/api/ps` | Speicherbelegung des Modells, **keine** GPU-Auslastung |
| GPU des DocPipe-Hosts | GPU-Provider (M2) | standardmäßig nicht konfiguriert, dann „nicht verfügbar“ |

- **Abgeleitet wird nichts.** Weder Verarbeitung aus `/api/ps` noch GPU-Auslastung aus
  `size_vram`.
- **Ollama auf anderem Host:** Liegt Ollama nicht auf dem DocPipe-Host (`ollama_location:
  remote`, aus `ollama.base_url` klassifiziert, ohne die Adresse auszugeben), weist die Seite
  darauf hin, dass Host-GPU-Werte nicht die GPU von Ollama beschreiben.
- **Keep-alive:** Der Countdown läuft im Browser ab `expires_at` der letzten Messung, korrigiert
  um die Uhrdifferenz zwischen Server und Browser (`generated_ms`). Ist der Modellstand veraltet
  (älter als drei Prüfintervalle + 30 s), gibt es keinen Countdown, sondern den Hinweis
  „veraltet“. Ist die Ablaufzeit erreicht, steht dort „Entladung nicht bestätigt“ – nie
  „entladen“.
- **Gemessen vs. gemeldet:** Von DocPipe gemessene Dauern und von Ollama gemeldete Zeiten und
  Tokens sind in Kennzahlen und Tabellen getrennt beschriftet.
- **Warm-ups** sind eine eigene Kennzahlengruppe und gehen nicht in die Aufrufstatistik ein.
- **Stirling:** Gezählt werden nur die Schritte `stirling.extract_text`, `stirling.ocr` und
  `stirling.convert`. Die native Formularanalyse und -befüllung ist keine Stirling-Nutzung.
  OCR, das aus der Formularanalyse heraus aufgerufen wird, ist Stirling-Nutzung und erscheint
  unter „Aufrufende DocPipe-Endpunkte“.

### 16.2 Laufende KI-Aufrufe

`MonitoredOllamaClient` meldet jeden `generate_structured`- und `warm_up`-Aufruf beim Recorder
an und ab: Request-ID, Client, Modell, Aufrufart (aus dem Route-Template) und Startzeit. Es
werden weder Prompt noch Antwort noch Schlüssel erfasst.

- **Client:** Er wird nur gesetzt, wenn die DocPipe-Authentifizierung ihn im Request-Zustand
  hinterlegt hat und er konfiguriert ist.
- **Mehrere Worker:** Jeder Worker schreibt beim Flush seinen Stand nach `live_ai_steps`. Die
  Seite zeigt die eigenen Aufrufe direkt und die anderer Worker aus dem Snapshot. Ist ein
  Snapshot älter als drei Flush-Intervalle + 5 s, ist der Zustand `unknown`.
- **Kosten:** etwa 1,4 µs pro Aufruf (lokal gemessen).

### 16.3 Durchsatz und Schema v3

Tokens/s werden gewichtet als Summe(Tokens) / Summe(Sekunden) berechnet. Dabei zählen nur
Aufrufe, bei denen Token-Anzahl und zugehörige Dauer (> 0) bekannt sind. Das ist die
Geschwindigkeit während der DocPipe-Aufrufe, nicht der Serverdurchsatz einschließlich Leerlauf.

Damit das auch für Rollup-Zeiträume gilt, führt die Migration v3 Paarsummen in `rollup_steps`
(siehe API-Dokumentation). Für die Migration gilt:

- **Bestehende Daten:** Sie bleiben erhalten. Bestehende Buckets werden nur nachberechnet, wenn
  ihre Rohzeilen vollständig vorhanden sind; andernfalls ist der Wert „nicht verfügbar“ bzw.
  „teilweise verfügbar“.
- **Mehrere Worker:** Die Migration läuft genau einmal (`BEGIN IMMEDIATE` mit erneuter
  Versionsprüfung).
- **Tests:** v1 → v3, v2 → v3, unvollständige Buckets, keine Doppelzählung, Begrenzung der
  Nachberechnung, vier parallel startende Worker
  (`tests/test_monitoring_migration_v3.py`).

### 16.4 Collector und Statusprüfungen – Befund und Korrektur

Untersucht wurden langsame und unerreichbare Upstreams mit echten lokalen Sockets
(Zeitlimit 1 s):

| Upstream-Verhalten | vorher | nachher |
|---|---|---|
| antwortet normal | 0,02–0,06 s | unverändert |
| Port geschlossen | ≈ 1 s je Dienst | unverändert |
| nimmt an, antwortet nie | ≈ 1 s je Dienst | unverändert |
| sendet tröpfchenweise (1 Byte alle 0,25 s) | ≈ 50 s je Dienst, **unbegrenzt**, solange gesendet wird | ≈ 1 s je Dienst |

**Ursache:** httpx wendet das Zeitlimit je Phase und je Lesevorgang an, nicht auf den ganzen
Request. Weil Statusprüfungen und Flushes denselben Collector-Thread teilen, hätte ein solcher
Upstream Flushes, Heartbeats und Live-Snapshots beliebig lange verzögert. Die Folgen wären
gewesen:

- volllaufender Puffer, danach verworfene Requests,
- Abdeckungslücken,
- veraltete Live-Ansichten.

**Korrektur (minimal):** `probes.timeout_seconds` ist jetzt eine Gesamtfrist pro Request
(Verbindung und alle Lesevorgänge).

**Ergebnisse nach der Korrektur** (`tests/test_monitoring_probe_performance.py`):

- **Flush-Verzögerung:** begrenzt auf Flush-Intervall + Schleifentakt (0,5 s) +
  Stirling-Frist + Ollama-Frist. Mit den Standardwerten sind das höchstens etwa 5 + 0,5 + 3 + 3
  s. Den Anteil von Stirling- und Ollama-Frist misst der Test.
- **Live-Snapshots:** Die Stale-Schwelle beträgt drei Flush-Intervalle + 5 s, also 20 s, und
  wird damit nicht erreicht.
- **Puffer und Verluste:** 100 API-Requests während einer hängenden Prüfung – nichts verworfen,
  alles gespeichert.
- **API-Wirkung:** Antwortzeiten der API unverändert, weil der Store-Lock während der
  HTTP-Prüfung nicht gehalten wird. Ein paralleler Flush dauert unter 0,5 s.

**Nicht umgesetzt (Vorschlag, braucht Freigabe):** Statusprüfungen in einen eigenen Thread
verlegen. Dann hinge die Flush-Verzögerung gar nicht mehr von Upstreams ab. Das ist eine
Architekturänderung des Collectors und wurde deshalb in M5 nicht vorgenommen.

### 16.5 Tests

- **API** (`tests/test_monitor_upstreams_api.py`):
  - Zugriffsmatrix inklusive Kunden-Keys und abgelaufener Session;
  - Validierung, SQL-Injection und XSS über Modellnamen;
  - gewichteter Durchsatz gegen Rohdaten, Rollup gleich Rohdaten bei vollständigen Buckets;
  - Warm-ups getrennt, Filter;
  - installierte und geladene Modelle, veralteter Stand, fehlende Felder älterer
    Ollama-Versionen;
  - Offline-Verlauf;
  - parallele echte KI-Aufrufe über den Proxy, laufende Aufrufe anderer Worker;
  - keine Upstream-Aufrufe, GPU nicht verfügbar, Standortklassifizierung, Telemetrie-Ausfall.
- **Browser** (`tests/test_monitor_upstreams_browser.py`):
  - beide Seiten bei 1440, 768 und 375 px, Deutsch und Englisch, hell und dunkel;
  - Filter, Live/Pause, Countdown;
  - Offline, veralteter Stand, Netzwerkfehler, leere Installation;
  - keine JS-Fehler und keine CSP-Verstöße.

### 16.6 Offene Punkte nach M5

- **Echte GPU-Werte:** Es gibt weiterhin keinen konfigurierten GPU-Provider. Liegt Ollama auf
  einem anderen Host, wären dessen GPU-Werte ohnehin nur dort messbar.
- **Statusprüfungen im eigenen Thread:** siehe 16.4, nur nach Freigabe.
- **Bestehende Pre-v3-Buckets** ohne vollständige Rohdaten bleiben dauerhaft „teilweise
  verfügbar“, bis sie durch die Retention entfallen.

## 17. Phase M7 – Qualitätssicherung, Stabilisierung, Release-Vorbereitung

Stand: Release-Kandidat auf `feature/monitoring-dashboard`. **Nicht** gemergt, nicht
getaggt, nicht deployt. Die spätere Installation beschreibt
[monitoring-operations.md](../operations/monitoring-operations.md).

### 17.1 Entscheidung: M6 (Live-Logs) entfällt

Der Betreiber hat entschieden, auf eine zusätzliche Logging-Infrastruktur zu
verzichten. Konsequenzen:

- Es gibt **keinen Log-Viewer**. Der geplante Menüpunkt „Logs“ ist entfernt;
  `meta.sections` führt ihn nicht mehr.
- Es entstehen **keine neuen Logs** nur für das Monitoring, kein Log-Collector,
  kein Import nativer Ollama- oder Stirling-Logs, keine doppelte Speicherung und
  keine zusätzlichen Berechtigungen.
- **Erhalten bleiben:**
  - die strukturierten Ereignisse (Tabelle `events`);
  - das Security-Audit (Bereich Security);
  - die technischen Fehlerinformationen in den Bereichen, in denen sie
    entstehen: Fehlercodes in Live Activity und Performance, Ergebnisse und
    Statusprüfungen in Ollama bzw. Stirling, Datenqualität auf jeder Seite.
- Das reguläre DocPipe-Request-Log ist unverändert.
- **AI Connectors** bleibt als „geplant (#2)“ markiert. Issue #7 (Client-
  Administration, API-Key-Lebenszyklus) ist zurückgestellt und hat die
  niedrigste Priorität; #4 und #2 gehören nicht zu diesem Release.
- Das Monitoring bleibt strikt **read-only**.

### 17.2 Funktionsreview

| Bereich | Navigation, Live, Filter | Daten- und Statuszustände | Geprüft durch |
|---|---|---|---|
| Übersicht | Zeitraum, Live/Pause, Hintergrund-Pause | Lücken, unvollständig, Schätzung, leer, GPU nicht verfügbar | `test_monitor_dashboard_api`, `test_monitor_dashboard_browser` |
| Live Activity | Zeitraum, Client, Dienst, Status, Pagination | veraltete Snapshots anderer Worker | dto. |
| Projects & Clients | Zeitraum, Vorperiode | nicht authentifizierte Anfragen getrennt | dto. |
| Performance | Client, Dienst, Schrittverschachtelung | exakt vs. Histogramm | dto. |
| Server | Zeitraum | Quelle, Status, Grund je Messwert | dto. |
| Ollama | Modell, Client, Aufrufart, Status | installiert/geladen/aktiv, veraltet, offline, Paarsummen teilweise | `test_monitor_upstreams_api`, `test_monitor_upstreams_browser` |
| Stirling PDF | Client, Operation, Status | offline, Fehler, keine Daten | dto. |
| Security | Kategorien, Pagination | eigene Sessions, Audit | `test_monitor_dashboard_api` |
| alle | Sessionablauf (Polling verlängert nicht, 401 → Anmeldung) | DE/EN, hell/dunkel, 320–1440 px | `test_monitor_web_auth`, `test_monitor_visual_release` |

Entfernt wurden nur der Logs-Platzhalter und die M6-Verweise. Neue Funktionen
gibt es in M7 nicht.

### 17.3 Sicherheitsreview

| Anforderung | Ergebnis | Nachweis |
|---|---|---|
| Argon2id, verpflichtendes TOTP | Produktionsparameter; ohne zweiten Faktor keine Session | `test_monitor_web_config::test_password_hashes_use_production_argon2id_parameters`, `test_monitor_web_auth` |
| TOTP-Replay-Schutz | Code nur einmal gültig, atomar über Worker hinweg | `test_totp_code_cannot_be_reused`, `test_parallel_mfa_requests_with_the_same_code_succeed_once`, `test_replay_check_is_atomic_across_store_connections` |
| Sessions, Rotation, Logout | `__Host-`-Cookies (Secure, HttpOnly, SameSite=Strict), keine Session-Fixierung, serverseitiges Logout, Begrenzung pro Benutzer | `test_session_fixation_is_impossible`, `test_logout_invalidates_the_session_server_side`, `test_sessions_per_user_are_limited` |
| Idle- und absolute Timeouts | Polling verlängert nicht; absolute Grenze hält | `test_idle_timeout_and_polling_does_not_extend_it`, `test_absolute_timeout_ends_even_an_active_session` |
| CSRF, Origin | sessiongebundenes Token, fremder Origin wird abgelehnt | `test_monitor_web_protection` |
| XSS | nur `textContent`, Autoescape, Modellnamen bereinigt | `test_pages_have_no_inline_code_…`, `test_manipulated_model_names_are_never_returned_raw` |
| SQL-Injection | nur parametrisiertes SQL, Filter gegen Registry bzw. Konfiguration validiert | `test_filters_are_validated` (M4/M5) |
| CSP | `default-src 'none'`, kein Inline-Code, kein CDN; im Browser keine Verstöße | `test_monitor_visual_release` (alle Seiten) |
| HTTPS-Pflicht | Standard; Ausnahme nur bei reiner Loopback-Allowlist | `test_web_interface_requires_https_by_default`, `test_local_development_without_https_on_loopback_only` |
| Trusted Proxies | Header nur von `trusted_proxies`, rechtester nicht vertrauter Eintrag, fehlerhafte Header werden abgelehnt (fail-closed) | `test_monitor_web_netaccess` |
| IP-Allowlist | jeder `/monitor`-Pfad außerhalb abgelehnt | `test_every_monitor_path_is_denied_outside_the_allowlist` |
| Login-Rate-Limits | IP- und Benutzersperre, Argon2-Begrenzung, über Worker geteilt | `test_brute_force_*`, `test_rate_limits_are_shared_between_worker_processes` |
| Abfragebegrenzung | 3-s-Zeitbudget, 120 Punkte, `api_requests_per_minute` | `test_monitor_dashboard_api` |
| Kunden-API-Keys | **keine** Monitoring-Route mit irgendeinem Kunden-Key, in jeder Header-Form, auch mit gefälschten Cookies | **neu:** `test_monitor_route_matrix` (alle Routen aus der App-Registry) |
| Getrennte Auth-Datenbank | eigene Datei, fail-closed, eigene Größengrenze | `test_monitor_web_failures` |
| Dateirechte und Eigentümer | Benutzerdatei 0600 und Eigentümer = Prozess-UID; Datenbanken 0600 (auch `-wal` und `-shm`, unter Linux geprüft) | `test_users_file_with_too_wide_permissions_is_rejected`, **neu:** `test_users_file_of_another_owner_is_rejected`, `test_restored_files_get_private_permissions` |
| Geheimnisbereinigung | siehe §17.4 | `test_monitor_privacy_release` |
| Abhängigkeiten | `pip-audit` gegen `requirements.txt` und gegen die tatsächlich im Image installierten Pakete: keine bekannten Schwachstellen (Stand 28.11.2026). Die vendorten Bibliotheken Bootstrap 5.3.3 und Chart.js 4.4.7 deckt `pip-audit` nicht ab; dafür sind keine Sicherheitsmeldungen bekannt (Annahme nach Kenntnisstand, nicht automatisiert geprüft) | §17.10 |
| H1, H2, H5 | Tests erneut grün | `test_api_key_constant_time`, `test_access_log_hardening`, `test_uvicorn_proxy_header_rewriting_is_detected` |

**Offene Risiken aus M3** (Bewertung in [hardening-backlog.md](../security/hardening-backlog.md#bewertung-zum-release-kandidaten-phase-m7)):

- **Lockout-DoS:** in `allowlist` niedrig, in `public_mfa` mittel. `public_mfa`
  ist deshalb **nicht** für die Produktion freigegeben.
- **Uvicorn-Start:** Das Image ist korrekt. Ein überschriebener Startbefehl ist
  ein Prüfpunkt der Installation; andere Launcher werden nicht technisch erkannt.
- **Eigentümer und Dateirechte unter Linux:** geprüft, inklusive des neuen
  Eigentümertests. Ein bereits vorhandenes Datenverzeichnis behält seine Rechte;
  Anforderung 0700 in der Betriebsanleitung.

Keine bestehende Sicherheitsmaßnahme wurde gelockert.

### 17.4 Datenschutzprüfung

Neu: `tests/test_monitor_privacy_release.py`. Kanarienwerte stecken in:

- Dokumenten, Dateinamen, Headern, Query-Strings;
- Prompts, KI-Antworten, Assistentenfragen, Formular-Passwörtern;
- unbekannten Pfaden;
- dem konfigurierten Upstream-Host, gegen den echte Statusprüfungen fehlschlagen;
- dem Passwort eines Operators.

Danach darf keiner dieser Werte auftauchen, ebenso wenig Session-Token,
TOTP-Secret, Passwort-Hash, Kunden-Keys, der Stirling-Key oder „Bearer“. Geprüft
werden:

- die **Rohbytes aller Dateien** im Datenverzeichnis (`monitor.db`,
  `monitor-auth.db`, jeweils `-wal` und `-shm`), auch UTF-16;
- **jede Daten-API-Antwort** (alle Bereiche, 1 h, 24 h, 7 d) und jede HTML-Seite;
- die **Log-Ausgabe**.

Zusammen mit den bestehenden Tests (`test_monitoring_security`,
`test_monitor_web_failures::test_no_secrets_in_audit_logs_html_or_api`) gilt:
**In keiner Datei, keiner API-Antwort, keiner Seite und keinem eigenen Log-Eintrag von DocPipe taucht einer der Werte auf**.

### 17.5 Performance- und Lasttests

Methodik: [loadtest/README.md](../../loadtest/README.md). **Lokale,
synthetische Messungen, keine Produktionsmessungen.**

- **Testrechner:** Notebook mit Intel Core i7-7600U (2 Kerne, 4 Threads), WSL2
  mit 4 vCPU und 7,7 GiB RAM, Docker Engine 29.1.3.
- **DocPipe-Container:** `--cpus 2 --memory 1g`; Lastgenerator `--cpus 1.5`.
- **Upstreams:** Fake über HTTP (KI 150 ms ±20 %, PDF 40 ms ±20 %).

**Überhead, sequenziell** (1 Worker, je 3 abwechselnde Wiederholungen,
Mittelwerte; 60 s Leerlauf, dann je 30 s `health` bzw. `extract` mit
Parallelität 1):

| Variante | CPU im Leerlauf (% eines Kerns) | RSS im Leerlauf | health p50 / p95 / p99 | extract p50 / p95 / p99 |
|---|---|---|---|---|
| aus | 0,88 | 85,4 MiB | 3,6 / 7,6 / 14,2 ms | 91,6 / 99,6 / 100,6 ms |
| Erfassung | 0,68 | 89,3 MiB | 3,3 / 6,1 / 11,4 ms | 91,6 / 99,8 / 101,9 ms |
| Erfassung + Web | 1,13 | 94,3 MiB | 4,6 / 10,6 / 16,5 ms | 92,6 / 100,1 / 102,5 ms |

**In-Process** (`python -m system.tools.monitoring_overhead` im Container mit
denselben Grenzen, 3 Läufe):

- Monitoring-Codepfad: 18–28 µs pro Request.
- Ende-zu-Ende über den ASGI-Stack: Median +0,15 bis +0,30 ms.
- Flush von 5.000 gepufferten Requests: 46–97 ms.

**Bewertung gegen die Ziele:**

| Ziel | Ergebnis |
|---|---|
| < 1 ms zusätzliche Latenz (p99) | **In-Process erfüllt.** Codepfad ≤ 28 µs, Ende-zu-Ende-Median ≤ 0,3 ms. **Auf HTTP-Ebene nicht belastbar nachweisbar:** Zwischen identischen Läufen schwankt p99 auf diesem Notebook um ±5 ms (health: Erfassung sogar 2,8 ms *besser* als aus). Bei `extract` liegt p99 um +1,3 ms (Erfassung) bzw. +1,9 ms (Web) höher, p95 nur um +0,3 bzw. +0,5 ms. Das liegt innerhalb der Streuung, ist aber kein Nachweis für < 1 ms. |
| < 1 % CPU im Leerlauf | **Erfüllt.** −0,20 bzw. +0,25 Prozentpunkte gegenüber „aus“; die Grundlast (Collector-Takt 0,5 s, Probes, Sampler) liegt innerhalb der Messstreuung. |
| < 30 MiB RSS (Foundation) | **Erfüllt.** Erfassung +3,9 MiB im Leerlauf bzw. +5,8 MiB nach Last. Web und Auth separat: +9 MiB im Leerlauf bzw. +24 MiB nach Last mit Dashboards (1 Worker). Bei 4 Workern: Erfassung +17 MiB (≈ 4,3 MiB pro Worker), Web +48 MiB (≈ 12 MiB pro Worker). |

**Last: 50 gleichzeitige Requests**, gemischt health/extract/analyze, 60 s,
Mittel aus 3 Wiederholungen:

| Variante | Worker | Durchsatz | p50 | p95 | p99 | Dashboard p95 | RSS nach Last |
|---|---|---|---|---|---|---|---|
| aus | 1 | 77,9 rps | 423 ms | 2043 ms | 3167 ms | – | 96,7 MiB |
| Erfassung | 1 | 84,2 rps | 396 ms | 1852 ms | 2902 ms | – | 102,5 MiB |
| Erfassung + Web, 3 Sitzungen | 1 | 80,5 rps | 417 ms | 1972 ms | 3140 ms | 411 ms | 121,1 MiB |
| aus | 4 | 90,9 rps | 344 ms | 1748 ms | 2828 ms | – | 396,1 MiB |
| Erfassung | 4 | 93,2 rps | 342 ms | 1734 ms | 2772 ms | – | 413,4 MiB |
| Erfassung + Web, 3 Sitzungen | 4 | 100,4 rps | 318 ms | 1568 ms | 2498 ms | 154 ms | 444,6 MiB |

- **Sättigung:** 50 parallele Requests sättigen die 2 CPUs des Containers
  (health p50 > 200 ms). Die Absolutwerte beschreiben also diese Sättigung,
  nicht DocPipe im Normalbetrieb.
- **Streuung:** Zwischen identischen Läufen streut der Durchsatz um ±10 %
  (z. B. Erfassung, 4 Worker: 81–100 rps). Ein Nachteil durch Monitoring ist
  innerhalb dieser Streuung **nicht messbar**; die Varianten mit Monitoring
  liegen teils sogar vorn.
- **Verbindungsabbrüche:** 0–5 `ReadError` pro Lauf (≤ 0,1 %), in **allen**
  Varianten einschließlich „aus“. Das ist ein Keep-alive-Wettlauf zwischen
  Lastgenerator und Uvicorn unter Sättigung, kein Monitoring-Effekt.
- **Vollständigkeit** in allen Läufen, auch mit 4 Workern und
  Dashboard-Sitzungen:
  - gespeicherte Requests = gesendete Requests + 2–8 Health-Checks des
    Testaufbaus;
  - 0 verworfen, 0 Schreibfehler;
  - keine SQLite-Sperrfehler.
- **Viele Dashboard-Sitzungen:** 4 Worker, 50 API-Requests parallel, 8
  Sitzungen fragen alle 0,25 s ab:
  - 1.308 Dashboard-Abfragen in 60 s, alle mit 200;
  - p95 je Seite 136–266 ms;
  - API-Durchsatz 93,7 rps, innerhalb der Streuung von „Erfassung + Web“.

**Störungen:**

| Szenario | Ergebnis |
|---|---|
| Statusprüfungen tröpfeln, hängen oder brechen ab (50 parallele Requests, Prüfung alle 10 s) | 0 verworfen, 0 Schreibfehler, alle Requests gespeichert. Flushes verzögern sich durch die Prüffristen (9 statt ≈ 15 Heartbeats in 70 s); größte Lücke ≈ Flush-Intervall + 0,5 s + 2 × 3 s Frist ≈ 11,5 s, unter der Stale-Schwelle von 20 s. Ereignisse `service_unreachable` und `service_recovered` vorhanden. |
| Upstream 20 s komplett weg (4 Worker) | 162 Requests mit 502/504 (korrekte Fehler der Kunden-API), 161 `upstream_error`-Ereignisse; Monitoring vollständig. |
| Puffer übergelaufen (`buffer_max_requests: 100`, Flush alle 30 s, ≈ 206 rps) | 200 gespeichert + 8.043 gezählte Verluste = alle gesendeten Requests; Ereignis `buffer_overflow`. Nichts geht stillschweigend verloren. |
| Speicherdruck (Datenbank 24,3 MiB, Grenze 16 MiB, Wartung alle 10 s) | Nach der Korrektur unten: Schreibpause sichtbar (`storage_limit_reached`), Pruning (`storage_pressure`), Datei 24,3 → 20,9 MiB in 60 s, nach der Last 15,8 MiB; Schreibzugriffe laufen wieder, gepufferte Requests werden nachgeschrieben (0 Verluste). |

**Wachstum der Datenbank:** ≈ 350–460 Byte pro Request in der Hauptdatei
(Rohdaten, Rollups, Indizes; gemessen bei 6.000 bzw. 56.000 Requests).
Beispiel: 10.000 Requests pro Tag bei 7 Tagen Rohdaten-Retention ergeben
≈ 25–35 MiB, dazu wenige MiB Rollups. Die WAL wächst zwischen den
Checkpoints der Wartung um einige MiB (im Lasttest bis 11 MiB). Standardgrenze:
512 MiB für alles zusammen.

**Rückweg unter die Speichergrenze:** Wartung im Standardintervall (60 s) mit
10 % der Rohzeilen pro Lauf. Liegt eine Datenbank deutlich über einer
nachträglich gesenkten Grenze, dauert der Rückweg einige Minuten. In dieser
Zeit puffern die Worker; bei sehr hoher Last zählen sie Verluste.

#### Befund aus dem Lasttest: Speichergrenze wurde nie wieder verlassen

`PRAGMA incremental_vacuum(N)` gibt Seiten nur frei, während das Ergebnis
durchlaufen wird. Mit `conn.execute(...)` ohne `fetchall()` gab SQLite genau
**eine** Seite pro Aufruf frei. Folgen:

- Gelöschte Zeilen ließen die Datei nicht schrumpfen.
- Eine Telemetrie-Datenbank, die `storage.max_bytes` einmal erreicht hatte,
  blieb **dauerhaft** für Schreibzugriffe gesperrt (seit M2). Fail-open: Die
  Kunden-API war nie betroffen, die Datenqualität zeigte die Lücke.

Der bisherige Test hatte die Dateigröße gemockt. **Korrektur:** `fetchall()` an
beiden Stellen (Retention, Speicherdruck). Neuer Test
`test_storage_limit_is_left_again_after_real_pruning` mit echten Dateigrößen;
ohne die Korrektur schlägt er mit 18,6 MB > 16 MiB fehl.

#### Befund aus dem Lasttest: gleichzeitiger Start mehrerer Worker

Mit `web` und 4 Uvicorn-Workern auf einem **leeren** Datenverzeichnis zeigte ein
Worker die Oberfläche als gesperrt (503, `auth_store_unavailable`). Ursache waren
zwei Fehler, die nur beim ersten Start mehrerer Prozesse gleichzeitig auftreten:

1. **WAL-Umschaltung:** `PRAGMA journal_mode = WAL` einer frischen Datei braucht
   eine exklusive Sperre. SQLite ruft dafür den Busy-Handler nicht immer auf, der
   Fehler „database is locked“ kommt sofort. Reproduziert mit 8 gleichzeitig
   öffnenden Verbindungen, je 20 Runden:
   - Auth-Datenbank: in 14 von 20 Runden;
   - Telemetrie-Datenbank: in 11 von 20 Runden.

   Folge bei der Telemetrie: Der betroffene Worker lief ohne Datenerfassung
   (fail-open, API unbeeinträchtigt). Folge bei der Auth-Datenbank: Seine
   Oberfläche war gesperrt (fail-closed).
2. **Auth-Migration:** Sie prüfte die Schemaversion nicht erneut, nachdem sie die
   Schreibsperre erhalten hatte („table meta already exists“). Die
   Telemetrie-Migration tat das seit M5 bereits.

**Korrektur (minimal):**

- `system/monitoring/sqlite_util.enable_wal` wiederholt die Umschaltung mit
  begrenztem Backoff innerhalb des vorhandenen Busy-Timeouts; andere Fehler
  bleiben unverändert.
- Die Auth-Migration prüft die Version erneut unter der Sperre.

**Ergebnis:**

- 0 von 20 Runden mit Fehlern, für beide Datenbanken.
- Im Container: dreimal 4 Worker frisch gestartet, jeweils 12 von 12
  Login-Seiten erreichbar.
- Neue Tests: `test_concurrently_starting_workers_open_a_fresh_auth_database`,
  `test_concurrent_workers_create_a_fresh_database`. Ohne Korrektur schlagen
  sie fehl: der Auth-Test in 4 von 5 Läufen, der Telemetrie-Test als Wettlauf in
  2 von 3 Läufen. Mit Korrektur gab es keinen Fehler (je 3 Läufe sowie 20
  Probe-Runden).

### 17.6 Datenbanken und Migrationen

| Prüfung | Ergebnis | Nachweis |
|---|---|---|
| v1 → v3, v2 → v3 | Daten unverändert, Paarsummen exakt rekonstruiert | `test_monitoring_migration_v3` |
| mehrere startende Worker | genau eine Migration | dto. (4 Worker parallel); unter Last mit 4 Uvicorn-Workern im Container |
| historische Rollups, Vollständigkeit | unvollständige Buckets bleiben „nicht verfügbar“, keine Doppelzählung | dto., `test_monitor_upstreams_api` |
| Speichergrenzen, WAL, Retention | Pressure-Pruning und Schreibpause sichtbar, Auth-DB unberührt | `test_monitoring_store`, Lasttest `pressure` |
| Trennung Telemetrie/Auth | getrennte Dateien, getrennte Grenzen; die Migration öffnet die Auth-DB nicht | `test_auth_database_is_not_touched` |
| fail-open / fail-closed | Telemetrie-Ausfall: API läuft (503 nur in der Daten-API); Auth-Ausfall: Oberfläche gesperrt | `test_telemetry_outage_returns_503_…`, `test_monitor_web_failures` |
| Backup und Wiederherstellung | Online-Backup während Schreibzugriffen, Integrität, Schema, Sessions, Rechte | **neu:** `test_monitor_backup_restore`; Vorgehen in der Betriebsanleitung §8 |

### 17.7 Regression HubDix und PDF4Fill

Neu: `tests/test_monitoring_regression_release.py`. Dieselbe Folge von Requests
läuft gegen drei Konfigurationen: Monitoring aus, nur Datenerfassung, Erfassung
plus Weboberfläche. Die Antworten (Status, Content-Type, normalisierter Body)
müssen identisch sein. Abgedeckt sind:

- **HubDix:**
  - `extract-text` inklusive OCR-Fallback und Upstream-Ausfall;
  - `convert-to-pdf`, `analyze`, `warmup`;
  - Prompt Lab (Liste, Laden, Analyse);
  - Assistent;
  - Berechtigungen: fehlender Dienst, gesperrter, unbekannter oder fehlender Key.
- **PDF4Fill:**
  - `forms/inspect` mit Erkennung und OCR (Formular, Scan-ähnlich, Namen ohne
    gültiges UTF-8);
  - `forms/fill` mit diesen Namen, falscher Hash;
  - Dienst deaktiviert, „forms only“-Client.
- **Allgemein:** unbekannte Routen. Ohne Monitoring gibt es keine Dateien, kein
  Monitoring-Objekt und `/monitor` ergibt 404.

Die bestehende Gesamtsuite (HubDix, PDF4Fill, Konfiguration) läuft unverändert
grün (§17.9).

**Nicht verifiziert:** der vollständige PDF4Fill-Workflow auf dem
Produktionssystem. Er ist ein Pflichtschritt vor der Freigabe
(Betriebsanleitung §3.1).

### 17.8 Browser und visuelle Abnahme

Neu: `tests/test_monitor_visual_release.py`. Geprüft werden alle 8 Seiten bei 320,
375, 768 und 1440 px, jeweils Deutsch und Englisch, hell und dunkel (128
Ansichten):

- keine horizontale Überbreite, kein abgeschnittener Text;
- keine JS-Fehler, keine CSP-Verstöße;
- WCAG-Kontrast für Text (AA: 4,5:1 bzw. 3:1 bei großer Schrift);
- Tastaturbedienung: Navigation, Filter, Live/Pause und Menü auf dem Telefon
  erreichbar und bedienbar, sichtbarer Fokus;
- `prefers-reduced-motion`: keine Diagramm-Animationen und keine CSS-Übergänge.

Ergebnis: Zunächst wurden zwei Kontrastprobleme gefunden: im Dunkelmodus die Kopfzeilen-Buttons mit 3,3:1, im Hellmodus „nicht verfügbar“-Markierungen mit 3,1:1. Dazu waren die Diagramme auf Telefonen zu niedrig, weil umbrechende Legenden kaum Platz für den Plot ließen. Alle drei Punkte sind behoben. Danach bestehen alle 11 Browser-Tests lokal (Edge), mit 157 Screenshots, davon 128 aus der M7-Matrix. Die vollständige Screenshot-Sammlung liegt im
CI-Artefakt `monitoring-screenshots`.

### 17.9 Testergebnisse

| Lauf | Ergebnis |
|---|---|
| Windows 10, Python 3.12, vollständige Suite inklusive Browser-Tests (Edge, `DOCPIPE_REQUIRE_BROWSER=1`) | 1360 bestanden, 9 übersprungen (nur POSIX-Rechte-Tests) |
| Linux (WSL Ubuntu 22.04, Python 3.12.16, ohne root, UID 1000), vollständige Suite | 1357 bestanden, 4 übersprungen (Browser-Module, dort ohne Playwright; sie laufen in der CI) |
| Ruff (`system`, `tests`, `loadtest`) | keine Befunde |
| Lokaler Docker-Build (`docker build`, Docker Engine 29.1.3) | erfolgreich, Image 269 MB; auch für alle Lasttests verwendet |
| `pip-audit` gegen `requirements.txt` und gegen `pip freeze` des Images (43 Pakete) | keine bekannten Schwachstellen (Stand 28.11.2026) |
| Secret-Scan `gitleaks` 8.24.3 (gesamte Historie des Branches, 83 Commits, und Arbeitsverzeichnis) | ein Fund, Fehlalarm: `GEHEIMKLAUSEL-4711` ist ein synthetischer Kanarienwert in `tests/test_contract_diagnose.py`, kein Zugangsdatum |

Die GitHub-CI auf dem finalen Commit ist im Draft-PR #6 dokumentiert (Job
`lint-and-test` sowie `browser-tests` mit Pflicht-Browser und
Screenshot-Artefakt).

### 17.10 Release-Voraussetzungen und blockierende Aufgaben

**Release-blockierend** (Betrieb, nicht Code):

1. **Ollama `think`-Patch:** liegt nicht im Repository; ohne nachweislich
   angewendeten Patch keine produktive Freigabe (Betriebsanleitung §2.1). Eine
   abwärtskompatible Konfigurationsoption ist als eigene Aufgabe beschrieben.
2. **PDF4Fill-Workflow** auf dem Produktionssystem mit synthetischen Formularen
   verifizieren (§17.7).
3. **Ressourcenmessung auf dem Zielsystem** nach Aktivierung der Erfassung
   (Schritt 8). Die lokalen Werte aus §17.5 ersetzen das nicht.
4. **Abhängigkeiten des Release-Images** mit `pip freeze` festhalten und prüfen
   (Versionsbereiche in `requirements.txt`).

**Nicht blockierend, dokumentiert:**

- Lockout-DoS in `public_mfa` (Modus nicht freigegeben);
- H1b, H3, H4;
- Statusprüfungen im Collector-Thread (begrenzt, §17.5);
- keine GPU-Werte ohne Provider;
- teilweise Tokens/s aus Pre-v3-Buckets.
