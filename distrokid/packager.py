from __future__ import annotations

import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from config.settings import settings
from suno.metadata import TrackMetadata


@dataclass
class PackageResult:
    output_dir: Path
    zip_path: Path
    audio_path: Path
    cover_path: Path
    metadata_path: Path


def _sanitize(value: str, fallback: str = "track") -> str:
    value = (value or "").strip()
    if not value:
        value = fallback
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._")
    return cleaned[:80] or fallback


def _write_metadata(path: Path, meta: TrackMetadata) -> None:
    payload = {
        "title": meta.title,
        "artist": meta.artist,
        "genre": meta.genre,
        "style": meta.style,
        "prompt": meta.prompt,
        "lyrics": meta.lyrics,
        "source_url": meta.source_url,
        "song_id": meta.song_id,
        "isrc": "ADD_ISRC_IF_AVAILABLE",
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in src_dir.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(src_dir)
                zf.write(file_path, arcname=str(arcname))


async def package_release(audio_path: Path, cover_path: Path, metadata: TrackMetadata) -> PackageResult:
    song_key = _sanitize(metadata.song_id or audio_path.stem, fallback="track")
    title_key = _sanitize(metadata.title, fallback="untitled")
    output_root = settings.paths.output_dir
    output_root.mkdir(parents=True, exist_ok=True)

    folder = output_root / f"{song_key}_{title_key}"
    folder.mkdir(parents=True, exist_ok=True)

    audio_ext = audio_path.suffix.lower() if audio_path.suffix else ".mp3"
    packaged_audio = folder / f"audio{audio_ext}"
    packaged_cover = folder / "cover.jpg"
    packaged_meta = folder / "metadata.json"

    shutil.copy2(audio_path, packaged_audio)
    shutil.copy2(cover_path, packaged_cover)
    _write_metadata(packaged_meta, metadata)

    zip_path = output_root / f"ready_for_distrokid_{song_key}.zip"
    _zip_dir(folder, zip_path)

    return PackageResult(
        output_dir=folder,
        zip_path=zip_path,
        audio_path=packaged_audio,
        cover_path=packaged_cover,
        metadata_path=packaged_meta,
    )
