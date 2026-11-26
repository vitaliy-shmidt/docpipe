# Forms inspect (Phase 2.1a + 2.1b, PDF4Fill)

> **Status (25.11.2026):** implemented and covered by automated tests (CI, 911 tests) and a local Docker build; merged to `main` via PR #1. **Not yet verified in production**: no server deployment, no real Stirling OCR run, no PDF4Fill client. Rollout checklist: [staging-deployment.md](staging-deployment.md#pdf4fill-forms-rollout-phase-2).

`POST /api/v1/documents/forms/inspect` reads the structure of a PDF form
template: native form fields, proposals for input fields on static pages,
page text with positions (OCR for pages without a text layer), the template's
SHA-256 and its security properties. It is the first half of the PDF4Fill
integration; filling is [`forms/fill`](forms-fill.md) (Phase 2.2). Design and the reasons for running
in-process instead of via Stirling's form endpoints:
[architecture/pdf4fill-capability-audit.md](architecture/pdf4fill-capability-audit.md).

Machine-readable contract (OpenAPI 3.1, extract of DocPipe's generated
document): [openapi/forms.openapi.json](openapi/forms.openapi.json).

Not part of this endpoint: filling (see [forms-fill.md](forms-fill.md)), image-based
line detection on scans, per-client limits or usage statistics.

## Access

A separate opt-in per client, independent of `documents`:

```yaml
clients:
  pdf4fill:
    enabled: true
    api_key: "..."
    services:
      documents: true   # optional: convert-to-pdf before inspecting
      forms: true
```

`forms` defaults to `false`; a client without the key behaves exactly as
before and its `/capabilities` body is unchanged (no `forms` key at all).
Calling the endpoint without the flag -> `403 forms_disabled`.

For a client with `forms: true`, `/capabilities` additionally contains
`services.forms: true`, the features `forms_inspect`, `forms_detect`, `forms_fill`
and - only if `stirling.ocr.enabled` - `forms_ocr`, plus:

```json
"forms": {"schema_version": "1", "max_pages": 200, "max_fields": 2000, "max_ocr_pages": 20,
          "fonts": {"sans": "Liberation Sans", "serif": "Liberation Serif", "mono": "Liberation Mono"},
          "max_images": 10}
```

## Request

`multipart/form-data`:

| Part | Required | Meaning |
|---|---|---|
| `file` | yes | The PDF (checked by its `%PDF-` signature; `server.max_file_size_mb` applies) |
| `password` | no | Open (user) or owner password. Only in the body, never in the URL; never logged or returned |
| `ocr` | no | `auto` (default): OCR pages without a text layer via Stirling; `off`: never |
| `detect` | no | `auto` (default): add proposals for static fields; `off`: native fields only |

Any other value of `ocr`/`detect` -> `400 invalid_request`.

## Response

`200`, `Cache-Control: no-store`. Excerpt for `tests/fixtures/forms/basic-form.pdf`
(first text block and first field only):

```json
{
  "ok": true,
  "data": {
    "schema_version": "1",
    "sha256": "<hex sha-256 of the uploaded bytes>",
    "page_count": 1,
    "text": "Vorname:\nEinverstanden\nAnrede\nLand",
    "pages": [{
      "page": 1, "width": 595.0, "height": 842.0, "rotation": 0,
      "text_source": "embedded",
      "text": "Vorname:\nEinverstanden\nAnrede\nLand",
      "text_blocks": [{"text": "Vorname:", "position": {"x": 50.0, "y": 73.28, "width": 47.69, "height": 11.0}}]
    }],
    "fields": [{
      "id": "field_001",
      "name": "Vorname",
      "input_name": "vorname",
      "native_ref": "5.0",
      "field_ref": "5.0",
      "widget_index": 0, "widget_count": 1,
      "page": 1,
      "position": {"x": 120.0, "y": 72.0, "width": 200.0, "height": 20.0},
      "source": "native",
      "type": "text",
      "required": true, "read_only": false, "hidden": false,
      "multiline": false, "max_length": 40,
      "options": []
    }],
    "ocr": {"status": "not_needed", "pages": []},
    "detection": {"enabled": true, "count": 0},
    "security": {
      "encrypted": false, "open_password_required": false, "encryption_method": null,
      "permissions": null, "signed": false, "certified": false, "xfa": false
    },
    "warnings": []
  }
}
```

A field proposal (`tests/fixtures/forms/static-form.pdf`, the line after "Name:"):

