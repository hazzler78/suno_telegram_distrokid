from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config.settings import settings
from utils.logging import get_logger

logger = get_logger(__name__)

VALID_STATUSES = {
    "downloaded",
    "metadata_extracted",
    "cover_generated",
    "packaged",
    "upload_attempted",
    "failed",
}


class SongTracker:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or (settings.paths.work_dir / "songs.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS songs (
                        id TEXT PRIMARY KEY,
                        song_id TEXT,
                        source_url TEXT,
                        title TEXT,
                        artist TEXT,
                        timestamp TEXT NOT NULL,
                        status TEXT NOT NULL,
                        audio_path TEXT,
                        cover_path TEXT,
                        zip_path TEXT,
                        notes TEXT
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_song_id ON songs(song_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_timestamp ON songs(timestamp DESC)")
                conn.commit()

    def log_song_start(
        self,
        *,
        song_id: Optional[str],
        source_url: str,
        title: Optional[str] = None,
        artist: Optional[str] = None,
    ) -> str:
        track_id = song_id or uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO songs (id, song_id, source_url, title, artist, timestamp, status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (track_id, song_id, source_url, title, artist, now, "downloaded", ""),
                )
                conn.commit()
        return track_id

    def update_song_status(self, id_or_song_id: str, new_status: str, **kwargs: Any) -> bool:
        if new_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {new_status}")
        updates = {"status": new_status, **kwargs}
        allowed_columns = {"song_id", "source_url", "title", "artist", "audio_path", "cover_path", "zip_path"}
        fields = []
        values = []
        for key, value in updates.items():
            if key in allowed_columns or key == "status":
                fields.append(f"{key} = ?")
                values.append(value)
        if not fields:
            return False
        values.extend([id_or_song_id, id_or_song_id])
        query = f"UPDATE songs SET {', '.join(fields)} WHERE id = ? OR song_id = ?"
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute(query, values)
                conn.commit()
                return cur.rowcount > 0

    def get_song_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute(
                    """
                    SELECT id, song_id, source_url, title, artist, timestamp, status, audio_path, cover_path, zip_path, notes
                    FROM songs
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]

    def get_song_status(self, song_key: str) -> Optional[dict[str, Any]]:
        key = song_key.strip()
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute(
                    """
                    SELECT id, song_id, source_url, title, artist, timestamp, status, audio_path, cover_path, zip_path, notes
                    FROM songs
                    WHERE id = ? OR song_id = ? OR id LIKE ? OR song_id LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (key, key, f"{key}%", f"{key}%"),
                )
                row = cur.fetchone()
                return dict(row) if row else None

    def add_notes(self, id_or_song_id: str, notes: str) -> bool:
        current = self.get_song_status(id_or_song_id)
        if not current:
            return False
        existing = current.get("notes") or ""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        merged = f"{existing}\n[{now}] {notes}".strip()
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute(
                    "UPDATE songs SET notes = ? WHERE id = ? OR song_id = ?",
                    (merged, id_or_song_id, id_or_song_id),
                )
                conn.commit()
                return cur.rowcount > 0


tracker = SongTracker()
