"""Reproducible release build: Dockerfile, requirements.txt and constraints.txt agree."""

from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from system.monitoring.buildinfo import read_build_info

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
BUILD_VARS = (
    "DOCPIPE_BUILD_COMMIT",
    "DOCPIPE_IMAGE_TAG",
    "DOCPIPE_BUILD_LOCAL_CHANGES",
    "DOCPIPE_BUILD_LOCAL_PATCHES",
)


def _lines(name: str) -> list[str]:
    text = (ROOT / name).read_text(encoding="utf-8")
    return [line.split("#", 1)[0].strip() for line in text.splitlines() if line.split("#", 1)[0].strip()]


def _pins() -> dict[str, str]:
    pins = {}
    for line in _lines("constraints.txt"):
        name, sep, version = line.partition("==")
        assert sep and version and not re.search(r"[<>!~*,; ]", version), f"not an exact pin: {line}"
        pins[canonicalize_name(name)] = version
    return pins


def test_every_requirement_is_pinned_within_its_declared_range():
    pins = _pins()
    for line in _lines("requirements.txt"):
        req = Requirement(line)
        name = canonicalize_name(req.name)
        assert name in pins, f"{req.name} has no exact version in constraints.txt"
        assert req.specifier.contains(pins[name], prereleases=True), (
            req.name,
            pins[name],
            str(req.specifier),
        )


def test_constraints_have_no_duplicates_and_no_pip():
    names = [canonicalize_name(line.partition("==")[0]) for line in _lines("constraints.txt")]
    assert len(names) == len(set(names))
    assert "pip" not in names and "setuptools" not in names  # come from the pinned base image


def test_dockerfile_pins_the_base_image_and_installs_with_constraints():
    base = re.search(r"^FROM (\S+)", DOCKERFILE, re.M).group(1)
    assert re.fullmatch(r"python:[0-9.]+-slim@sha256:[0-9a-f]{64}", base), base
    assert "COPY requirements.txt constraints.txt ./" in DOCKERFILE
    assert "pip install --no-cache-dir -r requirements.txt -c constraints.txt" in DOCKERFILE


def test_dockerfile_passes_build_metadata_and_keeps_runtime_settings():
    for var in BUILD_VARS:
        assert f'ARG {var}=""' in DOCKERFILE and f"{var}=${{{var}}}" in DOCKERFILE, var
    assert "org.opencontainers.image.revision" in DOCKERFILE
    assert "USER docpipe" in DOCKERFILE and "--no-proxy-headers" in DOCKERFILE


def test_build_metadata_from_the_image_environment_is_shown():
    info = read_build_info(
        {
            "DOCPIPE_BUILD_COMMIT": "B3B88B9",
            "DOCPIPE_IMAGE_TAG": "1.0.0-b3b88b9",
            "DOCPIPE_BUILD_LOCAL_CHANGES": "false",
            "DOCPIPE_BUILD_LOCAL_PATCHES": "",
        }
    )
    assert info.commit == "b3b88b9" and info.image_tag == "1.0.0-b3b88b9" and info.source_state == "clean"
    empty = read_build_info(dict.fromkeys(BUILD_VARS, ""))  # build args not given
    assert empty.commit is None and empty.source_state == "unknown"
