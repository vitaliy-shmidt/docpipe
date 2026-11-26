# Monitoring Dashboard – Architektur und Entwicklungsplan

Status (26.11.2026, Branch `feature/monitoring-dashboard`, Basis `origin/main` = `0c05f96`):

| Phase | Stand |
|---|---|
| M1 Bestandsanalyse und Konzept | **freigegeben** (26.11.2026), Entscheidungen in §11 |
| M2 Monitoring Foundation | **implementiert und lokal getestet** (§13), nicht deployt, nicht in `main` |
| M3–M7 | nicht begonnen – jeweils eigene Freigabe |

M2 enthält bewusst **keine** Browseroberfläche und **keine** Monitoring-Endpunkte.
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
| DocPipe strukturierte Ereignisse (neu, M2/M6) | Monitoring-DB | **Ja** – ausschließlich Enum-Werte und Zahlen. |
| Ollama | Host-Dienst (journald) [A] | **Nein** – aus dem Container nicht ohne Host-Mounts erreichbar; enthält ggf. Prompt-Fragmente. |
| Stirling | Container-Log / Volume [A] | **Nein** – nur über Docker-Socket; kann Dateinamen enthalten. |

Entscheidung **[E]**: Der Log-Viewer zeigt in M6 **nur strukturierte
DocPipe-Ereignisse** aus der eigenen Monitoring-DB. Native Logs von Ollama und
Stirling werden nicht eingebunden; die UI dokumentiert diese Einschränkung.

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
- **Polling verlängert keine unbeaufsichtigte Session:** Automatische Abfragen
  setzen einen Header `X-Monitor-Poll: 1` und aktualisieren die Idle-Zeit **nicht**.
  Nur echte Benutzerinteraktionen tun das, etwa Navigation, Filterwechsel oder ein
  explizites „Aktualisieren“. Ein offener, unbeobachteter Tab läuft also nach dem
  Idle-Timeout (Default 15 min) ab. Das absolute Limit (8 h) gilt immer.
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
| `GET /events?severity&service&client&q&from&to&cursor` | strukturierte Ereignisse (M6) |
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
Ollama · Stirling PDF · AI Connectors (deaktiviert) · Logs (ab M6) · Security
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

### M6 – Live-Logs
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
- Auth-Tabellen (`monitor_sessions`, `monitor_audit`, …) entstehen erst in M3 als
  Migration v2.

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
