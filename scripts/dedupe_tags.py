"""Find candidate duplicates by (artist, title, duration).

Two files matching on case-/whitespace-insensitive (artist, title) AND with
the same audio duration (within DURATION_TOLERANCE seconds) are treated as
the same song encoded differently (e.g. 192 vs 320 kbps MP3, or MP3 vs FLAC).
The highest-quality copy is kept; the rest move to a timestamped trash dir.

The duration check is important: the same song title can appear as both a
studio version and a live recording — same artist+title tags, but different
length. Without it, the script would silently delete distinct recordings.

Quality ranking (highest wins):
  1. Lossless (FLAC, WAV) beats lossy
  2. Higher bitrate
  3. Larger file size
  4. Shorter path (less nested)

Usage:
  python scripts/dedupe_tags.py                  # dry run, scan everything
  python scripts/dedupe_tags.py --apply          # move dupes to trash
  python scripts/dedupe_tags.py --artist M83     # restrict to one artist
  python scripts/dedupe_tags.py --no-duration    # skip duration check (DANGEROUS)
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mutagen  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from config import load_config  # noqa: E402
from library import Library, Track, _norm_key  # noqa: E402

TRASH_ROOT = Path.home() / ".local" / "share" / "discomp3-dedupe-trash"
LOSSLESS_EXTS = {".flac", ".wav"}
PLAN_PREVIEW = 60
DURATION_TOLERANCE = 2.0  # seconds — same recording across encodings stays within this


def read_quality(path: str) -> tuple[bool, int, int, float]:
    """Return (is_lossless, bitrate_bps, file_size, length_seconds). Zeros on read failure."""
    is_lossless = Path(path).suffix.lower() in LOSSLESS_EXTS
    bitrate = 0
    length = 0.0
    try:
        audio = mutagen.File(path)
        if audio is not None and audio.info is not None:
            bitrate = int(getattr(audio.info, "bitrate", 0) or 0)
            length = float(getattr(audio.info, "length", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        pass
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = 0
    return is_lossless, bitrate, size, length


def fmt_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if f < 1024 or unit == "T":
            return f"{f:.1f}{unit}"
        f /= 1024
    return f"{n}B"


def format_label(path: str, qual: tuple[bool, int, int, float]) -> str:
    """e.g. '[FLAC 1411kbps  32.5M  3:42]'"""
    _, bitrate, size, length = qual
    fmt = Path(path).suffix.lstrip(".").upper()
    br = f"{bitrate // 1000}kbps" if bitrate else "?kbps"
    mins, secs = divmod(int(length), 60)
    duration = f"{mins}:{secs:02d}" if length else "?:??"
    return f"[{fmt:<4} {br:>8} {fmt_size(size):>7} {duration:>5}]"


def pick_keeper(group: list[tuple[Track, tuple[bool, int, int, float]]]) -> Track:
    """Best (is_lossless, bitrate, size) wins; tiebreak: shortest path. Length is ignored."""
    return max(group, key=lambda tq: (*tq[1][:3], -len(tq[0].path)))[0]


def bucket_by_duration(
    items: list[tuple[Track, tuple[bool, int, int, float]]],
    tolerance: float,
) -> list[list[tuple[Track, tuple[bool, int, int, float]]]]:
    """Partition items so each bucket contains files within `tolerance` seconds.

    Greedy: sort by length, start a new bucket whenever the gap from the last
    item exceeds the tolerance. Files with length=0 (unreadable) all go into
    the zero bucket together.
    """
    if not items:
        return []
    sorted_items = sorted(items, key=lambda tq: tq[1][3])
    buckets: list[list[tuple[Track, tuple[bool, int, int, float]]]] = [[sorted_items[0]]]
    for item in sorted_items[1:]:
        last_length = buckets[-1][-1][1][3]
        if item[1][3] - last_length <= tolerance:
            buckets[-1].append(item)
        else:
            buckets.append([item])
    return buckets


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="move dupes to trash (default: dry run)")
    ap.add_argument("--artist", help="restrict to one artist (case-/whitespace-insensitive)")
    ap.add_argument(
        "--no-duration",
        action="store_true",
        help="disable the duration check (DANGEROUS — may flag live versions as dupes of studio)",
    )
    args = ap.parse_args()

    load_dotenv()
    config = load_config()
    library = Library(config.music_dir)
    print(f"Scanning {library.root} ...")
    tracks = library.tracks(force=True)

    artist_filter = _norm_key(args.artist) if args.artist else None

    by_key: dict[tuple[str, str], list[Track]] = defaultdict(list)
    for t in tracks:
        if not t.artist or not t.title:
            continue
        if artist_filter and _norm_key(t.artist) != artist_filter:
            continue
        by_key[(_norm_key(t.artist), _norm_key(t.title))].append(t)

    candidates = {k: v for k, v in by_key.items() if len(v) > 1}
    if not candidates:
        scope = f" for artist {args.artist!r}" if args.artist else ""
        print(f"\nNo (artist, title) duplicate groups found{scope}.")
        return

    total_candidate_files = sum(len(v) for v in candidates.values())
    print(f"  reading audio info for {total_candidate_files} candidate files...")

    # Sub-group each (artist, title) bucket by duration so that distinct
    # recordings (studio vs live, radio edit vs album version) stay separate.
    enriched: list[tuple[tuple[str, str], list[tuple[Track, tuple[bool, int, int, float]]]]] = []
    plan: list[tuple[Track, Track]] = []
    bytes_freed = 0
    rejected_by_duration = 0

    for key, group in candidates.items():
        group_q = [(t, read_quality(t.path)) for t in group]
        if args.no_duration:
            buckets = [group_q]
        else:
            buckets = bucket_by_duration(group_q, DURATION_TOLERANCE)
        for sub in buckets:
            if len(sub) < 2:
                rejected_by_duration += 1
                continue
            keeper = pick_keeper(sub)
            for t, _ in sub:
                if t == keeper:
                    continue
                plan.append((keeper, t))
                try:
                    bytes_freed += Path(t.path).stat().st_size
                except OSError:
                    pass
            enriched.append((key, sub))

    mode = "APPLYING" if args.apply else "DRY RUN"
    scope = f" within {args.artist!r}" if args.artist else ""
    duration_note = ""
    if not args.no_duration and rejected_by_duration:
        duration_note = (
            f" (skipped {rejected_by_duration} subgroup(s) that differed in duration "
            f"— likely live/edit variants)"
        )
    print(
        f"\n=== {mode}{scope}: {len(plan)} duplicate file(s) across {len(enriched)} "
        f"group(s) — frees ~{fmt_size(bytes_freed)}{duration_note} ===\n"
    )

    enriched.sort(key=lambda kg: kg[0])
    for (artist, title), group_q in enriched[:PLAN_PREVIEW]:
        keeper = pick_keeper(group_q)
        print(f"  {artist}  —  {title}")
        ordered = sorted(group_q, key=lambda tq: (tq[0] != keeper, tq[0].path))
        for t, qual in ordered:
            label = "KEEP " if t == keeper else " dupe"
            rel = Path(t.path).relative_to(library.root)
            print(f"    {label}  {format_label(t.path, qual)}  {rel}")
    if len(enriched) > PLAN_PREVIEW:
        print(f"  ...and {len(enriched) - PLAN_PREVIEW} more groups")

    if not args.apply:
        print("\n(dry run — no files were moved. Re-run with --apply to move duplicates to trash.)")
        return

    trash_dir = TRASH_ROOT / (time.strftime("%Y%m%d-%H%M%S") + "-tags")
    trash_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nMoving duplicates to {trash_dir} ...")
    moved = 0
    errors: list[tuple[Path, str]] = []
    for _, dupe in plan:
        src = Path(dupe.path)
        try:
            target = trash_dir / src.relative_to(library.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            moved += 1
        except Exception as exc:  # noqa: BLE001
            errors.append((src, str(exc)))

    print(f"\nDone. Moved {moved}/{len(plan)} files. {len(errors)} errors.")
    for path, err in errors[:20]:
        print(f"  ERROR  {path}: {err}")
    if len(errors) > 20:
        print(f"  ...and {len(errors) - 20} more errors")
    print(f"\nTo restore: rsync -a {trash_dir}/ {library.root}/")
    print(f"To delete:  rm -rf {trash_dir}")


if __name__ == "__main__":
    main()
