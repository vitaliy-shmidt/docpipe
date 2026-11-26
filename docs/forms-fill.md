# Forms fill (Phase 2.2, PDF4Fill)

> **Status (25.11.2026):** implemented and covered by automated tests (CI, 911 tests) and a local Docker build; merged to `main` via PR #1. **Not yet verified in production**: no server deployment, no real Stirling OCR run, no PDF4Fill client. Rollout checklist: [staging-deployment.md](staging-deployment.md#pdf4fill-forms-rollout-phase-2).

`POST /api/v1/documents/forms/fill` fills a PDF form template and returns a
new, **flattened, print-ready PDF**. It is the second half of the PDF4Fill
integration; the template and every position come from
[`forms/inspect`](forms-inspect.md) (same coordinate system, same field
identity). Everything runs in-process (pikepdf, reportlab, Pillow); Stirling
is not involved. No AI is used.

Machine-readable contract (OpenAPI 3.1, inspect and fill):
[openapi/forms.openapi.json](openapi/forms.openapi.json). The manifest's JSON
schema is published there as `x-docpipe-manifest-schema` of the fill
operation.

## Access

Same opt-in as inspect: `services.forms: true` (default `false`); otherwise
`403 forms_disabled`. Clients with the flag see the feature `forms_fill` and,
in the `forms` block of `/capabilities`, `fonts` and `max_images`.

## Request

`multipart/form-data`:

| Part | Required | Meaning |
|---|---|---|
| `file` | yes | The **unchanged** template PDF (its SHA-256 must match the manifest) |
| `manifest` | yes | JSON document, see below (max. 1 MiB) |
| `password` | no | Open (user) or owner password; body only, never logged or returned |
| `images` | no | PNG parts, repeated; each part's **file name** is its `image_ref` (`[A-Za-z0-9_.:-]{1,64}`, unique). Max. 10 images, 5 MiB and 25 megapixels each |

### Manifest (schema_version "1")

```json
{
  "schema_version": "1",
  "sha256": "<sha256 of the template, from inspect>",
  "fields": [
    {"id": "field_001", "mode": "native", "type": "text", "native_ref": "5.0",
     "value": "Zoë Müller", "style": {"font_size": 11, "color": "#1030a0", "auto_fit": true}},
    {"id": "field_004", "mode": "native", "type": "radio", "native_ref": "15.0", "value": "herr"},
    {"id": "f9", "mode": "overlay", "type": "date", "page": 1,
     "position": {"x": 400, "y": 81, "width": 75, "height": 11}, "value": "12.08.1964"},
    {"id": "sig", "mode": "overlay", "type": "signature_image", "page": 1,
     "position": {"x": 450, "y": 340, "width": 200, "height": 60}, "image_ref": "signature.png"}
  ],
  "output": {"flatten": true, "preview": false}
}
```

| Key | Meaning |
|---|---|
| `id` | Caller's id (`[A-Za-z0-9_.:-]{1,64}`, unique); only used in error `details` |
| `mode` | `native`: a widget of the template, identified by `native_ref`. `overlay`: drawn at `page` + `position` |
| `type` | `text`, `number`, `date` (values pre-formatted by the caller), `choice`, `checkbox`, `radio`, `stamp_image`, `signature_image` |
| `native_ref` | `mode=native`: from inspect. `input_name`, `page` and `position` may be sent as checks; a different `position` is a conflict (use `mode=overlay` for another place) |
| `page`, `position` | `mode=overlay`: required; inspect coordinates (points, visible page, origin top-left, page from 1, rotation applied) |
| `value` | Text types/`choice`: string (`choice` with `multi_select`: list); `checkbox`: `true`/`false` or its export value; `radio`: one export value of the group |
| `required` | An empty value is an error (also for native fields with the Required flag) |
| `image_ref` | Images: the file name of an `images` part |
| `style` | Fully resolved text style, see below |
| `output.flatten` | Only `true` (flattened output) is supported |
| `output.preview` | Same rendering plus a `PREVIEW` watermark on every page; response header `X-DocPipe-Preview: true` |

