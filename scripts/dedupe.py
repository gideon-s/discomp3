"""Find and (optionally) move byte-identical duplicates in the music library.

Strategy:
  1. Stat every audio file. Group by file size — files with no size-mate can't
     be duplicates, so we skip hashing them.
  2. SHA-256 the files that share a size with at least one other file.
  3. For each group of identical-hash files, pick one to keep (shortest path,
     alphabetical tiebreak) and plan to move the rest to a timestamped trash
     directory outside the music tree (so they don't get re-scanned).

Dry-run by default. Pass --apply to actually move files.

Usage:
  python scripts/dedupe.py            # print the plan, no changes
  python scripts/dedupe.py --apply    # move duplicates to trash
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from config import load_config  # noqa: E402
from library import AUDIO_EXTS  # noqa: E402

TRASH_ROOT = Path.home() / ".local" / "share" / "discomp3-dedupe-trash"
PLAN_PREVIEW = 30  # how many groups to print in the plan


def find_audio_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    )


def hash_file(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def find_duplicate_groups(files: list[Path]) -> dict[str, list[Path]]:
    """Return {hash: [paths]} for hashes with more than one file."""
    by_size: dict[int, list[Path]] = defaultdict(list)
    for p in files:
        try:
            by_size[p.stat().st_size].append(p)
        except OSError:
            pass

    candidates: list[Path] = [p for paths in by_size.values() if len(paths) > 1 for p in paths]
    print(f"  {len(files)} files; {len(candidates)} share a size with another — hashing those...")

    by_hash: dict[str, list[Path]] = defaultdict(list)
    total = len(candidates)
    for i, p in enumerate(candidates, 1):
        try:
            by_hash[hash_file(p)].append(p)
        except OSError as exc:
            print(f"  WARN read failed: {p} ({exc})")
        if i % 50 == 0 or i == total:
            print(f"  hashed {i}/{total}", end="\r", flush=True)
    print()  # newline after progress

    return {h: ps for h, ps in by_hash.items() if len(ps) > 1}


def pick_keeper(paths: list[Path]) -> Path:
    """Shortest path (least nested), alphabetical tiebreak."""
    return min(paths, key=lambda p: (len(str(p)), str(p)))


def fmt_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if f < 1024 or unit == "T":
            return f"{f:.1f}{unit}"
        f /= 1024
    return f"{n}B"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="move dupes to trash (default: dry run)")
    args = ap.parse_args()

    load_dotenv()
    config = load_config()
    root = Path(config.music_dir).expanduser().resolve()
    if not root.exists():
        print(f"Music directory not found: {root}")
        return

    print(f"Scanning {root} ...")
    files = find_audio_files(root)
    if not files:
        print("No audio files found.")
        return

    groups = find_duplicate_groups(files)
    if not groups:
        print("\nNo duplicate files found.")
        return

    # Build (keeper, dupe) plan and tally bytes freed.
    plan: list[tuple[Path, Path]] = []
    bytes_freed = 0
    for paths in groups.values():
        keeper = pick_keeper(paths)
        for p in paths:
            if p == keeper:
                continue
            plan.append((keeper, p))
            try:
                bytes_freed += p.stat().st_size
            except OSError:
                pass

    mode = "APPLYING" if args.apply else "DRY RUN"
    print(
        f"\n=== {mode}: {len(plan)} duplicate file(s) in {len(groups)} group(s) "
        f"— frees ~{fmt_size(bytes_freed)} ===\n"
    )

    sorted_groups = sorted(groups.values(), key=lambda ps: pick_keeper(ps).as_posix().lower())
    for paths in sorted_groups[:PLAN_PREVIEW]:
        keeper = pick_keeper(paths)
        print(f"  KEEP    {keeper.relative_to(root)}")
        for p in sorted(paths):
            if p == keeper:
                continue
            print(f"    dupe  {p.relative_to(root)}")
    if len(sorted_groups) > PLAN_PREVIEW:
        print(f"  ...and {len(sorted_groups) - PLAN_PREVIEW} more groups")

    if not args.apply:
        print("\n(dry run — no files were moved. Re-run with --apply to move duplicates to trash.)")
        return

    trash_dir = TRASH_ROOT / time.strftime("%Y%m%d-%H%M%S")
    trash_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nMoving duplicates to {trash_dir} ...")
    moved = 0
    errors: list[tuple[Path, str]] = []
    for _, dupe in plan:
        try:
            target = trash_dir / dupe.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dupe), str(target))
            moved += 1
        except Exception as exc:  # noqa: BLE001
            errors.append((dupe, str(exc)))

    print(f"\nDone. Moved {moved}/{len(plan)} files. {len(errors)} errors.")
    for path, err in errors[:20]:
        print(f"  ERROR  {path}: {err}")
    if len(errors) > 20:
        print(f"  ...and {len(errors) - 20} more errors")
    print(f"\nTo restore: rsync -a {trash_dir}/ {root}/")
    print(f"To delete:  rm -rf {trash_dir}")


if __name__ == "__main__":
    main()
