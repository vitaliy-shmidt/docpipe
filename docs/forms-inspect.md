# Forms inspect (Phase 2.1a, PDF4Fill)

`POST /api/v1/documents/forms/inspect` reads the structure of a PDF form
template: native form fields, page text with positions, the template's
SHA-256 and its security properties. It is the first half of the PDF4Fill
integration (fill follows in Phase 2.2). Design and the reasons for running
in-process instead of via Stirling:
[architecture/pdf4fill-capability-audit.md](architecture/pdf4fill-capability-audit.md).

Not part of this phase: filling, OCR for scanned pages, detection of empty
areas in static PDFs, per-client limits or usage statistics.

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
`services.forms: true`, the feature `forms_inspect` and:

```json
"forms": {"schema_version": "1", "max_pages": 200, "max_fields": 2000}
```

## Request

`multipart/form-data`:

| Part | Required | Meaning |
|---|---|---|
| `file` | yes | The PDF (checked by its `%PDF-` signature; `server.max_file_size_mb` applies) |
| `password` | no | Open (user) or owner password. Only in the body, never in the URL; never logged or returned |

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
    "security": {
      "encrypted": false, "open_password_required": false, "encryption_method": null,
      "permissions": null, "signed": false, "certified": false, "xfa": false
    },
    "warnings": []
  }
}
```

### Coordinates

All positions (fields and text blocks) use one system, as a viewer displays
the page:

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

### Fields

One entry **per widget**, ordered by page, then top-to-bottom, then
left-to-right; `id` is `field_NNN` in that order. A field with several
widgets (e.g. the same name on two pages, or the buttons of a radio group)
yields several entries with the same `input_name` and `field_ref`, distinct
`native_ref`, `widget_index` and the shared `widget_count`.

| Key | Meaning |
|---|---|
| `name` | Display name: the field's tooltip (`/TU`) or, if none, `input_name` |
| `input_name` | Fully qualified technical field name (`parent.child`), the identity used for filling |
| `native_ref` | Opaque widget reference. Only valid together with the `sha256` of the same file |
| `field_ref` | Opaque reference of the field (shared by its widgets) |
| `type` | `text`, `checkbox`, `radio`, `choice` or `signature` |
| `required`, `read_only`, `hidden` | Field flags; `hidden` = widget flagged Hidden/NoView |
| `options` | `checkbox`/`radio`: export values (`/Opt` labels if present); `choice`: `{value, label}` from `/Opt` |
| `export_value` | `checkbox`/`radio`: the on-state of THIS widget |
| `multiline`, `max_length`, `password_field` | `text` only (the last two only when set) |
| `choice_style`, `editable`, `multi_select` | `choice` only (`combo` or `list`) |
| `signed` | `signature` only: the field carries a signature value |

Current field values are not returned. Push buttons, fields without a name
or of an unknown type, and widgets that are on no page are ignored with a
warning carrying a `count`.

### Text

`text_blocks` holds one entry per text line (pdfminer.six layout analysis),
`text` the page text, `data.text` all page texts joined by a blank line.
`text_source` is `embedded` or `none`; there is no OCR in this endpoint, a
page without text yields the warning `no_text_layer` (use
`/documents/extract-text` for OCR'd text).

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

Permissions are respected, not bypassed: without `extract_text` no text is
returned (`text_extraction_not_permitted`); without `fill_forms` the fields
are reported with the warning `form_filling_not_permitted`.

### Warnings

`warnings` is a list of `{code, message, ...}`; optional keys `page`,
`count`, `field_ref`. Codes: `pdf_structure_repaired`, `invalid_page_rotation`,
`widget_clipped`, `widgets_outside_page_ignored`, `widgets_without_page_ignored`,
`push_buttons_ignored`, `unnamed_fields_ignored`, `unsupported_fields_ignored`,
`field_tree_invalid`, `xfa_present`, `signed_document`, `certified_document`,
`form_filling_not_permitted`, `text_extraction_not_permitted`,
`text_extraction_failed`, `no_text_layer`. Messages never contain names,
values or text.

## Errors

The normal `{ok:false, code, message}` body:

| code | HTTP | When |
|---|---|---|
| `unauthorized` | 401 | Key missing, unknown or client disabled |
| `forms_disabled` | 403 | The client has no `services.forms` |
| `invalid_file` | 400 | Missing/empty file, no `%PDF-` signature, unreadable PDF, no pages |
| `file_too_large` | 413 | Upload exceeds `server.max_file_size_mb` |
| `limit_exceeded` | 413 | More pages than `forms.max_pages` or more widgets than `forms.max_fields` |
| `password_required` | 422 | The file needs an open password and none was sent |
| `invalid_password` | 422 | The supplied password does not open the file |
| `xfa_unsupported` | 422 | Dynamic XFA (`NeedsRendering`) or XFA without AcroForm fields |

## Configuration

```yaml
forms:              # optional; defaults shown
  max_pages: 200    # technical safety limits, not quotas
  max_fields: 2000
```

Unknown keys, non-integers and values <= 0 are rejected at startup
(`ConfigError`).

## Implementation and tests

- `system/forms/geometry.py`: the visible-page coordinate system.
- `system/forms/inspect.py`: structure (pikepdf), text (pdfminer.six on an
  in-memory copy with explicit MediaBox and `/Rotate 0`, so text and widgets
  share one mapping); the upload itself is never modified.
- `system/routes/forms.py`: route, temp-file handling (shared with
  extract-text), metadata-only log line `forms_inspect`.
- Reference PDFs: `tests/fixtures/forms/` (synthetic; generator
  `python -m tests.forms_fixtures`; `.gitattributes` keeps them binary).
  Covered: all field types, 0/90/180/270 and inherited rotation, CropBox
  with offset MediaBox, clipped/outside widgets, multi-widget and
  hierarchical fields, hidden widgets, no text layer, AES-256 with user
  password, owner restrictions, signed/certified, hybrid and dynamic XFA,
  invalid rotation, damaged xref, page/field limits.
- Expected positions in `tests/test_forms_inspect.py` are computed by hand
  from the generator's user-space rectangles, not by the code under test.
