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

Der Build ist reproduzierbar und braucht **keine manuellen Quellcode-Patches**
mehr.

| Baustein | Umsetzung |
|---|---|
| Quellstand | genau ein Commit dieses Repositorys, aus einer **separaten, sauberen Arbeitskopie** (`git status` leer) |
| Basis-Image | im `Dockerfile` per Digest fixiert (`python:3.12.16-slim@sha256:…`) |
| Python-Pakete | `constraints.txt` fixiert alle 43 Pakete; das `Dockerfile` installiert `-r requirements.txt -c constraints.txt` |
| Build-Metadaten | Build-Argumente, im Image als Umgebung (Monitoring-Übersicht) und als OCI-Labels sichtbar |
| Image-Tag | eindeutig und unveränderlich, z. B. `docpipe:<version>-<kurz-commit>`; ein Tag wird nie überschrieben |

```sh
git clone <repository> docpipe-release && cd docpipe-release
git checkout <commit>                      # der freigegebene Commit
git status --porcelain                     # muss leer sein
docker build \
  --build-arg DOCPIPE_BUILD_COMMIT="$(git rev-parse HEAD)" \
  --build-arg DOCPIPE_IMAGE_TAG="<version>-$(git rev-parse --short HEAD)" \
  --build-arg DOCPIPE_BUILD_LOCAL_CHANGES=false \
  -t docpipe:<version>-$(git rev-parse --short HEAD) .
docker run --rm docpipe:<tag> pip freeze > docpipe-<tag>-packages.txt
diff <(grep -v '^#' constraints.txt | sort -f) <(sort -f docpipe-<tag>-packages.txt)   # keine Ausgabe
docker run --rm docpipe:<tag> pip check                                               # "No broken requirements"
docker image inspect docpipe:<tag> --format '{{json .Config.Labels}}'                  # Commit und Tag
```

**Aktualisierung der Abhängigkeiten:** nur gezielt, bei einem Sicherheits- oder
Funktionsgrund.

1. Den Digest im `Dockerfile` bzw. die betroffene Zeile in `constraints.txt`
   anpassen.
2. Neu bauen und `pip freeze` erneut übernehmen.
3. Die Tests laufen lassen; `tests/test_release_build.py` prüft, dass jede
   Anforderung exakt und innerhalb ihres Bereichs fixiert ist.

Die CI installiert ebenfalls mit `-c constraints.txt`, testet also genau die
Release-Versionen.

**Bit-identisch** sind zwei Builds nicht, weil Docker Zeitstempel aufnimmt.
Identisch sind Paketversionen und Inhalt; lokal mit zwei Builds ohne Cache
geprüft.

### 2.1 Ollama `think` – Konfiguration statt Patch

Das bisherige produktive Image enthielt eine **lokale Quellcode-Anpassung**
(`"think": False` fest im Payload von `system/services/ollama.py`). Sie entfällt.
Stattdessen gilt:

```yaml
ollama:
  think: false     # für die produktive Installation ausdrücklich setzen
```

- **Wirkung:** `think: false` sendet `"think": false` bei jeder strukturierten
  Generierung (Analyse, Prompt Lab, Assistent, jeweils inklusive des einen
  Reparaturaufrufs). Das entspricht dem Verhalten des bisherigen
  Produktionsimages.
- **Warm-up:** Es lädt das Modell nur und erhält das Feld nicht.
- **Ohne die Option** wird kein `think`-Feld gesendet. Das ist das Verhalten des
  unveränderten Repositorys, und kein Standardwert ändert sich.
- **Gültige Werte:** nur `true` und `false`. `"false"` in Anführungszeichen, `0`
  usw. lassen den Start mit einem Konfigurationsfehler scheitern.
- **`think: true` bei einem Modell ohne Thinking-Unterstützung** ergibt
  `ai_processing_failed`, ohne Wiederholung und ohne anderes Modell.

Details: README, Abschnitt „Ollama think“.

## 3. Release- und Rollback-Checkliste

Für den Betreiber. Jeder Schritt hat Prüfkriterien und eine Rückfallmaßnahme.
Schlägt eine Prüfung fehl, geht es mit der Rückfallmaßnahme weiter, nicht mit
dem nächsten Schritt. Konkrete Hosts, Pfade und Zugangsdaten gehören in die
Betriebsunterlagen, **nicht** in dieses Repository.