Unknown keys anywhere -> `invalid_manifest` with `details.path`.

### Style

| Key | Values (default) |
|---|---|
| `font_family` | `sans` = Liberation Sans, `serif` = Liberation Serif, `mono` = Liberation Mono (`sans`) |
| `font_size` | 4-72 pt (10) |
| `color` | `#RRGGBB` (`#000000`) |
| `font_weight` / `font_style` | `normal`, `bold` / `normal`, `italic` |
| `align` | `left`, `center`, `right` (`left`) |
| `auto_fit` | Shrink in 0.5 pt steps down to `min_font_size` (`false`) |
| `min_font_size` | 4-72 pt, at most `font_size` (6) |
| `multiline` | Override; default: the native field's Multiline flag, otherwise `false` |

The fonts are bundled (`system/forms/fonts/`, SIL Open Font License 1.1) and
metric-compatible with Arial/Helvetica, Times and Courier, so text sized for
those keeps its width. Coverage: Latin incl. Central/Eastern European, Greek,
Cyrillic, common symbols. Characters outside the font -> error, never a
substitute glyph.

## Rendering (preview and final output share it)

One renderer (`system/forms/render.py`) draws everything onto one overlay
page per template page, in display coordinates; the overlay is placed onto
the page with the matrix of its CropBox and `/Rotate`, so text and images are
upright on the displayed page at exactly the inspect positions. Preview is
the identical render with an extra watermark item - nothing else differs.

- **Text**: inner padding 2 pt horizontal, 1 pt vertical; single line
  vertically centred; multiline top-aligned, line height 1.15 x size, word
  wrap (words longer than a line break between characters). **No
  truncation**: if the text does not fit - after auto-fit down to
  `min_font_size` - the result is `text_overflow`. A line break in a
  single-line field is an overflow too.
- **Native text/choice**: drawn on every visible widget of the field (one
  value per field; listing several widgets of one field with different
  values is a conflict). The widget's own border/background stays; its old
  variable text (`/Tx BMC ... EMC`) is removed. `choice` prints the option's
  label, not its export value.