```json
{
  "id": "detected_001", "name": "Name", "input_name": null,
  "native_ref": null, "field_ref": null,
  "page": 1, "position": {"x": 100.0, "y": 77.0, "width": 230.0, "height": 16.0},
  "source": "detected", "type": "text", "detection": "underline", "confidence": 0.85,
  "label": {"text": "Name:", "position": {"x": 50.0, "y": 81.28, "width": 32.4, "height": 11.0}},
  "multiline": false, "options": []
}
```

### Coordinates

All positions (fields, proposals and text blocks, also from OCR) use one
system, as a viewer displays the page:

- PDF points; page numbers start at 1;
- the visible page = CropBox clipped to the MediaBox (inherited values from
  the page tree are resolved);
- origin top-left, y grows downwards;
- page rotation (`/Rotate` 0/90/180/270, clockwise, also inherited) is
  applied: `width`/`height` of a rotated page are the displayed dimensions.

A widget partly outside the visible page is clipped to it (`widget_clipped`);
one entirely outside is left out (`widgets_outside_page_ignored`). Positions
are therefore always inside the page. An invalid rotation (not a multiple of
90) is treated as 0 with the warning `invalid_page_rotation`.

Text on a rotated page is grouped in its displayed reading direction: if the
glyphs of a rotated page are drawn to be read upright on screen (the normal
case, e.g. a landscape scan stored with `/Rotate 90`), that page is analysed
a second time with the rotation applied, and the result is mapped back
exactly. Text drawn sideways stays sideways.

### Fields

`fields` holds the native widgets first (`source: native`, ids `field_NNN`),
then the proposals (`source: detected`, ids `detected_NNN`). Native ids never
depend on detection, so switching detection on or off does not renumber them.

One native entry **per widget**, ordered by page, then top-to-bottom, then
left-to-right. A field with several widgets (e.g. the same name on two pages,
or the buttons of a radio group) yields several entries with the same
`input_name` and `field_ref`, distinct `native_ref`, `widget_index` and the
shared `widget_count`.

| Key | Meaning |
|---|---|
| `name` | Native: the field's tooltip (`/TU`) or `input_name`. Proposal: the label text, or `null` |
| `input_name` | Native: fully qualified technical field name (`parent.child`), the identity used for filling. Proposal: `null` |
| `native_ref` | Opaque widget reference, only valid with the `sha256` of the same file. Proposal: `null` |
| `field_ref` | Opaque reference of the field (shared by its widgets). Proposal: `null` |
| `type` | `text`, `checkbox`, `radio`, `choice` or `signature` (proposals: `text` or `checkbox`) |
| `required`, `read_only`, `hidden` | Native field flags; `hidden` = widget flagged Hidden/NoView |
| `options` | `checkbox`/`radio`: export values (`/Opt` labels if present); `choice`: `{value, label}` from `/Opt` |
| `export_value` | `checkbox`/`radio`: the on-state of THIS widget |
| `multiline`, `max_length`, `password_field` | `text` only (the last two only when set) |
| `choice_style`, `editable`, `multi_select` | `choice` only (`combo` or `list`) |
| `signed` | `signature` only: the field carries a signature value |
| `detection` | Proposal only: `underline`, `box`, `checkbox` or `placeholder` |
| `confidence` | Proposal only: 0..1 (see below) |
| `label` | Proposal only: the neighbouring text line used as name, with its position, or `null` |

Current field values are not returned. Push buttons, fields without a name
or of an unknown type, and widgets that are on no page are ignored with a
warning carrying a `count`.

### Export values of checkboxes and radio buttons (PDF names)

The export values in `options[].value` and `export_value` are the widget's
appearance-state names (`/AP /N` keys). A PDF name is a byte sequence, not
text, and real forms also use single-byte encodings, e.g. `/au#DFer`
(0xDF = "ß" in Windows-1252). DocPipe reports every name as JSON-safe text
that maps back to exactly one PDF name (`system/forms/names.py`):

| PDF name (bytes) | Reported value | Rule |
|---|---|---|
| `Ja`, `Grüße` (UTF-8), `A#B`, `Sp ace` | unchanged | valid UTF-8 without `#` + two hex digits - as before |
| `au` 0xDF `er` | `au#DFer` | each byte that is not valid UTF-8 becomes `#XX` |
| `Wei#DF` (literal `#`) | `Wei#23DF` | in the escaped form every `#` is written `#23` |

- An escaped value always contains `#` + two upper-case hex digits, an
  unchanged one never does, so different names never get the same value
  (`Wei` 0xDF, UTF-8 `Weiß` and literal `Wei#DF` are three options).
- `label` is for display only: the `/Opt` label if present, otherwise the
  name decoded as UTF-8 or, for escaped names, as Windows-1252 (`außer`);
  labels may coincide, values never do.