| # | Schritt | Prüfkriterien | Rückfall |
|---|---|---|---|
| 1 | **Bestehende Installation sichern:** Compose-Datei, `config.yaml`, Runtime-Prompts, Image-Tag und Digest des laufenden Images (`docker image inspect`), ggf. das Monitoring-Datenverzeichnis (§8) | Sicherungen vollständig, lesbar, Prüfsummen notiert; das bisherige Image ist lokal vorhanden und **wird nicht gelöscht** | – (nichts verändert) |
| 2 | **Aktuellen PDF4Fill-Fill-Workflow** auf der bestehenden Installation mit synthetischen Formularen prüfen (inspect → Ausfüllen → fill), dabei Ergebnisse festhalten | Formulare werden erkannt und korrekt befüllt; die Ergebnisse dienen als Referenz für Schritt 9 | Abweichungen klären, bevor ein neues Image kommt |
| 3 | **Release-Image** aus einer separaten, sauberen Arbeitskopie bauen (§2) | `git status` leer, Build fehlerfrei, eindeutiger Tag | Image verwerfen; die laufende Installation ist unberührt |
| 4 | **Client-Konfiguration und Zugangsdaten** unverändert übernehmen (gesicherte `config.yaml`) | `clients:` byte-gleich zur Sicherung (`diff`); keine neuen oder geänderten Keys | gesicherte `config.yaml` |
| 5 | **`ollama.think: false`** in `config.yaml` setzen, sonst nichts ändern | `diff` zur Sicherung zeigt nur diese Zeile (plus ggf. den `monitoring`-Block mit `enabled: false`); Startvalidierung ohne Fehler | gesicherte `config.yaml` |
| 6 | **Build-Metadaten und Paketversionen** dokumentieren: Commit, Tag, Image-Digest, `pip freeze` (§2) | `pip freeze` = `constraints.txt`, `pip check` ohne Befund; Labels mit Commit und Tag | Build wiederholen |
| 7 | **Rollback vorbereiten:** bisheriges Image (Tag und Digest) und Compose-Backup verifizieren | `docker image inspect <bisheriges Image>` erfolgreich; die gesicherte Compose-Datei startet syntaktisch (`docker compose config`) | – |
| 8 | **Neues Image ohne Monitoring** starten: nur Image-Tag ändern, `monitoring.enabled: false` bzw. kein `monitoring`-Block | Container healthy, `GET /api/v1/health` 200; Log ohne Fehler; keine Monitoring-Dateien, kein `/monitor` (404) | bisheriges Image und Compose-Datei, Neustart |
| 9 | **HubDix und PDF4Fill prüfen** (§3.1) | alle Prüfungen wie in Schritt 2 bzw. §3.1; Analyse-Antwortzeiten ohne auffälligen Anstieg (kein „Thinking“-Anteil) | wie Schritt 8 |
| 10 | **Datenerfassung** aktivieren: `monitoring.enabled: true`, `web.enabled: false`, persistentes Datenverzeichnis (§4.1) | `monitor.db` entsteht mit 0600; API-Antworten unverändert (Stichprobe wie Schritt 9); Log ohne Monitoring-Fehler | `monitoring.enabled: false`, Neustart; Datenverzeichnis bleibt liegen |
| 11 | **Ressourcenverbrauch** nach Aktivierung prüfen (z. B. `docker stats` über einige Stunden normalen Betriebs) | RSS pro Worker etwa +4 MiB; CPU im Leerlauf ohne erkennbaren Anstieg; Datenbankgröße im erwarteten Rahmen (§6) | wie Schritt 10 |
| 12 | **Geschützte Oberfläche** aktivieren: Benutzer per CLI (§5), `web.enabled: true`, `access_mode: allowlist`, `allowed_networks`, `trusted_proxies`, `require_https: true`, Reverse Proxy mit HTTPS (§4.3) | Startvalidierung ohne Fehler; Oberfläche nicht gesperrt | `monitoring.web.enabled: false`, Neustart; keine Löschung von Sessions oder Audit |
| 13 | **Funktions- und Sicherheitsprüfung** (§3.2) | Smoke-Test vollständig bestanden; Übersicht zeigt Commit und Tag des Release-Builds, Datenqualität „vollständig“ | wie Schritt 12 |
| 14 | **Ressourcen unter realen Betriebsbedingungen** erneut messen (über mindestens einen typischen Betriebstag) und mit den lokalen Referenzwerten vergleichen | Abweichungen dokumentiert und bewertet | bei Bedarf Schritt 12 bzw. 10 zurücknehmen |

**Vollständiger Rollback** (jederzeit): Bisheriges Image eintragen, gesicherte
Compose-Datei und `config.yaml` zurück, Neustart. Das alte Image kennt
`ollama.think` und `monitoring` nicht und ignoriert beides; mit der gesicherten
Konfiguration ist der Ausgangszustand wiederhergestellt. Das
Monitoring-Datenverzeichnis bleibt unangetastet (§9).

### 3.1 Regression HubDix und PDF4Fill (Schritte 2 und 9)

Mit den bestehenden Client-Keys und **synthetischen** Dokumenten:

- **HubDix:**
  - `extract-text`;
  - `analyze` für jeden genutzten Modus;
  - Prompt-Lab-Lesezugriff;
  - `assistant/query`;
  - Warm-up.
- **PDF4Fill:**
  - `forms/inspect` mit Erkennung und OCR;
  - `forms/fill`;
  - ein PDF mit Feldnamen, die nicht gültiges UTF-8 sind;
  - Bearer-Authentifizierung.
- **Allgemein:**
  - Ein gesperrter bzw. unbekannter Key ergibt 401/403.
  - Deaktivierte Dienste bleiben deaktiviert.

**Hinweis:** Der vollständige PDF4Fill-Workflow auf dem Produktionssystem ist
bisher **nicht verifiziert** (Schritt 2).

### 3.2 Smoke-Test der Oberfläche (Schritt 13)

- **Zugang:**
  - `/monitor` von außerhalb der Allowlist ergibt 403.
  - Über HTTP ohne TLS keine Anmeldung möglich.
  - Ein Kunden-API-Key öffnet keine Monitoring-Route (401 bzw. Weiterleitung zur Anmeldung).
- **Anmeldung:**
  - Login nur mit Passwort und TOTP.
  - Derselbe TOTP-Code wird kein zweites Mal akzeptiert.
  - Logout beendet die Session.
- **Anzeige:** Die Übersicht zeigt Commit und Image-Tag des Release-Builds und den Build-Status „unverändert“ (keine lokalen Patches mehr).

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

### 4.4 Minimalbeispiel (Schritt 10, dann 12)

```yaml
monitoring:
  enabled: true
  data_dir: <data_dir>
  web:
    enabled: false            # Schritt 12: true
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
**keine** Produktionsmessungen; in den Schritten 11 und 14 auf dem Zielsystem
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