- **Checkbox/radio**: the template's own appearance state is switched on
  (`/AS`); a radio button only if it owns the chosen export value. If a
  widget has no appearance for that state, a vector check mark (checkbox) or
  dot (radio) is drawn instead. The export value is the exact value inspect
  reported; names that are not valid UTF-8 arrive escaped (`au#DFer`) and
  are set as the original PDF name byte for byte
  ([forms-inspect.md](forms-inspect.md#export-values-of-checkboxes-and-radio-buttons-pdf-names)).
- **Images**: PNG, proportionally scaled into the rectangle and centred,
  transparency kept; never stretched.
- **Flattening**: all widget appearances become page content (hidden
  widgets stay invisible), remaining widgets and the AcroForm (including a
  hybrid XFA part) are removed. Unlisted native fields keep their template
  appearance. Other annotations (e.g. links) are kept.
- **Determinism**: the same template and manifest give byte-identical output
  (unencrypted templates).

## Protection (no bypass)

| Situation | Result |
|---|---|
| Open password needed and missing / wrong | `password_required` / `invalid_password` |
| Encrypted template | Output keeps the **same encryption, passwords and permissions** |
| Permission "fill in forms" missing and native fields listed | `permission_denied`, `details.permission: fill_forms` |
| Permission "modify" missing (needed for the flattened output) | `permission_denied`, `details.permission: modify` |
| Owner password supplied | All permissions (as for inspect) |
| A signature field carries a signature, or DocMDP certification | `signature_conflict` (`details.reason`: `signed` / `certified`) - signed documents are never changed |
| Dynamic XFA | `xfa_unsupported`; hybrid XFA: the AcroForm is filled |

## Response

`200`, `Content-Type: application/pdf`, `Cache-Control: no-store`,
`X-DocPipe-Preview: true|false`; body = the PDF. The uploaded template is
never modified; nothing is stored.

## Errors

The normal body `{ok:false, code, message}`, here with an additive
`details` object (field id, reason, permission, path, image_ref - **never
submitted values**):

| code | HTTP | When (`details`) |
|---|---|---|
| `invalid_file` | 400 | Missing/empty/no PDF, no pages, manifest part missing |
| `invalid_manifest` | 400 | Schema violation (`path`), duplicate ids, overlay without page/position, radio/choice as overlay, image as native, unknown `image_ref`, `min_font_size` > `font_size` (`field_id`) |
| `invalid_image` | 400 | Not a PNG, unreadable, unsupported size, bad/duplicate part name (`image_ref` / `reason`) |
| `forms_disabled` | 403 | No `services.forms` |
| `hash_mismatch` | 409 | Template SHA-256 differs from the manifest |
| `file_too_large` / `limit_exceeded` | 413 | Upload, manifest, images, pages or fields over their limits |
| `password_required`, `invalid_password`, `xfa_unsupported` | 422 | As for inspect |
| `permission_denied` | 422 | `permission` |
| `signature_conflict` | 422 | `reason` |
| `unknown_field` | 422 | `native_ref` not in the template (`field_id`) |
| `field_conflict` | 422 | `reason`: `duplicate_native_ref`, `type_mismatch`, `input_name_mismatch`, `geometry_mismatch`, `inconsistent_value`, `overlaps_native` (an overlay covering a filled native widget) |
| `invalid_geometry` | 422 | `reason`: `page` (does not exist), `outside_page` |
| `invalid_value` | 422 | `reason`: `missing_value`, `too_long` (`/MaxLen`), `option`, `type` |
| `text_overflow` | 422 | `field_id` |
| `unsupported_characters` | 422 | `field_id`, `count` (number of distinct characters) |

## Limits

`forms.max_pages` and `forms.max_fields` (config, shared with inspect; the
manifest may not list more fields than `max_fields`), manifest 1 MiB, 10
images of at most 5 MiB / 25 megapixels, `server.max_file_size_mb` for the
template.

## Logging

One `forms_fill` line per request: input/output size, pages, native/overlay
field counts, image count, preview, encrypted, status, duration. Never field
ids, values, labels, file names, images or the password.

## Implementation and tests

- `system/forms/fill.py` (validation, protection, planning, flattening,
  placement, encryption-preserving save), `system/forms/render.py` (the one
  renderer), `system/forms/fonts.py` (bundled fonts, glyph coverage),
  `system/forms/schemas.py` (manifest and responses), route in
  `system/routes/forms.py`. Native widget identity is shared with inspect
  (`inspect.native_widgets()`).
- `tests/test_forms_fill.py` (57 tests): visual checks render the output with
  PDFium (`pypdfium2`, dev dependency) and measure where ink lands - text
  inside its field, alignment, colour, upright text and images on pages with
  `/Rotate` 0/90/180/270 (`tests/fixtures/forms/rotated-fields.pdf`, field
  positions hand-computed), CropBox offsets, proportional image fit with
  transparency, checkbox/radio states, vector marks, kept borders without
  old text, multi-widget fields on two pages, hidden widgets; Unicode and
  unsupported characters; auto-fit, multiline, overflow; every error and
  conflict; encryption kept (user/owner password, AES-256, restrictions);
  signatures, XFA; determinism; preview = final + watermark; route,
  temp-file cleanup, logs without values; manifest and error responses
  against the published OpenAPI schema.

## Not supported yet

Non-flattened (interactive) output, output passwords (post-MVP per the
PDF4Fill contract), comb fields, rich text, vertical text, colours other
than RGB, fonts other than the bundled three families, images other than PNG.