- Clients must send `forms/fill` exactly the reported value; it is mapped
  back to the original bytes. Values that inspect cannot produce (e.g.
  `au#dfer` in lower case) are rejected as `invalid_value` / `option`.
- Any other text that cannot be encoded as UTF-8 (lone surrogates) is
  replaced by U+FFFD with the warning `text_unencodable_replaced` (`count`),
  so the response is always valid JSON. Names are never replaced.

### Field proposals (static pages)

Deterministic layout heuristics (`system/forms/detect.py`), no AI. They are
**proposals only**: PDF4Fill shows them for confirmation, nothing is adopted
automatically, and a name is only ever taken from text on the page.

| `detection` | Recognised as | Label searched |
|---|---|---|
| `underline` | Horizontal line with free space above it; the field area is the 16 pt above the line (less if another line is closer) | Text on the same row to the left (`Name: ____`), else a caption directly below (signature lines) |
| `box` | Empty rectangle, 30 pt wide or more, 10-200 pt high; `multiline` above 30 pt | Same row to the left, else directly above |
| `checkbox` | Empty square of 6-20 pt | Same row to the right, else close to the left |
| `placeholder` | A run of 4+ `_` or 6+ `.` in the text (works on OCR text too) | The text before it on the same line |

Not proposed: lines or boxes that already contain text (underlined headings,
info boxes), page-wide separators without a caption, page frames and boxes
that contain other boxes, horizontal rules that end on vertical rules
(tables drawn from single lines), and anything overlapping a native widget.

`confidence` = 0.45 (underline), 0.5 (box, checkbox) or 0.55 (placeholder),
+0.3 with a label, +0.1 if the label ends with `:`, at most 0.95. Proposals
below 0.6 are counted in the warning `fields_detected` as
`low_confidence_count`. At most 200 proposals per page (`detection_limit`).

