import asyncio
from io import BytesIO
from typing import Any

import pytest
from fastapi import UploadFile

from backend.media import MediaPreparationError, prepare_media_upload


def test_photo_upload_is_staged_with_generated_private_name(tmp_path: Any) -> None:
    upload = UploadFile(file=BytesIO(b"\xff\xd8\xffphoto"), filename="../../face.jpg")

    prepared = asyncio.run(
        prepare_media_upload(
            upload,
            kind="photo",
            root=tmp_path,
            maximum_bytes=1024,
            ffmpeg_path="missing-ffmpeg",
            width=800,
            height=600,
        )
    )

    assert prepared.path.parent == tmp_path
    assert prepared.path.name != "face.jpg"
    assert prepared.path.suffix == ".jpg"
    assert prepared.path.read_bytes() == b"\xff\xd8\xffphoto"
    assert prepared.width == 800
    assert prepared.height == 600


def test_media_upload_limit_removes_partial_file(tmp_path: Any) -> None:
    upload = UploadFile(file=BytesIO(b"too-large"), filename="voice.ogg")

    with pytest.raises(MediaPreparationError) as error:
        asyncio.run(
            prepare_media_upload(
                upload,
                kind="voice_note",
                root=tmp_path,
                maximum_bytes=4,
                ffmpeg_path="missing-ffmpeg",
                width=0,
                height=0,
            )
        )

    assert error.value.code == "media_too_large"
    assert list(tmp_path.iterdir()) == []


def test_browser_recording_requires_available_transcoder(tmp_path: Any) -> None:
    upload = UploadFile(file=BytesIO(b"webm"), filename="recording.webm")

    with pytest.raises(MediaPreparationError) as error:
        asyncio.run(
            prepare_media_upload(
                upload,
                kind="voice_note",
                root=tmp_path,
                maximum_bytes=1024,
                ffmpeg_path="definitely-not-installed-ffmpeg",
                width=0,
                height=0,
            )
        )

    assert error.value.code == "media_transcoder_unavailable"
    assert list(tmp_path.iterdir()) == []
