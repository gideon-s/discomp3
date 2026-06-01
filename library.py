"""Scans the local music directory, reads ID3 tags, resolves track queries."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import mutagen

# Audio formats ffmpeg can stream. The project is "mp3 player" but there's no
# reason to reject other common formats the same machinery handles.
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma"}


@dataclass(frozen=True)
class Track:
    """A playable audio file in the library."""

    name: str              # display name: path relative to root, without extension
    path: str              # absolute path on disk
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track_num: int | None = None

    @property
    def display(self) -> str:
        """Prefer 'Artist — Title' from tags; fall back to relative path."""
        if self.title and self.artist:
            return f"{self.artist} — {self.title}"
        if self.title:
            return self.title
        return self.name


def _read_tags(path: str) -> tuple[str | None, str | None, str | None, int | None]:
    """Return (title, artist, album, track_num) from the file's tags."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None, None, None, None
    if audio is None:
        return None, None, None, None

    def first(key: str) -> str | None:
        v = audio.get(key)
        if not v:
            return None
        s = str(v[0]).strip()
        return s or None

    def first_int(key: str) -> int | None:
        s = first(key)
        if s is None:
            return None
        # tracknumber is often "3/12" — take the leading integer
        head = s.split("/", 1)[0].strip()
        try:
            return int(head)
        except ValueError:
            return None

    return first("title"), first("artist"), first("album"), first_int("tracknumber")


class Library:
    """A view over a directory of audio files, with per-file mtime caching.

    The scan walks the directory tree; for each file unchanged since last scan,
    the previously-parsed Track (with tags) is reused. A short TTL on the
    top-level list keeps autocomplete from re-walking the tree on every keystroke.
    """

    def __init__(self, root: str, cache_ttl: float = 5.0):
        self.root = Path(root).expanduser().resolve()
        self.cache_ttl = cache_ttl
        self._cache: list[Track] | None = None
        self._cache_time = 0.0
        self._file_cache: dict[str, tuple[float, Track]] = {}  # path -> (mtime, Track)

    def tracks_cached(self) -> list[Track]:
        """Return the last scanned tracks without touching the filesystem.

        Safe to call from latency-sensitive contexts like autocomplete, where
        going through `tracks()` could trigger a cold scan that blocks the
        event loop past Discord's 3-second interaction deadline.
        """
        return self._cache or []

    async def refresh(self) -> list[Track]:
        """Run a full scan in a worker thread; update the cache."""
        return await asyncio.to_thread(self.tracks, True)

    def tracks(self, force: bool = False) -> list[Track]:
        now = time.monotonic()
        if not force and self._cache is not None and now - self._cache_time < self.cache_ttl:
            return self._cache

        out: list[Track] = []
        if self.root.exists():
            for p in sorted(self.root.rglob("*")):
                if not (p.is_file() and p.suffix.lower() in AUDIO_EXTS):
                    continue
                path = str(p)
                mtime = p.stat().st_mtime
                cached = self._file_cache.get(path)
                if cached and cached[0] == mtime:
                    out.append(cached[1])
                    continue
                rel = p.relative_to(self.root).with_suffix("")
                title, artist, album, track_num = _read_tags(path)
                track = Track(
                    name=str(rel),
                    path=path,
                    title=title,
                    artist=artist,
                    album=album,
                    track_num=track_num,
                )
                self._file_cache[path] = (mtime, track)
                out.append(track)

        # Prune cache entries for files that no longer exist.
        existing = {t.path for t in out}
        for k in list(self._file_cache):
            if k not in existing:
                del self._file_cache[k]

        self._cache = out
        self._cache_time = now
        return out

    def search(self, query: str, *, refresh: bool = True) -> list[Track]:
        """Case-insensitive substring match across name, title, artist, and album.

        Pass `refresh=False` to skip the filesystem scan and search only the
        last cached list (used by autocomplete).
        """
        q = query.lower().strip()
        tracks = self.tracks() if refresh else self.tracks_cached()
        if not q:
            return tracks
        exact = [t for t in tracks if t.name.lower() == q]
        if exact:
            return exact
        matches: list[Track] = []
        for t in tracks:
            haystack = " ".join(filter(None, [t.name, t.title, t.artist, t.album])).lower()
            if q in haystack:
                matches.append(t)
        return matches

    def resolve(self, query: str) -> Track | None:
        """Resolve a query to a single track (1-based index or name fragment)."""
        q = query.strip()
        if not q:
            return None
        tracks = self.tracks()
        if q.isdigit():
            i = int(q) - 1
            return tracks[i] if 0 <= i < len(tracks) else None
        matches = self.search(q)
        return matches[0] if matches else None

    def albums(self, *, refresh: bool = True) -> dict[str, list[Track]]:
        """Group tracks by album tag (missing -> 'Unknown Album'), sorted by album name."""
        out: dict[str, list[Track]] = {}
        source = self.tracks() if refresh else self.tracks_cached()
        for t in source:
            key = t.album or "Unknown Album"
            out.setdefault(key, []).append(t)
        return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))

    def artists(self, *, refresh: bool = True) -> dict[str, list[Track]]:
        """Group tracks by artist tag (missing -> 'Unknown Artist'), sorted by artist name."""
        out: dict[str, list[Track]] = {}
        source = self.tracks() if refresh else self.tracks_cached()
        for t in source:
            key = t.artist or "Unknown Artist"
            out.setdefault(key, []).append(t)
        return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))

    def tracks_by_album(self, name: str) -> list[Track]:
        """Return all tracks on a given album, ordered by tracknumber then path."""
        target = name.strip().lower()
        matches = [
            t for t in self.tracks()
            if (t.album or "Unknown Album").lower() == target
        ]
        matches.sort(key=lambda t: (t.track_num if t.track_num is not None else 9999, t.path))
        return matches

    def tracks_by_artist(self, name: str) -> list[Track]:
        """Return all tracks by a given artist, grouped by album then tracknumber."""
        target = name.strip().lower()
        matches = [
            t for t in self.tracks()
            if (t.artist or "Unknown Artist").lower() == target
        ]
        matches.sort(
            key=lambda t: (
                (t.album or "").lower(),
                t.track_num if t.track_num is not None else 9999,
                t.path,
            )
        )
        return matches
