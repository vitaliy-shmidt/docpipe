"""Runtime-resolved, versioned prompt storage - the only place in DocPipe
that reads or writes a prompt *file*.

Two layers, checked in this order for every lookup:

  1. `runtime_dir` - writable, persisted across deploys (a mounted volume
     in production), holds every prompt version ever saved through the
     Prompt Lab plus each mode's `active.json` pointer. Always the
     write target - this class never writes into `base_dir`.
  2. `base_dir` - the repository-shipped prompts (`system/prompts/`),
     read-only in production (bind-mounted `:ro`, see docker-compose.yml).
     Provides every mode's original version (e.g. "v1") and remains the
     fallback source for any version never touched by the Lab.

Nothing is cached: every call reads straight from disk. For this
deployment's scale that is a deliberately simple, sufficient choice (see
docs/prompt-lab.md) - it also means "hot reload" needs no special
handling at all: activating a new version is visible to the very next
request, no cache to invalidate, no restart required.

Version names and mode/subdir keys are validated strictly (see
`is_valid_version_name`/`_validate_subdir`) before ever touching the
filesystem - the only path segments involved are either a hardcoded
literal (a route builds `f"assistant/{mode.name}"` from its own fixed
mode registry, never from request input) or a version string that has
already passed `is_valid_version_name`. There is no code path that turns
raw client input directly into a filesystem path.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from system.errors import DocPipeError

# v1, v2, ... v10, ... - no leading zeros, no other characters. Deliberately
# simple (see task): this alone makes "../../foo", "test.txt", and "v1/abc"
# all invalid without needing a separate path-traversal-specific check.
_VERSION_PATTERN = re.compile(r"^v[1-9][0-9]*$")
# Mode/subdir keys built by callers from their own fixed registries (never
# from request input) - still validated defensively. Allows one optional
# "/"-separated segment (the "assistant/<name>" nesting).
_SUBDIR_PATTERN = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)?$")

ACTIVE_FILENAME = "active.json"


def is_valid_version_name(version: str) -> bool:
    return bool(_VERSION_PATTERN.match(version))


def _validate_subdir(subdir: str) -> None:
    if not _SUBDIR_PATTERN.match(subdir):
        raise ValueError(f"Invalid prompt registry subdir: {subdir!r}")


class PromptRegistry:
    def __init__(self, base_dir: Path, runtime_dir: Path, max_content_length: int) -> None:
        self._base_dir = base_dir
        self._runtime_dir = runtime_dir
        self._max_content_length = max_content_length

    def _base_path(self, subdir: str, version: str) -> Path:
        return self._base_dir / subdir / f"{version}.txt"

    def _runtime_path(self, subdir: str, version: str) -> Path:
        return self._runtime_dir / subdir / f"{version}.txt"

    def _active_path(self, subdir: str) -> Path:
        return self._runtime_dir / subdir / ACTIVE_FILENAME

    def version_exists(self, subdir: str, version: str) -> bool:
        _validate_subdir(subdir)
        return self._runtime_path(subdir, version).is_file() or self._base_path(subdir, version).is_file()

    def list_versions(self, subdir: str) -> list[str]:
        """All versions available for `subdir`, runtime ∪ base, naturally sorted (v2 before v10)."""
        _validate_subdir(subdir)
        found: set[str] = set()
        for directory in (self._runtime_dir / subdir, self._base_dir / subdir):
            if not directory.is_dir():
                continue
            for entry in directory.glob("*.txt"):
                if is_valid_version_name(entry.stem):
                    found.add(entry.stem)
        return sorted(found, key=lambda v: int(v[1:]))

    def resolve_active_version(self, subdir: str, default_version: str) -> str:
        """The mode's currently active version.

        Reads `runtime_dir/<subdir>/active.json` if present and if the
        version it names still actually resolves; otherwise falls back to
        `default_version` (the mode's repo-shipped default, e.g. "v1") -
        this is exactly the pre-Lab behavior for any mode nobody has ever
        activated a version for.
        """
        _validate_subdir(subdir)
        active_path = self._active_path(subdir)
        if active_path.is_file():
            try:
                data = json.loads(active_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = None
            version = data.get("version") if isinstance(data, dict) else None
            if (
                isinstance(version, str)
                and is_valid_version_name(version)
                and self.version_exists(subdir, version)
            ):
                return version
        return default_version

    def load_prompt(self, subdir: str, version: str) -> str:
        """Raises DocPipeError("unknown_prompt_version") if `version` resolves nowhere."""
        _validate_subdir(subdir)
        if not is_valid_version_name(version):
            raise DocPipeError("unknown_prompt_version", f"Unknown prompt version: {version!r}.")
        runtime_path = self._runtime_path(subdir, version)
        if runtime_path.is_file():
            return runtime_path.read_text(encoding="utf-8")
        base_path = self._base_path(subdir, version)
        if base_path.is_file():
            return base_path.read_text(encoding="utf-8")
        raise DocPipeError("unknown_prompt_version", f"Unknown prompt version: {version!r}.")

    def save_version(self, subdir: str, version: str, content: str) -> None:
        """Writes a brand-new version to `runtime_dir` only. Never overwrites.

        Raises:
            DocPipeError("invalid_prompt_version") - malformed version name.
            DocPipeError("invalid_prompt") - empty content.
            DocPipeError("prompt_too_large") - content exceeds the configured max.
            DocPipeError("prompt_version_exists") - this version already
                resolves (runtime OR base) - versions are immutable once
                they exist, regardless of which layer holds them.
        """
        _validate_subdir(subdir)
        if not is_valid_version_name(version):
            raise DocPipeError(
                "invalid_prompt_version", f"Invalid prompt version name: {version!r} (expected e.g. 'v2')."
            )
        if not content.strip():
            raise DocPipeError("invalid_prompt", "Prompt content must not be empty.")
        if len(content) > self._max_content_length:
            raise DocPipeError(
                "prompt_too_large",
                f"Prompt content exceeds the maximum length of {self._max_content_length} characters.",
            )
        if self.version_exists(subdir, version):
            raise DocPipeError(
                "prompt_version_exists", f"Prompt version {version!r} already exists for this mode."
            )
        target = self._runtime_path(subdir, version)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, content)

    def activate_version(self, subdir: str, version: str) -> None:
        """Raises DocPipeError("unknown_prompt_version") if `version` does not resolve."""
        _validate_subdir(subdir)
        if not is_valid_version_name(version) or not self.version_exists(subdir, version):
            raise DocPipeError("unknown_prompt_version", f"Unknown prompt version: {version!r}.")
        active_path = self._active_path(subdir)
        active_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(active_path, json.dumps({"version": version}))

    def default_exists(self, subdir: str, default_version: str) -> bool:
        """Startup fail-fast check (see system/config.py's
        `_validate_prompt_defaults`, which turns a False here into a
        ConfigError): the repo-shipped default prompt must actually be
        readable. Mirrors the crash-at-import behavior this registry
        replaced (a missing/broken base prompt file must stop the process
        from starting, never surface as a per-request 500). Deliberately
        returns a bool rather than raising here - this is a pure
        filesystem check with no request-facing error code of its own,
        unlike every other method on this class."""
        _validate_subdir(subdir)
        return self._base_path(subdir, default_version).is_file()


def _atomic_write(path: Path, content: str) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        # os.replace() already removed tmp_path on success; this only
        # cleans up after a failed write (e.g. disk full) - never leaves a
        # half-written temp file lying around.
        if tmp_path.exists():
            tmp_path.unlink()
