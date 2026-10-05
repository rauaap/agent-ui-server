"""Registered filesystem roots and contained, unauthenticated document serving."""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.datastructures import MutableHeaders, URL
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from .db import Database

ASSET_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts; object-src 'none'",
    "Cache-Control": "no-cache",
}


def normalize_path(path: str) -> str:
    if not Path(path).is_absolute():
        raise ValueError("Path must be absolute")
    return str(Path(path).resolve(strict=False))


class CreateAssetRoot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    asset_root: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    path: str
    project_id: int | None = Field(default=None, ge=1)

    @field_validator("path")
    @classmethod
    def normalize(cls, value: str) -> str:
        return normalize_path(value)


class UpdateAssetRoot(CreateAssetRoot):
    asset_root: str = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    path: str = Field(default=None)


def root_object(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "url": f"/shared-assets/{quote(row['asset_root'], safe='')}/"}


def resolve_asset_link(db: Database, path: str) -> dict[str, str]:
    candidate = Path(normalize_path(path))
    matches = []
    for row in db.list_shared_asset_roots():
        root = Path(row["path"]).resolve(strict=False)
        if candidate.is_relative_to(root):
            matches.append((root, row["asset_root"]))
    if not matches:
        raise ValueError("No registered shared asset root contains this path")
    root, name = min(matches, key=lambda item: (-len(item[0].parts), item[1]))
    relative = candidate.relative_to(root)
    suffix = "" if relative == Path(".") else quote(relative.as_posix(), safe="/")
    return {"url": f"/shared-assets/{quote(name, safe='')}/{suffix}"}


def open_directory(path: Path) -> int:
    """Pin a resolved directory without following any newly swapped symlink."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class ContainedFileResponse(FileResponse):
    def __init__(self, root: Path, candidate: Path) -> None:
        super().__init__(str(candidate), headers=ASSET_HEADERS)
        self.root = root
        self.candidate = candidate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # StaticFiles' realpath containment is retained below. Pin every component
        # with directory descriptors as well, so a symlink swap between lookup and
        # FileResponse's open cannot turn an approved path into an outside file.
        directory_fd = file_fd = None
        try:
            directory_fd = open_directory(self.root)
            relative = self.candidate.relative_to(self.root)
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child
            file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            self.stat_result = os.fstat(file_fd)
            if not stat.S_ISREG(self.stat_result.st_mode):
                raise HTTPException(404, headers=ASSET_HEADERS)
            self.set_stat_headers(self.stat_result)
            self.path = f"/proc/self/fd/{file_fd}"
            # Do not hand a temporary fd path off to an ASGI server's pathsend.
            scope = {**scope, "extensions": {}}
            if scope["method"] == "HEAD":
                # Range applies only to GET, including malformed Range values.
                scope["headers"] = [(key, value) for key, value in scope["headers"] if key.lower() != b"range"]

            async def send_asset(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    # FileResponse also generates its own range-error responses.
                    MutableHeaders(scope=message).update(ASSET_HEADERS)
                await send(message)

            await super().__call__(scope, receive, send_asset)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                raise HTTPException(404, headers=ASSET_HEADERS) from exc
            raise
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)


class AssetFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        # Re-resolve roots for every request, and use StaticFiles' containment
        # implementation, never unrestricted symlink following or an unchecked join.
        root = Path(self.directory).resolve(strict=False)
        self.all_directories = [str(root)]
        full_path, result = self.lookup_path(path)
        if result is not None and stat.S_ISDIR(result.st_mode):
            if not scope["path"].endswith("/"):
                url = URL(scope=scope)
                return RedirectResponse(str(url.replace(path=url.path + "/")), headers=ASSET_HEADERS)
            full_path, result = self.lookup_path(os.path.join(path, "index.html"))
        if result is None or not stat.S_ISREG(result.st_mode):
            raise HTTPException(404, headers=ASSET_HEADERS)
        candidate = Path(full_path)
        if not candidate.is_relative_to(root):
            raise HTTPException(404, headers=ASSET_HEADERS)
        return ContainedFileResponse(root, candidate)
