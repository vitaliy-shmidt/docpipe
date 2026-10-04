"""Deterministic merge of chunked extraction results into one result that
matches the mode's unchanged response schema. No AI call involved.

Per field:
- only the chunks authoritative for that field (its group) are consulted;
  if no such chunk ran (the group had no relevant passage at all), every
  chunk is - a value a chunk saw in its own verbatim passages still counts;
- no value -> null; one distinct value -> that value;
- different values -> null, and the conflict is recorded in `notes` -
  never pick one side blindly;
- `notes` collects every chunk's distinct note, then the conflicts;
- dependent fields (unit without value, currency without amount, ...) are
  nulled afterwards. Every rule can only keep or null a model-returned
  value - nothing is computed or invented here.
"""

from __future__ import annotations

import json

NOTES_FIELD = "notes"


def _same_key(value) -> str:
    if isinstance(value, str):
        return "s:" + " ".join(value.split()).casefold()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"n:{float(value)!r}"
    return "j:" + json.dumps(value, sort_keys=True)


def merge_results(
    chunk_results: list[tuple[tuple[str, ...] | None, dict]],
    schema: dict,
    dependent_fields: dict[str, str],
) -> tuple[dict, list[str]]:
    """`chunk_results`: (authoritative fields or None for all, result) per
    call, in call order. Returns (merged result, conflicting field names)."""
    merged: dict = {}
    conflicts: list[str] = []
    conflict_notes: list[str] = []
    for field_name in schema.get("properties", {}):
        if field_name == NOTES_FIELD:
            continue
        owners = [result for fields, result in chunk_results if fields is None or field_name in fields]
        candidates = owners or [result for _, result in chunk_results]
        distinct: dict[str, object] = {}
        for result in candidates:
            value = result.get(field_name)
            if value is not None:
                distinct.setdefault(_same_key(value), value)
        if len(distinct) == 1:
            merged[field_name] = next(iter(distinct.values()))
        else:
            merged[field_name] = None
            if len(distinct) > 1:
                conflicts.append(field_name)
                shown = " / ".join(json.dumps(value, ensure_ascii=False) for value in distinct.values())
                conflict_notes.append(
                    f"Widersprüchliche Angaben in verschiedenen Textstellen ({field_name}): {shown}"
                )

    for dependent, required in dependent_fields.items():
        if dependent in merged and merged.get(required) is None:
            merged[dependent] = None

    if NOTES_FIELD in schema.get("properties", {}):
        notes: dict[str, str] = {}
        for _, result in chunk_results:
            note = result.get(NOTES_FIELD)
            if isinstance(note, str) and note.strip():
                notes.setdefault(_same_key(note), note.strip())
        all_notes = list(notes.values()) + conflict_notes
        merged[NOTES_FIELD] = "\n".join(all_notes) if all_notes else None
    return merged, conflicts
