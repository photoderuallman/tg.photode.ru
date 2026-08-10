from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile


class MediaPreparationError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PreparedMedia:
    path: Path
    width: int
    height: int


async def prepare_media_upload(
    upload: UploadFile,
    *,
    kind: str,
    root: Path,
    maximum_bytes: int,
    ffmpeg_path: str,
    width: int,
    height: int,
) -> PreparedMedia:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    suffix = Path(upload.filename or "upload.bin").suffix.lower()
    if suffix not in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".mp4",
        ".mov",
        ".m4v",
        ".webm",
        ".ogg",
        ".oga",
        ".mp3",
        ".m4a",
    }:
        suffix = ".bin"
    source = root / f"{uuid4().hex}{suffix}"

    try:
        await _save_limited(upload, source, maximum_bytes)
        if kind == "photo":
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                raise MediaPreparationError(
                    "unsupported_photo_format",
                    "Photos must be JPEG, PNG, or WebP.",
                )
            return PreparedMedia(path=source, width=max(width, 0), height=max(height, 0))

        if kind == "voice_note":
            if suffix in {".ogg", ".oga", ".mp3", ".m4a"}:
                return PreparedMedia(path=source, width=0, height=0)
            output = source.with_suffix(".ogg")
            await _ffmpeg(
                ffmpeg_path,
                source,
                output,
                "-vn",
                "-c:a",
                "libopus",
                "-b:a",
                "48k",
                "-ac",
                "1",
                "-threads",
                "1",
            )
            source.unlink(missing_ok=True)
            return PreparedMedia(path=output, width=0, height=0)

        if kind == "video_note":
            output = source.with_suffix(".note.mp4")
            await _ffmpeg(
                ffmpeg_path,
                source,
                output,
                "-t",
                "60",
                "-vf",
                "crop=min(iw\\,ih):min(iw\\,ih),scale=480:480",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                "-threads",
                "1",
            )
            source.unlink(missing_ok=True)
            return PreparedMedia(path=output, width=480, height=480)

        if kind == "video":
            if suffix == ".mp4":
                return PreparedMedia(
                    path=source,
                    width=max(width, 0),
                    height=max(height, 0),
                )
            output = source.with_suffix(".mp4")
            await _ffmpeg(
                ffmpeg_path,
                source,
                output,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "25",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                "-threads",
                "1",
            )
            source.unlink(missing_ok=True)
            return PreparedMedia(
                path=output,
                width=max(width, 0),
                height=max(height, 0),
            )

        raise MediaPreparationError(
            "unsupported_media_kind",
            "Use photo, video, voice_note, or video_note.",
            status_code=400,
        )
    except Exception:
        source.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


async def _save_limited(upload: UploadFile, destination: Path, maximum: int) -> None:
    total = 0
    try:
        with destination.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > maximum:
                    raise MediaPreparationError(
                        "media_too_large",
                        f"Media uploads are limited to {maximum // (1024 * 1024)} MB.",
                        status_code=413,
                    )
                output.write(chunk)
        os.chmod(destination, 0o600)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


async def _ffmpeg(
    configured_path: str,
    source: Path,
    destination: Path,
    *arguments: str,
) -> None:
    executable = shutil.which(configured_path)
    if executable is None:
        raise MediaPreparationError(
            "media_transcoder_unavailable",
            "This recording format requires the server media transcoder.",
            status_code=503,
        )
    process = await asyncio.create_subprocess_exec(
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        *arguments,
        str(destination),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
    except TimeoutError:
        process.kill()
        await process.wait()
        destination.unlink(missing_ok=True)
        raise MediaPreparationError(
            "media_transcode_timeout",
            "The recording took too long to convert.",
            status_code=504,
        ) from None
    if process.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        raise MediaPreparationError(
            "media_transcode_failed",
            "The uploaded recording could not be converted to a Telegram format.",
        ) from RuntimeError(stderr.decode(errors="replace")[-500:])
    os.chmod(destination, 0o600)
