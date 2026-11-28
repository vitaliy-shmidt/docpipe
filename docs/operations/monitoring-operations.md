# Monitoring – Installation, Betrieb, Backup und Rollback

Betriebsanleitung für das Operator-Monitoring (Issue #3, Stand Phase M7). Sie
beschreibt die **spätere, getrennt freizugebende** Installation und enthält
bewusst keine Angaben zu konkreten Servern, Pfaden, Netzen oder Zugangsdaten.
Platzhalter wie `<data_dir>` sind für die jeweilige Installation zu setzen.

Architektur und Sicherheitsmodell: [monitoring-dashboard.md](../architecture/monitoring-dashboard.md).
Daten-API: [monitoring-api.md](../architecture/monitoring-api.md).
Offene Sicherheitspunkte: [hardening-backlog.md](../security/hardening-backlog.md).

## 1. Grundsätze

- **Zwei Schalter, zwei Schritte.** Datenerfassung (`monitoring.enabled`) und
  Weboberfläche (`monitoring.web.enabled`) sind getrennt. Ein Deployment
  aktiviert nie gleichzeitig das Monitoring.
- **Verhalten bei Fehlern:** Fällt die Telemetrie aus, laufen die Kunden-API
  und DocPipe unverändert weiter (fail-open). Fehlt die Auth-Datenbank oder ist
  sie beschädigt, ist die Oberfläche gesperrt (fail-closed).
- **Betriebsmodus:** `access_mode: allowlist` (VPN bzw. IP-Allowlist) ist der
  vorgesehene Modus. `public_mfa` ist nicht als uneingeschränkt produktionsreif
  freigegeben (siehe §10).
- **Keine Kundendaten:** Gespeichert werden nur strukturierte Werte (Routen,
  Statuscodes, Dauern, Größen, Modellnamen, Fehlercodes), nie Inhalte,
  Dateinamen, Prompts, Antworten, Schlüssel oder Fehlermeldungstexte.

## 2. Release-Build

1. **Quellstand** ist ein Commit dieses Repositorys, ohne weitere Änderungen
   außer den unten genannten, ausdrücklich dokumentierten lokalen Patches.
2. **Image-Tag eindeutig und unveränderlich**, z. B.
   `docpipe:<version>-<kurz-commit>[-<patch-slug>]`. Das Tag eines laufenden
   Images wird nie überschrieben.
3. **Build-Informationen** werden als Umgebungsvariablen gesetzt. Die
   Übersicht zeigt sie an.
   - `DOCPIPE_BUILD_COMMIT=<commit>`
   - `DOCPIPE_IMAGE_TAG=<tag>`
   - `DOCPIPE_BUILD_LOCAL_CHANGES=true|false`
   - `DOCPIPE_BUILD_LOCAL_PATCHES=<slug,...>`, z. B. `ollama-think-false`
4. **Das bisherige produktive Image bleibt als Rollback-Image erhalten**, samt
   Tag, Compose-Stand und Konfigurationssicherung.

### 2.1 Pflichtprüfpunkt: Ollama `think`-Patch

Das derzeit produktive Image enthält eine **lokale Anpassung**, die nicht im
Repository liegt: `system/services/ollama.py` sendet `"think": False` im
Payload von `/api/generate`. Ein Build aus dem Repository allein **verliert
diese Anpassung**.

Vor jedem produktiven Build:

1. Den Patch aus der bisherigen Build-Quelle als Diff sichern und gegen den
   neuen Quellstand anwenden, ohne weitere Änderungen.
2. Nach dem Build im Image prüfen, dass der Payload `"think": False` enthält,
   z. B. `grep -n '"think"' /app/system/services/ollama.py` im Container.
3. `DOCPIPE_BUILD_LOCAL_PATCHES=ollama-think-false` setzen. Das Dashboard zeigt
   den Build dann als „modifiziert (ollama-think-false)“.
4. Funktionsprüfung mit einem synthetischen Analyse-Request. Die Antwortzeit
   darf nicht durch einen „Thinking“-Anteil auffällig steigen.

**Ohne nachweislich angewendeten Patch keine produktive Freigabe.**

**Vorschlag für eine spätere, getrennte Freigabe (nicht Teil von M7):**

- Neue Option `ollama.think` mit den Werten `null`, `true` oder `false`.
  `null` lässt das Feld weg; das ist das heutige Verhalten des Repositorys.
- Optional pro Modellprofil überschreibbar (`models.<profil>.think`).
- Gesendet wird das Feld nur, wenn ein Wert gesetzt ist.
- Bestehende Konfigurationen bleiben unverändert gültig; die Produktion setzt
  `ollama.think: false`.
- Danach entfällt der lokale Patch.

## 3. Ablauf der späteren Installation

Jeder Schritt hat eine Prüfung. Schlägt sie fehl, geht es mit dem
Rollback (§9) weiter, nicht mit dem nächsten Schritt.

| # | Schritt | Prüfung |
|---|---|---|
| 1 | **Sichern:** Compose-Datei, `config.yaml`, Runtime-Prompts, Tag bzw. Digest des laufenden Images | Sicherungen lesbar, Prüfsummen notiert |
| 2 | **Image bauen** (§2) | Build ohne Fehler, Tag eindeutig |
| 3 | **think-Patch anwenden** und prüfen (§2.1) | Patch im Image nachweisbar |
| 4 | **Neues Image mit `monitoring.enabled: false`** starten (bisherige Konfiguration, nur Image-Tag geändert) | `GET /api/v1/health` 200, Container healthy |
| 5 | **HubDix und PDF4Fill prüfen** (synthetische Dokumente) | Siehe §3.1, alles unverändert |
| 6 | **Datenerfassung aktivieren:** `monitoring.enabled: true`, `web.enabled: false`, Datenvolume (§4) | Datenbank `<data_dir>/monitor.db` entsteht (0600), API-Antworten unverändert, Log ohne Monitoring-Fehler |
| 7 | **Oberfläche aktivieren:** `monitoring.web.enabled: true`, Benutzer einrichten (§5), Reverse Proxy (§4.3) | Smoke-Test §3.2 |
| 8 | **Funktion und Ressourcen prüfen** | Datenqualität „vollständig“, CPU/RAM im erwarteten Rahmen (§7) |

### 3.1 Regression HubDix und PDF4Fill (Schritt 5)

Mit den bestehenden Client-Keys und synthetischen Dokumenten:

- **HubDix:**
  - `extract-text`;
  - `analyze` für jeden genutzten Modus;
  - Prompt-Lab-Lesezugriff;
  - `assistant/query`;
  - Warm-up.
- **PDF4Fill:**
  - `forms/inspect`, `forms/detect`, `forms/fill`;
  - OCR-Fallback;
  - ein PDF mit Feldnamen, die nicht gültiges UTF-8 sind.
- **Allgemein:**
  - Ein gesperrter bzw. unbekannter Key ergibt 401/403.
  - Deaktivierte Dienste bleiben deaktiviert.

**Hinweis:** Der vollständige PDF4Fill-Workflow auf dem Produktionssystem ist
bisher **nicht verifiziert**. Er muss vor der Freigabe mit dem PDF4Fill-Betreiber
und synthetischen Formularen durchgeführt werden.

### 3.2 Smoke-Test der Oberfläche (Schritt 7)

- **Zugang:**
  - `/monitor` von außerhalb der Allowlist ergibt 403.
  - Über HTTP ohne TLS keine Anmeldung möglich.
  - Ein Kunden-API-Key öffnet keine Monitoring-Route (401 bzw. Weiterleitung zur Anmeldung).
- **Anmeldung:**
  - Login nur mit Passwort und TOTP.
  - Derselbe TOTP-Code wird kein zweites Mal akzeptiert.
  - Logout beendet die Session.
- **Anzeige:** Die Übersicht zeigt den Build mit `ollama-think-false`.

## 4. Konfiguration

### 4.1 Datenvolume, Benutzer, Rechte

- **Ein persistentes Volume** für `monitoring.data_dir`. Es enthält
  `monitor.db` (Telemetrie) und `monitor-auth.db` (Sessions, Audit) samt `-wal`
  und `-shm`.
- **Eigentümer:** Das Verzeichnis gehört der UID/GID des Containers (Image:
  UID 1000) und hat die Rechte **0700**. Existiert es schon, übernimmt DocPipe
  vorhandene Rechte des Verzeichnisses. Die Datenbankdateien setzt DocPipe
  selbst auf 0600; `-wal` und `-shm` übernehmen das.
- **Benutzerdatei** (`monitoring.web.users_file`): Sie liegt außerhalb des
  Repositorys und außerhalb des Images, gehört dem Prozessbenutzer und hat die
  Rechte **0600**. Sonst bleibt die Oberfläche gesperrt (`users_file_owner`,
  `users_file_permissions`).
- **`config.yaml`** enthält die Kunden-Keys im Klartext (H1b). Rechte 0600 bzw.
  Read-only-Mount, nie ins Repository.
- **Mount:** `prompts.runtime_dir` bleibt das bisherige schreibbare Volume; das
  Monitoring ändert daran nichts.

### 4.2 Uvicorn und Proxy-Header

- **Das Image startet mit `--no-proxy-headers`.** Ein Compose-`command:`, der den
  Start überschreibt, muss das Flag ebenfalls setzen. Sonst sperrt DocPipe die
  Oberfläche (`uvicorn_proxy_headers`); die Kunden-API ist nicht betroffen.
- **Andere Launcher** (z. B. Gunicorn mit Uvicorn-Workern) werden nicht
  automatisch erkannt. Hier liegt die Verantwortung beim Betrieb.
- **Mehrere Worker** (`--workers N`) werden unterstützt. Telemetrie und Sessions
  liegen in SQLite (WAL) und werden von allen Workern geteilt.

### 4.3 Reverse Proxy, HTTPS, Zugang

- **HTTPS** terminiert am Reverse Proxy; `require_https: true` bleibt gesetzt.
- **Forwarded-Header:** Der Proxy hängt die echte Gegenstelle an
  `X-Forwarded-For` an und setzt `X-Forwarded-Proto: https`. Seine Adresse steht
  in `monitoring.web.trusted_proxies`.
- **Zugriffsschutz:** `/monitor` wird zusätzlich am Proxy auf VPN bzw.
  Allowlist beschränkt (Defense in Depth); `access_mode: allowlist` mit
  `allowed_networks`.
- **Proxy-Logs:** Der Proxy darf `/monitor`-Bodies nicht protokollieren
  (Login-Formulare).

### 4.4 Minimalbeispiel (Schritt 6, dann 7)

```yaml
monitoring:
  enabled: true
  data_dir: <data_dir>
  web:
    enabled: false            # Schritt 7: true
    access_mode: allowlist
    allowed_networks: ["<vpn-netz>/<präfix>"]
    trusted_proxies: ["<proxy-adresse>/32"]
    require_https: true
    users_file: <users_file>
```

Alle weiteren Werte haben sichere Standardwerte, siehe `config.example.yaml`.

## 5. Benutzer und MFA

Benutzer gibt es nur über das Betreiber-CLI, nie über die Weboberfläche:

```sh
docker compose exec -it <service> python -m system.tools.monitor_user --file <users_file> add <name>
```

1. **Passwort** über einen Prompt ohne Echo eingeben (mindestens 12 Zeichen).
   Es wird als Argon2id-Hash gespeichert.
2. **Den angezeigten TOTP-Schlüssel** bzw. die `otpauth://`-URI in eine
   Authenticator-App übernehmen. Gespeichert wird erst nach Eingabe eines
   gültigen Codes.
3. **Weitere Befehle:**
   - `list` zeigt Namen und Status, nie Geheimnisse.
   - `disable`, `enable`, `remove` verwalten Benutzer.
   - `reset-password` und `reset-mfa` beenden alle Sessions des Benutzers.
   - `revoke-sessions <name> --auth-db <data_dir>/monitor-auth.db` beendet
     Sessions sofort.
4. **Verlorenes Gerät:** `reset-mfa`. Wiederherstellungscodes gibt es bewusst
   nicht.

## 6. Betrieb

- **Datenqualität:** Jede Seite zeigt Quelle, Lücken, verworfene Anfragen
  und Schreibfehler. „Unvollständig“ heißt: Für diesen Zeitraum fehlen Daten.
  Lücken werden nie als 0 gezeigt.
- **Retention:** Rohdaten 7 Tage, Minuten-Rollups 14 Tage, Stunden-Rollups
  400 Tage, Audit 90 Tage (Standard). Die Wartung läuft im Collector, einmal
  pro Minute.
- **Speichergrenze:** `storage.max_bytes` (Standard 512 MiB) gilt für die
  Telemetrie-DB samt WAL und SHM.
  - Ab 90 % entfernt DocPipe die ältesten Rohdaten (Ereignis
    `storage_pressure`); Rollups bleiben erhalten.
  - Ist die Grenze erreicht, pausieren die Schreibzugriffe (Ereignis
    `storage_limit_reached`). Die Kunden-API läuft weiter, die Datenqualität
    zeigt die Lücke.
  - Die Auth-Datenbank hat eine eigene Grenze (`auth_db_max_bytes`) und wird
    nie automatisch geleert.
- **Sessions:** Idle-Timeout 15 min, absolut 8 h. Dashboard-Polling
  verlängert die Sitzung nicht. Höchstens 3 Sitzungen pro Benutzer.
- **Monitoring abschalten:** `monitoring.web.enabled: false` bzw.
  `monitoring.enabled: false`, dann Neustart. Daten und Sessions bleiben
  erhalten, gelöscht wird nichts.

## 7. Was gemessen wird – und was nicht

| Wert | Status |
|---|---|
| Requests, Dauer, Statuscodes, Clients, Schritte (Stirling, KI, Formulare) | gemessen von DocPipe |
| Ollama-Zeiten und -Tokens | von Ollama gemeldet, pro Aufruf |
| Tokens/s | gewichtet aus gültigen Paaren; nicht der Serverdurchsatz einschließlich Leerlauf |
| Laufende KI-Aufrufe | nur von DocPipe ausgelöste Aufrufe |
| Installierte und geladene Modelle | aus GET-Statusprüfungen (`/api/tags`, `/api/ps`) |
| Host, Container, Prozess | aus `/proc` und cgroup v2, ohne Zusatzrechte; nicht lesbare Werte sind „nicht verfügbar“ |
| GPU-Auslastung | **nicht verfügbar**, es ist kein GPU-Provider konfiguriert. Der VRAM-Wert von Ollama ist Speicherbelegung, keine Auslastung. |
| Native Ollama- und Stirling-Logs | **nicht erfasst** (M6 entfällt, siehe Architektur §17) |

Ressourcenbedarf: Messungen in einer lokalen, synthetischen Umgebung stehen in
[monitoring-dashboard.md §17](../architecture/monitoring-dashboard.md). Das sind
**keine** Produktionsmessungen; nach Schritt 8 auf dem Zielsystem
nachzumessen.

## 8. Backup und Wiederherstellung

Beide Datenbanken laufen im WAL-Modus. Ein Kopieren der Dateien im laufenden
Betrieb kann inkonsistent sein. Deshalb die **Online-Backup-API von SQLite**
verwenden; sie funktioniert auch bei laufenden Schreibzugriffen. Das Image
enthält keine `sqlite3`-CLI, aber Python:

```sh
docker compose exec -T <service> python - <<'EOF'
import os, sqlite3
os.umask(0o077)                      # Backup-Dateien nur für den Eigentümer
for name in ("monitor.db", "monitor-auth.db"):
    src = sqlite3.connect(f"file:<data_dir>/{name}?mode=ro", uri=True)
    dst = sqlite3.connect(f"<backup_dir>/{name}")
    with dst:
        src.backup(dst)
    assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    dst.close(); src.close()
EOF
```

- **Inhalt:** `monitor-auth.db` enthält Sessions, Audit und Schlüsselmaterial
  der Sessions. Backups sind **wie Geheimnisse** zu behandeln: Rechte 0600,
  verschlüsselt, nicht in Tickets oder Repositorys.
- **Wiederherstellung:**
  1. Den Dienst stoppen.
  2. Die Hauptdateien (`monitor.db`, `monitor-auth.db`) in ein leeres
     `<data_dir>` kopieren, ohne `-wal` und `-shm`.
  3. Eigentümer UID 1000 setzen, dann den Dienst starten. DocPipe setzt beim
     Öffnen die Rechte 0600.
  4. Eine wiederhergestellte Auth-DB enthält die Sessions vom Zeitpunkt des
     Backups. Nach einer Wiederherstellung wegen eines Sicherheitsvorfalls
     `revoke-sessions` für alle Benutzer ausführen.
- **Geprüft:** Das Vorgehen ist als Test automatisiert
  (`tests/test_monitor_backup_restore.py`): Backup während laufender
  Schreibzugriffe, Wiederherstellung, Integritätsprüfung, Schema-Version,
  erhaltene Sessions und Rechte.
- **Benutzerdatei:** Sie wird mit der Konfiguration gesichert (0600).

## 9. Rollback

| Ebene | Vorgehen |
|---|---|
| Oberfläche | `monitoring.web.enabled: false`, Neustart. Keine Löschung von Sessions oder Audit. |
| Datenerfassung | `monitoring.enabled: false`, Neustart. Die Datenbank bleibt liegen, die Kunden-API ist unverändert. |
| Image | Den bisherigen Image-Tag wieder eintragen, mit der gesicherten Compose-Datei und `config.yaml`. Ein Image vor M2 kennt den `monitoring`-Block nicht und ignoriert ihn; für die Kundenfunktion ist das unkritisch, die Konfiguration sollte aber auf den gesicherten Stand zurück. |
| Telemetrie-Schema | Kein Down-Migrationspfad. Ein älteres Monitoring-Image öffnet eine neuere Datenbank nicht (fail-open: Monitoring aus, API läuft). Für ein älteres Monitoring-Image die vor dem Upgrade gesicherte `monitor.db` zurückspielen oder das Verzeichnis leer neu beginnen. |
| Auth-Datenbank | Unabhängig vom Telemetrie-Schema. Nicht löschen; bei Bedarf Sessions per CLI widerrufen. |

## 10. Bekannte Einschränkungen

Siehe [monitoring-dashboard.md §17](../architecture/monitoring-dashboard.md) und
[hardening-backlog.md](../security/hardening-backlog.md). Die wichtigsten
Punkte:

- **Lockout-DoS:** Im Modus `public_mfa` können Fehlversuche einen Benutzer
  zeitweise sperren. Im Modus `allowlist` geht das nur aus freigegebenen Netzen.
- **Andere Launcher als die Uvicorn-CLI:** Ihre Proxy-Header-Auswertung wird
  nicht technisch erkannt.
- **Statusprüfungen** laufen im Collector-Thread. Jede Prüfung hat eine
  Gesamtfrist; bei hängenden Upstreams verzögert sich der Flush um höchstens
  zwei Fristen.