**Detection limits:** lines inside scanned images are not detected (only
OCR'd placeholder text is); grid cells drawn from separate line segments,
comb fields and circles are not proposed. Measured only on synthetic
documents so far - see "Implementation and tests".

### Text and OCR

`text_blocks` holds one entry per text line (pdfminer.six layout analysis),
`text` the page text, `data.text` all page texts joined by a blank line.
`text_source` is `embedded`, `ocr` or `none`.

Pages without any embedded text are OCR'd through DocPipe's existing
Stirling client (`stirling.ocr`: languages, timeout) when `ocr=auto`, the
server has `stirling.ocr.enabled` and the PDF permits text extraction. Only
those pages are sent, as a decrypted subset with resolved page boxes and
rotation; the password never leaves DocPipe. At most `forms.max_ocr_pages`
pages per request (Stirling is shared with other clients); further pages are
not processed (`ocr_page_limit`, `ocr.status: partial`).

Stirling's answer is checked page by page against the template
(`ocr.pages[].geometry`):

| `geometry` | Meaning | Positions |
|---|---|---|
| `identical` | Same MediaBox, CropBox and rotation (Stirling's OCRmyPDF path) | used as is |
| `rescaled` | A new unrotated page of the visible page's size within 2 % and the same aspect ratio within 1 % (Stirling's Tesseract fallback renders at 300 dpi) | scaled onto the template |
| `mismatch` | Anything else | **not used**: the page gets the OCR text without `text_blocks` and the warning `ocr_geometry_mismatch` - never a guessed position |

`ocr.status`: `not_needed` (every page has text), `applied`, `partial`,
`failed` (with `reason`, e.g. `timeout`, `upstream_unavailable`,
`ocr_invalid_output` for no PDF / different page count), `disabled` (server),
`off` (request) or `not_permitted` (permissions). An OCR failure is a
warning (`ocr_failed`), not an error: the structural result is still
returned.

### Security

| Key | Meaning |
|---|---|
| `encrypted` | The file is encrypted |
| `open_password_required` | The file cannot be opened without a password (and the supplied one worked) |
| `encryption_method` | e.g. `aesv3` (AES-256), `aesv2`, `rc4` |
| `permissions` | Only for encrypted files: `extract_text`, `fill_forms`, `modify_annotations`, `modify`, `assemble`, `print`. With the owner password all are `true` |
| `signed` | At least one signature field carries a signature |
| `certified` | The document has a DocMDP certification |
| `xfa` | An XFA form is present (hybrid: the AcroForm fields are reported) |

Permissions are respected, not bypassed: without `extract_text` no text, no
OCR and no proposals are returned (`text_extraction_not_permitted`, OCR
status `not_permitted`); without `fill_forms` the fields are reported with
the warning `form_filling_not_permitted`.

### Warnings

`warnings` is a list of `{code, message, ...}`; optional keys `page`,
`count`, `field_ref`, `reason`, `low_confidence_count`. Codes:
`pdf_structure_repaired`, `invalid_page_rotation`, `widget_clipped`,
`widgets_outside_page_ignored`, `widgets_without_page_ignored`,
`push_buttons_ignored`, `unnamed_fields_ignored`, `unsupported_fields_ignored`,
`field_tree_invalid`, `xfa_present`, `signed_document`, `certified_document`,
`form_filling_not_permitted`, `text_extraction_not_permitted`,
`text_extraction_failed`, `no_text_layer`, `ocr_failed`, `ocr_page_limit`,
`ocr_no_text`, `ocr_geometry_mismatch`, `fields_detected`, `detection_limit`,
`text_unencodable_replaced`.
Messages never contain names, values or text.

## Errors

The normal `{ok:false, code, message}` body:

| code | HTTP | When |
|---|---|---|
| `unauthorized` | 401 | Key missing, unknown or client disabled |
| `forms_disabled` | 403 | The client has no `services.forms` |
| `invalid_file` | 400 | Missing/empty file, no `%PDF-` signature, unreadable PDF, no pages |
| `invalid_request` | 400 | `ocr` or `detect` other than `auto`/`off` |
| `file_too_large` | 413 | Upload exceeds `server.max_file_size_mb` |
| `limit_exceeded` | 413 | More pages than `forms.max_pages` or more widgets than `forms.max_fields` |
| `password_required` | 422 | The file needs an open password and none was sent |
| `invalid_password` | 422 | The supplied password does not open the file |
| `xfa_unsupported` | 422 | Dynamic XFA (`NeedsRendering`) or XFA without AcroForm fields |

OCR problems are never errors of this endpoint (see "Text and OCR").

## Configuration

```yaml
forms:                # optional; defaults shown
  max_pages: 200      # technical safety limits, not quotas
  max_fields: 2000
  max_ocr_pages: 20   # pages per request sent to Stirling OCR
```

Unknown keys, non-integers and values <= 0 are rejected at startup
(`ConfigError`). OCR languages and timeout come from `stirling.ocr`.

## OpenAPI

`docs/openapi/forms.openapi.json` is generated from the running
application (`python -m system.tools.export_forms_openapi`); the response
models live in `system/forms/schemas.py`. Tests fail when the committed file
is outdated, and validate real responses (native, detected, OCR, encrypted,
signed, XFA) against its schema.

## Implementation and tests

- `system/forms/geometry.py`: the visible-page coordinate system.
- `system/forms/layout.py`: pdfminer.six text lines, glyphs, rules and
  rectangles on the visible page (neutral and display pass).
- `system/forms/ocr.py`: subset for Stirling, output validation, geometry
  classification (`identical`/`rescaled`/`mismatch`).
- `system/forms/detect.py`: field proposals.
- `system/forms/inspect.py`: structure (pikepdf) and orchestration; the
  upload itself is never modified.
- `system/forms/names.py`: reversible, JSON-safe text for PDF names (shared
  by inspect and fill); tests in `tests/test_forms_pdf_names.py` (byte 0xDF,
  UTF-8 umlauts, literal `#`, look-alike names, inspect route, fill round trip).
- `system/routes/forms.py`: route, temp-file handling (shared with
  extract-text), metadata-only log line `forms_inspect` (counts of native
  fields, proposals and OCR pages; never names, text or the password).
- Reference PDFs: `tests/fixtures/forms/` (synthetic; generator
  `python -m tests.forms_fixtures`; `.gitattributes` keeps them binary).
  2.1a: all field types, 0/90/180/270 and inherited rotation, CropBox with
  offset MediaBox, clipped/outside widgets, multi-widget and hierarchical
  fields, hidden widgets, AES-256, owner restrictions, signed/certified,
  hybrid and dynamic XFA, invalid rotation, damaged xref, limits.
  2.1b: a static form with 8 input fields and 5 decoys in three variants that
  display identically (plain landscape, portrait with `/Rotate 90` and
  upright content, offset MediaBox/CropBox; 180/270 are generated in-test),
  a native field on top of a line, scans (A4, `/Rotate 90`, CropBox,
  encrypted) with fake OCR answers in both Stirling output forms plus
  geometry mismatch, timeout, outage, no PDF, missing page, no text, page
  limit.
- Detection quality on these synthetic documents: all 8 expected fields
  found at the hand-computed positions, no proposal on any decoy, identical
  results in all five orientations/variants, and no proposals on the 12
  reference PDFs of phase 2.1a. **Real-world quality is not measured yet**
  (no corpus of real forms); treat `confidence` as a ranking, not a
  probability.
