"""Expansion, inheritance, and validation for user-configured sandbox mounts."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SandboxMount:
    source: Path
    destination: Path
    write: bool


def overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def destination(value: str) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Sandbox path must be absolute: {value!r}")
    return Path(os.path.normpath(path))


def validate_paths(entries: list[dict[str, Any]]) -> list[SandboxMount]:
    mounts: list[SandboxMount] = []
    home = Path.home().resolve()
    protected = [Path(p) for p in (
        "/usr", "/bin", "/lib", "/lib64", "/etc/ssl/certs",
        "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
        "/tmp", "/dev", "/proc", "/opt/agent-ui",
    )]
    for entry in entries:
        value = entry["path"]
        try:
            target = destination(value)
            source = target.resolve(strict=True)
            if not (source.is_file() or source.is_dir()):
                raise ValueError("must be a regular file or directory")
            if any(home.is_relative_to(p) for p in (target, source)):
                raise ValueError("must not expose the entire home directory")
            if any(overlaps(p, fixed) for p in (target, source) for fixed in protected):
                raise ValueError("overlaps a built-in sandbox mount")
            mount = SandboxMount(source, target, entry.get("write", False))
            for other in mounts:
                if overlaps(target, other.destination) or overlaps(source, other.source):
                    raise ValueError(f"overlaps sandbox path {other.destination}")
            mounts.append(mount)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"Invalid sandbox path {value!r}: {exc}") from exc
    return mounts


def merge_paths(server: list[dict[str, Any]], project: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Validate each scope first, so duplicate entries within one scope aren't
    # accidentally treated as overrides. Validate effective mounts separately.
    validate_paths(server)
    validate_paths(project)
    merged = {destination(entry["path"]): entry for entry in server}
    merged.update({destination(entry["path"]): entry for entry in project})
    entries = list(merged.values())
    validate_paths(entries)
    return entries
