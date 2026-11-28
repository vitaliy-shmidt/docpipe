"""Build information supplied by the operator through environment variables.

DocPipe cannot know on its own which image it runs in or whether that image
was built from an unmodified commit (production images may carry local
patches). So nothing is guessed:

    DOCPIPE_BUILD_COMMIT         git commit the image was built from (hex, 7-40)
    DOCPIPE_IMAGE_TAG            image tag, e.g. "0c05f96-hubdix"
    DOCPIPE_BUILD_LOCAL_CHANGES  "true" / "false" - whether the build source
                                 differs from that commit; unset = unknown
    DOCPIPE_BUILD_LOCAL_PATCHES  optional comma-separated slugs naming the
                                 local changes, e.g. "ollama-think-false"

`source_state` is "clean" only when the operator explicitly declared
`DOCPIPE_BUILD_LOCAL_CHANGES=false`; with a declared patch list it is always
"modified". Invalid values are dropped (stored as unknown), never stored
verbatim.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,127}")
_PATCH = re.compile(r"[a-z0-9][a-z0-9._\-]{0,63}")
MAX_PATCHES = 8


@dataclass(frozen=True)
class BuildInfo:
    commit: str | None
    image_tag: str | None
    # True / False / None (= not declared)
    local_changes: bool | None
    local_patches: tuple[str, ...]

    @property
    def source_state(self) -> str:
        if self.local_changes or self.local_patches:
            return "modified"
        if self.local_changes is False:
            return "clean"
        return "unknown"


def read_build_info(env: Mapping[str, str] | None = None) -> BuildInfo:
    env = os.environ if env is None else env
    commit = env.get("DOCPIPE_BUILD_COMMIT", "").strip().lower()
    tag = env.get("DOCPIPE_IMAGE_TAG", "").strip()
    raw_changes = env.get("DOCPIPE_BUILD_LOCAL_CHANGES", "").strip().lower()
    local_changes = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}.get(
        raw_changes
    )
    patches = tuple(
        p
        for p in (part.strip().lower() for part in env.get("DOCPIPE_BUILD_LOCAL_PATCHES", "").split(","))
        if _PATCH.fullmatch(p)
    )[:MAX_PATCHES]
    return BuildInfo(
        commit=commit if _COMMIT.fullmatch(commit) else None,
        image_tag=tag if _TAG.fullmatch(tag) else None,
        local_changes=local_changes,
        local_patches=patches,
    )
