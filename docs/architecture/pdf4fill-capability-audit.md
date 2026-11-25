# PDF4Fill-Integration – Stirling Capability Audit (Phase 2.0)

Stand: 25.11.2026 · DocPipe `main` @ `c6b5397` (v0.1.0) · Status: **Audit und Vorschlag, keine Implementierung**

> **Umsetzungsstand:** Phase 2.1a (`services.forms`, `POST /api/v1/documents/forms/inspect`) und 2.1b (OCR über die vorhandene Stirling-Anbindung, Vorschläge für statische Felder, OpenAPI-Snapshot) sind auf Branch `feature/forms-inspect` umgesetzt. Server laut Betreiber: Stirling-PDF v3.2.2; dessen OCR-Endpunkt und Parameter sind gegen den Quellcode des Tags geprüft; Vertrag: [docs/forms-inspect.md](../forms-inspect.md). Präzisierung gegenüber Abschnitt 5.1: `native_ref` ist die Widget-Objektreferenz (`<objnum>.<gen>`); die gemeinsame Feldidentität mehrerer Widgets trägt `field_ref`.

Grundlage: DocPipe-Code und -Doku, PDF4Fill [ROADMAP Phase 2](https://github.com/vitaliy-shmidt/PDF4Fill/blob/main/ROADMAP.md) und [`docs/api/contracts.md`](https://github.com/vitaliy-shmidt/PDF4Fill/blob/main/docs/api/contracts.md), Stirling-PDF-Quellcode der Tags `v2.14.2` und `v3.5.0`, isolierte Tests mit synthetischen PDFs. Auf dem Server wurde nichts geändert, an DocPipe-Code und -Konfiguration ebenfalls nicht.

## 1. Ist-Zustand DocPipe

| Bereich | Befund |
|---|---|
| Architektur | FastAPI, zustandslos (keine DB, keine Queue, keine Persistenz). Uploads nur als Temp-Datei je Request (`system/routes/documents.py`). |
| Stirling-Anbindung | Ausschließlich über `system/services/stirling.py` (`StirlingClient`, `X-API-KEY`, keine Redirects, Fehlerabbildung auf DocPipe-Codes). Genutzt: `convert/pdf/text`, `misc/ocr-pdf`, `convert/file/pdf`, `convert/img/pdf`. |
| Öffentliche Endpunkte | `GET /health`, `GET /capabilities`, `POST /documents/extract-text` (mit OCR-Fallback), `POST /documents/convert-to-pdf`, `POST /documents/analyze`, Prompt Lab, Assistant. Fehlerformat `{ok:false, code, message}`. |
| PDF-Bibliotheken im Prozess | **Keine.** Alle PDF-Arbeit läuft über Stirling. |
| Stirling-Version | `docker-compose.yml` verwendet `stirlingtools/stirling-pdf:latest` (**nicht fixiert**). Der Code ist laut Kommentar gegen `v2.14.2` verifiziert. Aktuelles Release: `v3.5.0` (22.11.2026). Die auf `docpipe2.adresser.de` **installierte Version wurde nicht verifiziert**: Stirling ist von außen nicht erreichbar (gut), und ein Server-Login war für dieses Audit nicht freigegeben. |
| Staging | `https://docpipe2.adresser.de/api/v1/health` → `{"ok":true,"service":"docpipe","version":"0.1.0"}`; `/capabilities` ohne Key → 401. |
| Tests | `pytest`: **748 passed** (lokal, isolierte venv, 25.11.2026). |

## 2. Stirling-Fähigkeiten (aus dem Quellcode des Tags v2.14.2, in v3.5.0 unverändert vorhanden)

Präfixe: `misc` = `/api/v1/misc`, `security` = `/api/v1/security`, `general` = `/api/v1/general`, `analysis` = `/api/v1/analysis`. Die Formular-Endpunkte haben kein Lizenz-Gating.

| Funktion | Stirling-Endpunkt | Eignung für PDF4Fill |
|---|---|---|
| Formularfelder auslesen | `POST /api/v1/form/fields` → Name, Label, Typ, Wert, Optionen, Pflicht, `pageIndex` | Teilweise: keine Geometrie |
| Felder mit Koordinaten | `POST /api/v1/form/fields-with-coordinates` → je Widget `pageIndex` (0-basiert), x/y/Breite/Höhe, Exportwert, Schriftgröße | **Eingeschränkt**, siehe 2.1 |
| Felder füllen | `POST /api/v1/form/fill` (`file`, `data` = JSON Name→Wert, `flatten`) | **Eingeschränkt**, siehe 2.1 |
| Felder ändern/löschen/anlegen | `form/modify-fields`, `form/delete-fields`; ab v3.5.0 auch `add-fields`, `edit-fields` | Nicht benötigt |
| Formulare entsperren | `misc/unlock-pdf-forms` (entfernt ReadOnly) | Nicht verwenden (Schutzumgehung) |
| OCR | `misc/ocr-pdf` (bereits genutzt, `ocrType=skip-text`, hOCR) | **Wiederverwenden** |
| Konvertierung | `convert/file/pdf`, `convert/img/pdf` (bereits genutzt) | **Wiederverwenden** |
| Flatten | `misc/flatten` (`flattenOnlyForms`, `renderDpi`); `form/fill?flatten=true` | Nur bedingt (Stile und Unicode, siehe 2.1) |
| Bild/Stempel | `misc/add-stamp` (Text/Bild, Seitenauswahl, `overrideX/Y`, Bildhöhe = `fontSize`, Deckkraft, Rotation); `misc/add-image` (x/y, Originalgröße, erste oder alle Seiten) | Ungeeignet für „proportional in Rechteck einpassen“ |
| Passwortschutz | `security/add-password` (AES 40/128/256, Berechtigungen), `security/remove-password` | Möglich (nach MVP) |
| PDF-Info | `security/get-info-on-pdf`; `analysis/page-count`, `basic-info`, `document-properties`, `page-dimensions`, `form-fields`, `annotation-info`, `font-info`, `security-info` | Möglich, aber für Inspect nicht nötig |
| Merge | `general/merge-pdfs` | Später (Formularpakete) |
| Komprimierung | `misc/compress-pdf` | Optional später |

### 2.1 Technische Grenzen der Stirling-Formularfunktionen (`FormUtils`, v2.14.2)
1. **Rotation wird ignoriert.** Die Koordinaten sind relativ zur CropBox und y-gespiegelt (Ursprung oben links), aber ausdrücklich **ohne Rotation**. Gedrehte Seiten liefern also falsche Positionen für PDF4Fill.
2. **Dokumentation widerspricht dem Code.** Das Schema beschreibt einen Ursprung unten links, der Code liefert oben links. Das ist ein Risiko bei einem Stirling-Update (`latest`).
3. **Widgets außerhalb der Seitengrenzen werden stillschweigend verworfen** (`return null`). PDF4Fill fordert stattdessen eine Warnung.
4. **Appearances nur mit Helvetica (Standard-14, WinAnsi).** Keine eigene Schrift, Farbe, Ausrichtung, kein Auto-Fit und keine Mindestgröße. Zeichen außerhalb WinAnsi (z. B. `Ł`, Kyrillisch) sind nicht darstellbar.
5. **Kein Überlauf-Fehler**: Kein Overlay-Modus für Felder ohne native Widgets; kein Hash; keine Erkennung statischer Leerbereiche; keine Seiten-Textblöcke mit Koordinaten.
6. **Füllen nur über den Feldnamen**: Bei mehreren Widgets eines Felds lässt sich keine Position einzeln ansprechen. XFA, Verschlüsselung und Signaturen werden nicht strukturiert gemeldet.

**Folgerung:** Stirling bleibt für OCR und Konvertierung zuständig. Formular-Inspect und -Fill gehören in DocPipe in einen eigenen, im Prozess laufenden Baustein. Die Stirling-Endpunkte `form/fields*` dienen nur als Gegenprobe in Referenztests.

## 3. Zusätzliche Python-Bibliotheken

Isoliert getestet (Scratch-venv, synthetisches PDF mit Text-/Checkbox-/Radio-/Auswahlfeld, um 90° gedrehter Seite, versetzter CropBox, Feld mit zwei Widgets, AES-256-Verschlüsselung):

| Bibliothek (getestet) | Lizenz | Zweck | Ergebnis |
|---|---|---|---|
| `pikepdf` 10.18 (qpdf) | MPL-2.0 | Struktur, AcroForm/Widgets, Rotation/CropBox, XFA-/Signatur-/Verschlüsselungserkennung, Entschlüsseln mit Passwort, Overlay-Merge, Flatten von Button-Appearances | Alle 10 Widgets erkannt; Verschlüsselung (AESv3), fehlendes Öffnungspasswort und Berechtigung „Formular füllen“ zuverlässig erkannt |
| `pdfminer.six` | MIT | Seitentext, Textblöcke mit Koordinaten, Linien/Rechtecke für Feldkandidaten | Labels („Vorname:“, „Anrede“ …) mit Bounding Box |
| `reportlab` | BSD | Text-Overlay mit eingebetteter TrueType-Schrift, Auto-Fit, Farbe, Ausrichtung; PNG proportional einpassen | Unicode-Text (`Łęcka Жанна`) korrekt und extrahierbar, Auto-Fit bis Mindestgröße |
| `Pillow` | HPND | Validierung von PNG-Stempeln/Unterschriften (Format, Größe, Alpha) | Funktioniert |
| `fontTools` (Vorschlag) | MIT | Zeichenabdeckung der Schrift prüfen → Fehler `unsupported_characters` | nicht getestet |
| `pypdf` 7.1 (Vergleich) | BSD | Natives Füllen | Wert korrekt gesetzt, aber Appearance mit Helvetica: `Łęcka Жанна` → `?cka ?????` (dieselbe Klasse von Grenze wie Stirling) |

Die eigene Normalisierung (Ursprung oben links, Seite ab 1, CropBox und `/Rotate` 0/90/180/270) ist gegen die synthetischen Fälle nachgerechnet und korrekt. Beispiel 90°: unrotiertes Rechteck (120, 750, 320, 770) → x 750, y 120, B 20, H 200 auf einer 841,89 × 595,28 pt großen Seite.

**Nicht verwenden:** PyMuPDF/fitz (AGPL-3.0 oder kommerzielle Lizenz) wegen des SaaS-Einsatzes. Alle empfohlenen Pakete haben Wheels für `python:3.12-slim`; zusätzliche Systempakete sind nicht nötig. Schriften (z. B. Noto Sans, OFL) werden als freigegebene Liste mit dem Image ausgeliefert, nicht vom Host geladen.

## 4. Mandantenverwaltung und Isolation

**Ist:** Clients stehen in `config.yaml` (`clients.<id>`: `enabled`, `api_key`, `services.{documents, ai, prompt_lab, assistant}`, `model_overrides`). Authentifizierung per `Authorization: Bearer`. Die Rechte werden je Route über `require_*_service` geprüft. Laut README gibt es keine Quoten, Tarife oder Nutzungsstatistik. Es gibt nur Logzeilen mit `client_id`, und `max_file_size_mb` gilt global.

| Anforderung PDF4Fill | Ist | Lücke / Vorschlag |
|---|---|---|
| Eigener Client, getrennt von HubDix | Möglich über einen eigenen Eintrag `clients.pdf4fill` mit eigenem Key | Eintrag und Key durch den Betreiber anlegen (**nicht Teil dieses Audits**) |
| Eigene Rechte | Service-Flags | Neues, separat aktivierbares Flag `services.forms`. PDF4Fill: `documents` + `forms`; `ai` erst mit `form_question_extraction`; nie `prompt_lab`/`assistant`. HubDix erhält `forms` nicht (Standard `false`), sein Verhalten bleibt unverändert. |
| Eigene Limits | Nur global `max_file_size_mb` | Optionale technische Limits je Client in `config.yaml` (`limits.max_file_size_mb`, `max_pages`, `max_fields`, `max_images`, `requests_per_minute`, `max_concurrent`), Standard = heutiges Verhalten. **Kommerzielle Quoten bleiben in PDF4Fill** (`TenantQuotas`, ADR-003). |
| Eigene Nutzungsstatistik | Nur Request-Logzeilen | Strukturierte Metrik-Logzeile je Operation (`client_id`, `operation`, `pages`, `fields`, `input_bytes`, `output_bytes`, `status`, `duration_ms`; nie Inhalte, Namen oder Werte), auswertbar je Client. PDF4Fill führt die maßgebliche Nutzungsstatistik je Mandant selbst. Eine DocPipe-DB wird bewusst nicht eingeführt (Zustandslosigkeit bleibt). |
| Schlüsselsicherheit | Key im Klartext in `config.yaml`, Vergleich mit `==` | Empfehlung (eigene Freigabe, rückwärtskompatibel): `hmac.compare_digest` und optional `api_key_sha256` statt Klartext. |

Der Mandantenkontext von PDF4Fill (eigene Mandanten) wird DocPipe nicht übergeben. DocPipe sieht nur den Client `pdf4fill`. Die Trennung der PDF4Fill-Mandanten untereinander bleibt vollständig in PDF4Fill. Das passt zur Zustandslosigkeit: DocPipe speichert nichts und kann deshalb nichts zwischen Mandanten vermischen.

## 5. Architekturvorschlag

```text
PDF4Fill ──Bearer(pdf4fill)──▶ DocPipe routes/forms.py ──▶ require_forms_service + client limits
                                        │
                     ┌──────────────────┼──────────────────────────────┐
                     ▼                  ▼                              ▼
              forms/inspect.py    forms/fill.py                 services/stirling.py (vorhanden)
              pikepdf + pdfminer  pikepdf + reportlab + Pillow  ocr_pdf() / convert_to_pdf()
                     └──── forms/geometry.py (CropBox/Rotation → oben links, Seite ab 1)
                           forms/security.py (XFA, Verschlüsselung, Signaturen, Berechtigungen)
                           forms/fonts.py   (Schriftliste, Zeichenabdeckung)
```

**Wiederverwendung:** `_spool_upload_to_temp_file`, `_assert_is_pdf`, Größenlimit, Fehlerformat `DocPipeError`, Logging-Muster mit `request_id`/`client_id`, `/capabilities`, `StirlingClient.ocr_pdf` und `convert_to_pdf`, `text_quality.is_text_sufficient` für die Entscheidung über OCR. Bestehende Endpunkte und Verträge bleiben unverändert; alles ist additiv.

### 5.1 `POST /api/v1/documents/forms/inspect`
Multipart `file` (PDF), optional `password` (nur im Body). Ablauf:
1. Upload in eine Temp-Datei; SHA-256 der Originalbytes berechnen.
2. Mit `pikepdf` öffnen → `password_required` / `invalid_password` / `permission_denied`. XFA ergibt die Warnung `xfa_present`; reines dynamisches XFA ohne AcroForm ergibt `xfa_unsupported`.
3. Seiten: `width`/`height` der sichtbaren Seite nach Rotation, `rotation`, `text_source` (`embedded`/`ocr`/`none`).
4. Text und Textblöcke mit `pdfminer.six`. Bei zu wenig Text → bestehende Stirling-OCR → Textblöcke aus dem OCR-PDF. Übernahme nur, wenn Seitenzahl und Seitengrößen identisch sind, sonst die Warnung `ocr_geometry_mismatch` ohne Positionen.
5. Native Felder: je Widget ein Eintrag mit `id`, `name` (voll qualifiziert), `type` (`text`/`checkbox`/`radio`/`choice`/`signature`), `options`/Exportwerte, `multiline`, `required`, `read_only`, `page`, `position`. Gemeinsame Feldidentität bei mehreren Widgets; `native_ref` = opak (`<objnum>.<gen>`), nur gültig zusammen mit `sha256`. Widgets außerhalb der Seite → Warnung, nicht verwerfen. Signierte Signaturfelder → `signed: true`.
6. Statische PDFs: Kandidaten aus Linien/Rechtecken mit benachbartem Label → `source: "detected"` mit `confidence` und Warnung. Keine erfundenen Namen; eine leere Liste ist erlaubt.
7. Antwort nach dem Schema in `contracts.md` (`schema_version: "1"`). Logs nur mit Metadaten.

### 5.2 `POST /api/v1/documents/forms/fill`
Multipart `file` (unveränderte Arbeitsvorlage) + `manifest` (JSON) + optional `image_<ref>`-Teile (PNG) + optional `password`. Ablauf:
1. `sha256` des Manifests ≠ Datei → `hash_mismatch` (409). Validierung: bekannte Typen, Optionen, Seiten, Geometrie innerhalb der Seite, Konflikt `native` vs. `overlay` für dasselbe Widget → `invalid_manifest`.
2. Schutz: signierte Dokumente → `signature_conflict`; fehlende Füll- bzw. Änderungsberechtigung → `permission_denied`.
3. **Ein gemeinsamer Rendervertrag für Vorschau und Endausgabe:** Text (auch in nativen Textfeldern) zeichnet DocPipe selbst als Overlay mit dem aufgelösten `style` (Schrift aus der Freigabeliste, Größe, Farbe, Schnitt, Ausrichtung, mehrzeilig, Auto-Fit bis `min_font_size`). Weiterer Überlauf → `text_overflow`, nicht unterstützte Zeichen → `unsupported_characters`. Danach wird das native Text-Widget entfernt. Checkbox/Radio: `/V` und `/AS` auf den gültigen Exportwert setzen und die vorhandene Appearance flatten.
4. Bilder: PNG validieren (Pillow: Typ, Pixelzahl, Bytes), proportional und zentriert ins Rechteck einpassen, Transparenz erhalten, Seitenrotation berücksichtigen.
5. Ausgabe: MVP nur `flatten: true` → 200 `application/pdf`, `Cache-Control: no-store`. Die Original-Temp-Datei bleibt unverändert und wird in jedem Fall gelöscht. Fehlertexte enthalten Feld-IDs, nie Werte.
6. Nach dem MVP: Ausgabe mit Passwort per `pikepdf` (AES-256), Alternative Stirling `security/add-password`. Kein unverschlüsselter Fallback.

**Neue Fehlercodes (additiv):** `forms_disabled` 403, `password_required` 422, `invalid_password` 422, `permission_denied` 422, `signature_conflict` 422, `xfa_unsupported` 422, `invalid_manifest` 400, `hash_mismatch` 409, `unknown_field` 422, `invalid_geometry` 422, `text_overflow` 422, `unsupported_characters` 422, `invalid_image` 400, `limit_exceeded` 413/429.

**Capabilities:** Bei `services.forms` die Features `forms_inspect` und `forms_fill`, dazu `forms.fonts` (Liste), `forms.schema_version` und die Limits des Clients.

## 6. Risiken und offene Punkte
- **Stirling `latest` nicht fixiert:** Vor Phase 2 auf eine geprüfte Version festlegen. Die installierte Version auf dem Server verifizieren (eigene Freigabe, nur lesend).
- **Client-Eintrag für PDF4Fill und Key** legt der Betreiber an. Die Änderung an `config.yaml` erfordert einen DocPipe-Neustart, was HubDix kurz unterbricht. Wartungsfenster abstimmen.
- RAM auf dem 8-GB-Server: Inspect und Fill im Prozess sind leichtgewichtig, OCR bleibt der teure Pfad. `max_pages` je Client begrenzen.
- Erkennung von Feldern in statischen oder gescannten PDFs ist heuristisch, deshalb `detected` + Warnung. Die Qualität ist an einem realen Referenzkorpus zu messen.
- Native Interaktivität (`flatten: false`) ist im MVP nicht vorgesehen, weil der Stil dann nicht garantiert werden kann.

## 7. Empfohlener nächster Implementierungsschritt
**Phase 2.1a (nach Freigabe):** Im DocPipe-Repo additiv `services.forms` (Standard `false`), `routes/forms.py` mit **nur `forms/inspect`** (native Felder, Geometrie-Normalisierung, Text/Textblöcke, Verschlüsselungs-/XFA-/Signatur-Erkennung) mit `pikepdf` und `pdfminer.six`, ohne Detection und ohne OCR. Dazu kommt ein synthetischer Referenzkorpus (gedreht 0/90/180/270, CropBox-Versatz, Mehrfach-Widgets, verschlüsselt, signiert, XFA) samt Gegenprobe gegen Stirling `form/fields-with-coordinates`. Abnahme: bestehende 748 Tests grün, `/capabilities` für Clients ohne `forms` unverändert. Danach folgen 2.1b (OCR und Kandidaten) und 2.2 (Fill).
