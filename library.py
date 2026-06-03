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

# Directory names and filename prefixes that mark macOS-zip junk. AppleDouble
# files (`._foo.mp3`) and `__MACOSX/` mirror dirs aren't real audio and would
# show up as broken tracks in `/list`.
SKIP_DIR_NAMES = {"__MACOSX"}
SKIP_FILENAME_PREFIXES = ("._",)


@dataclass(frozen=True)
class Track:
    """A playable audio file in the library."""

    name: str              # display name: path relative to root, without extension
    path: str              # absolute path on disk
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track_num: int | None = None
    disc_num: int | None = None

    @property
    def display(self) -> str:
        """Prefer 'Artist — Title' from tags; fall back to relative path."""
        if self.title and self.artist:
            return f"{self.artist} — {self.title}"
        if self.title:
            return self.title
        return self.name


def _norm_key(s: str) -> str:
    """Case- and whitespace-insensitive key for grouping ('The  Beatles' == 'the beatles')."""
    return " ".join(s.lower().split())


def _read_tags(path: str) -> tuple[str | None, str | None, str | None, int | None, int | None]:
    """Return (title, artist, album, track_num, disc_num) from the file's tags."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None, None, None, None, None
    if audio is None:
        return None, None, None, None, None

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
        # tracknumber/discnumber are often "3/12" — take the leading integer
        head = s.split("/", 1)[0].strip()
        try:
            return int(head)
        except ValueError:
            return None

    return (
        first("title"),
        first("artist"),
        first("album"),
        first_int("tracknumber"),
        first_int("discnumber"),
    )


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
                if p.name.startswith(SKIP_FILENAME_PREFIXES):
                    continue
                if any(part in SKIP_DIR_NAMES for part in p.parts):
                    continue
                path = str(p)
                mtime = p.stat().st_mtime
                cached = self._file_cache.get(path)
                if cached and cached[0] == mtime:
                    out.append(cached[1])
                    continue
                rel = p.relative_to(self.root).with_suffix("")
                title, artist, album, track_num, disc_num = _read_tags(path)
                track = Track(
                    name=str(rel),
                    path=path,
                    title=title,
                    artist=artist,
                    album=album,
                    track_num=track_num,
                    disc_num=disc_num,
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

    def _group_case_insensitive(
        self,
        *,
        field: str,
        unknown_label: str,
        source: list[Track] | None = None,
        refresh: bool = True,
    ) -> dict[str, list[Track]]:
        """Group tracks by a tag field, merging case/whitespace variants.

        The displayed name for each group is the spelling used by the most
        tracks (ties broken alphabetically), so `/list` shows a single
        canonical entry rather than splitting `Devo` and `DEVO` apart.

        Pass `source` to group a pre-filtered subset (e.g. one artist's tracks)
        instead of the whole library.
        """
        by_norm: dict[str, dict[str, list[Track]]] = {}
        if source is None:
            source = self.tracks() if refresh else self.tracks_cached()
        for t in source:
            value = getattr(t, field) or unknown_label
            by_norm.setdefault(_norm_key(value), {}).setdefault(value, []).append(t)

        out: dict[str, list[Track]] = {}
        for spellings in by_norm.values():
            canonical = max(spellings, key=lambda s: (len(spellings[s]), s))
            out[canonical] = [t for ts in spellings.values() for t in ts]
        return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))

    def albums(self, *, refresh: bool = True) -> dict[str, list[Track]]:
        """Group tracks by album, case- and whitespace-insensitive."""
        return self._group_case_insensitive(
            field="album", unknown_label="Unknown Album", refresh=refresh
        )

    def artists(self, *, refresh: bool = True) -> dict[str, list[Track]]:
        """Group tracks by artist, case- and whitespace-insensitive."""
        return self._group_case_insensitive(
            field="artist", unknown_label="Unknown Artist", refresh=refresh
        )

    def albums_by_artist(self, artist_name: str, *, refresh: bool = True) -> dict[str, list[Track]]:
        """Return {album: [tracks]} for albums containing tracks by this artist."""
        target = _norm_key(artist_name)
        source = self.tracks() if refresh else self.tracks_cached()
        matching = [t for t in source if _norm_key(t.artist or "Unknown Artist") == target]
        return self._group_case_insensitive(
            field="album", unknown_label="Unknown Album", source=matching
        )

    def tracks_by_album(self, name: str, *, artist: str | None = None) -> list[Track]:
        """Return all tracks on an album, ordered by (disc, tracknumber, path).

        When `artist` is given, only tracks whose artist tag matches are
        returned — used to disambiguate albums that share a name across
        different artists (e.g. "Greatest Hits").
        """
        target = _norm_key(name)
        artist_target = _norm_key(artist) if artist else None
        matches: list[Track] = []
        for t in self.tracks():
            if _norm_key(t.album or "Unknown Album") != target:
                continue
            if artist_target is not None and _norm_key(t.artist or "Unknown Artist") != artist_target:
                continue
            matches.append(t)
        matches.sort(key=lambda t: (
            t.disc_num if t.disc_num is not None else 1,
            t.track_num if t.track_num is not None else 9999,
            t.path,
        ))
        return matches

    def tracks_by_artist(self, name: str) -> list[Track]:
        """Return all tracks by a given artist, grouped by (album, disc, tracknumber)."""
        target = _norm_key(name)
        matches = [
            t for t in self.tracks()
            if _norm_key(t.artist or "Unknown Artist") == target
        ]
        matches.sort(
            key=lambda t: (
                _norm_key(t.album or ""),
                t.disc_num if t.disc_num is not None else 1,
                t.track_num if t.track_num is not None else 9999,
                t.path,
            )
        )
        return matches
