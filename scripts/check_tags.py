"""One-off ID3 consistency check across the music library.

Usage:  python scripts/check_tags.py
Reads MUSIC_DIR from .env (same as the bot).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

# Make the project root importable when running as `python scripts/check_tags.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from config import load_config  # noqa: E402
from library import Library, Track  # noqa: E402

TOP_N = 25  # how many examples to print per section before truncating


def fmt_pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total else "0%"


def norm(s: str) -> str:
    """Collapse whitespace and lowercase — used to detect equivalent spellings."""
    return " ".join(s.lower().split())


def section(title: str, count: int) -> None:
    print(f"\n=== {title} ({count}) ===")


def truncated(items: list, n: int) -> None:
    if len(items) > n:
        print(f"  ...and {len(items) - n} more")


def report_coverage(tracks: list[Track]) -> None:
    total = len(tracks)
    have_title = sum(1 for t in tracks if t.title)
    have_artist = sum(1 for t in tracks if t.artist)
    have_album = sum(1 for t in tracks if t.album)
    have_track_num = sum(1 for t in tracks if t.track_num is not None)
    print(f"\n=== Tag coverage (of {total} tracks) ===")
    print(f"  title:       {have_title:>6} ({fmt_pct(have_title, total)})")
    print(f"  artist:      {have_artist:>6} ({fmt_pct(have_artist, total)})")
    print(f"  album:       {have_album:>6} ({fmt_pct(have_album, total)})")
    print(f"  tracknumber: {have_track_num:>6} ({fmt_pct(have_track_num, total)})")


def report_critical_missing(tracks: list[Track]) -> None:
    missing = [t for t in tracks if not t.title or not t.artist]
    section("Tracks missing title or artist", len(missing))
    if not missing:
        print("  (none)")
        return
    for t in missing[:TOP_N]:
        flags = []
        if not t.title:
            flags.append("no title")
        if not t.artist:
            flags.append("no artist")
        print(f"  [{', '.join(flags)}] {t.name}")
    truncated(missing, TOP_N)


def report_duplicate_spellings(tracks: list[Track], field: str, label: str) -> None:
    groups: dict[str, set[str]] = defaultdict(set)
    for t in tracks:
        value = getattr(t, field)
        if value:
            groups[norm(value)].add(value)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    section(f"Likely duplicate {label} spellings", len(dupes))
    if not dupes:
        print("  (none)")
        return
    sorted_dupes = sorted(dupes.values(), key=lambda s: sorted(s)[0].lower())
    for spellings in sorted_dupes[:TOP_N]:
        print(f"  {'  |  '.join(sorted(spellings))}")
    truncated(sorted_dupes, TOP_N)


def report_album_tracknumber_health(tracks: list[Track]) -> None:
    by_album: dict[str, list[Track]] = defaultdict(list)
    for t in tracks:
        if t.album:
            by_album[t.album].append(t)

    findings: list[tuple[str, int, list[str]]] = []
    for album, in_album in by_album.items():
        if len(in_album) < 2:
            continue
        # A multi-disc album with the same tracknumber on different discs is
        # fine — the bot now sorts by (disc, track). Only flag dupes within
        # the same disc.
        nums_by_disc: dict[int, list[int]] = defaultdict(list)
        missing_count = 0
        for t in in_album:
            if t.track_num is None:
                missing_count += 1
                continue
            nums_by_disc[t.disc_num if t.disc_num is not None else 1].append(t.track_num)

        present_total = sum(len(v) for v in nums_by_disc.values())
        problems: list[str] = []

        if present_total and missing_count:
            problems.append(f"mixed: {missing_count}/{len(in_album)} tracks missing tracknumber")
        elif not present_total and len(in_album) >= 3:
            problems.append(f"no tracks have tracknumber ({len(in_album)} tracks)")

        for disc, nums in sorted(nums_by_disc.items()):
            seen: dict[int, int] = defaultdict(int)
            for n in nums:
                seen[n] += 1
            dups = sorted(n for n, c in seen.items() if c > 1)
            if dups:
                disc_label = f"disc {disc}: " if len(nums_by_disc) > 1 else ""
                problems.append(f"{disc_label}duplicate tracknumbers: {dups}")

        if problems:
            findings.append((album, len(in_album), problems))

    findings.sort(key=lambda x: x[0].lower())
    section("Albums with track-number issues", len(findings))
    if not findings:
        print("  (none)")
        return
    for album, count, problems in findings[:TOP_N]:
        print(f"  {album!r} ({count} tracks)")
        for p in problems:
            print(f"    - {p}")
    truncated(findings, TOP_N)


def main() -> None:
    load_dotenv()
    config = load_config()
    library = Library(config.music_dir)
    print(f"Scanning {library.root} ...")
    tracks = library.tracks(force=True)
    if not tracks:
        print("No audio files found.")
        return

    report_coverage(tracks)
    report_critical_missing(tracks)
    report_duplicate_spellings(tracks, "artist", "artist")
    report_duplicate_spellings(tracks, "album", "album")
    report_album_tracknumber_health(tracks)


if __name__ == "__main__":
    main()
