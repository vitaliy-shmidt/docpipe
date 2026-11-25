"""Export the OpenAPI 3.1 contract of the PDF4Fill forms endpoints.

    python -m system.tools.export_forms_openapi [target]

Writes docs/openapi/forms.openapi.json (default): the forms path(s) of
DocPipe's generated OpenAPI document plus exactly the component schemas they
reference. tests/test_forms_ocr_detect.py fails when the committed file and the
running application drift apart, so regenerate after changing
system/forms/schemas.py or the route signature.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_TARGET = Path(__file__).resolve().parents[2] / "docs" / "openapi" / "forms.openapi.json"
FORMS_PREFIX = "/api/v1/documents/forms/"


def _refs(node, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)


def build() -> dict:
    from system.main import app

    spec = app.openapi()
    paths = {p: v for p, v in spec["paths"].items() if p.startswith(FORMS_PREFIX)}
    schemas = spec.get("components", {}).get("schemas", {})
    wanted: set[str] = set()
    _refs(paths, wanted)
    while True:  # transitive closure of nested references
        before = set(wanted)
        for name in before:
            _refs(schemas.get(name, {}), wanted)
        if wanted == before:
            break
    return {
        "openapi": spec["openapi"],
        "info": {
            "title": "DocPipe forms API (PDF4Fill): inspect and fill",
            "version": spec["info"]["version"],
            "description": "Extract of DocPipe's generated OpenAPI document. Contract and behaviour: "
                           "docs/forms-inspect.md. Authentication: Authorization: Bearer <api key> of a "
                           "client with services.forms.",
        },
        "paths": paths,
        "components": {"schemas": {name: schemas[name] for name in sorted(wanted)}},
    }


def render() -> str:
    return json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(target: Path = DEFAULT_TARGET) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {target}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TARGET)
