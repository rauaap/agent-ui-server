"""Immutable original image storage and native-harness serialization."""
from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from PIL import Image, UnidentifiedImageError

from pydantic import BaseModel

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_IMAGES = 10
MAX_TURN_IMAGE_BYTES = 20 * 1024 * 1024
FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/gif": "GIF", "image/webp": "WEBP"}
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
METADATA_FIELDS = ("id", "mime_type", "size", "width", "height")
validation_slots = asyncio.Semaphore(2)


class ImageMetadata(BaseModel):
    id: str
    mime_type: str
    size: int
    width: int
    height: int


def metadata(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in METADATA_FIELDS}


def validate_file(path: Path, mime_type: str) -> tuple[int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format != FORMATS[mime_type]:
                    raise ValueError("Image MIME type does not match content")
                dimensions = image.size
                image.verify()
            # verify() alone does not decode all formats (notably JPEG).
            with Image.open(path) as image:
                for frame in range(getattr(image, "n_frames", 1)):
                    image.seek(frame)
                    image.load()
            return dimensions
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=400, detail="Invalid image content") from exc


async def upload_image(request: Request, database: Any) -> dict[str, Any]:
    mime_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime_type not in FORMATS:
        raise HTTPException(status_code=415, detail="Unsupported image media type")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared_size = int(length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if declared_size < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if declared_size > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Image exceeds 10 MiB")
    directory = database.path.resolve().parent / "images"
    directory.mkdir(exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=directory, prefix=".upload-")
    temporary = Path(temporary_name)
    final: Path | None = None
    committed = False
    # Await worker completion even on cancellation: cleanup must not race a write.
    async def work(function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
    try:
        with os.fdopen(fd, "wb") as stream:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise HTTPException(status_code=413, detail="Image exceeds 10 MiB")
                await work(stream.write, chunk)
            await work(stream.flush)
        if not size:
            raise HTTPException(status_code=400, detail="Empty image")
        async with validation_slots:
            width, height = await work(validate_file, temporary, mime_type)
        image_id = uuid.uuid4().hex
        destination = directory / (image_id + EXTENSIONS[mime_type])
        # Publish atomically without ever replacing an immutable existing file.
        os.link(temporary, destination)
        final = destination
        record = {"id": image_id, "relative_path": f"images/{final.name}",
                  "mime_type": mime_type, "size": size, "width": width, "height": height}
        database.insert_image(record)
        committed = True
        return metadata(record)
    finally:
        temporary.unlink(missing_ok=True)
        if final is not None and not committed:
            final.unlink(missing_ok=True)


def native_images(database: Any, images: list[dict[str, Any]]) -> list[dict[str, str]]:
    result = []
    for image in images:
        record = database.get_image(image["id"])
        if record is None:
            raise KeyError(image["id"])
        data = (database.path.resolve().parent / record["relative_path"]).read_bytes()
        result.append({"type": "image", "mimeType": record["mime_type"],
                       "data": base64.b64encode(data).decode("ascii")})
    return result
