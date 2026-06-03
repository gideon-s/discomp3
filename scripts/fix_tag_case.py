"""Normalize artist/album tag case across the library.

For each group of tracks whose artist (or album) tag differs only in case or
whitespace, pick the most-used spelling as canonical and rewrite the outliers
to match. Dry-run by default — pass --apply to actually rewrite files.

Usage:
  python scripts/fix_tag_case.py             # dry run: print the plan
  python scripts/fix_tag_case.py --apply     # rewrite tags on disk
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mutagen  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from config import load_config  # noqa: E402
from library import Library, Track, _norm_key  # noqa: E402


def plan_changes(tracks: list[Track], field: str) -> list[tuple[Path, str, str, str]]:
    """Return (path, field, current_value, canonical_value) for every needed rewrite."""
    by_norm: dict[str, dict[str, list[Track]]] = defaultdict(lambda: defaultdict(list))
    for t in tracks:
        value = getattr(t, field)
        if value:
            by_norm[_norm_key(value)][value].append(t)

    changes: list[tuple[Path, str, str, str]] = []
    for spellings in by_norm.values():
        if len(spellings) <= 1:
            continue
        # Canonical: most files use it; tiebreak alphabetical for determinism.
        canonical = max(spellings, key=lambda s: (len(spellings[s]), s))
        for spelling, ts in spellings.items():
            if spelling == canonical:
                continue
            for t in ts:
                changes.append((Path(t.path), field, spelling, canonical))
    return changes


def apply_change(path: Path, field: str, new_value: str) -> str | None:
    """Rewrite a single tag in place. Returns an error string on failure, None on success."""
    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception as exc:  # noqa: BLE001
        return f"open failed: {exc}"
    if audio is None:
        return "no tag container"
    try:
        audio[field] = [new_value]
        audio.save()
    except Exception as exc:  # noqa: BLE001
        return f"write failed: {exc}"
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually rewrite tags (default: dry run)")
    args = ap.parse_args()

    load_dotenv()
    config = load_config()
    library = Library(config.music_dir)
    print(f"Scanning {library.root} ...")
    tracks = library.tracks(force=True)

    all_changes = plan_changes(tracks, "artist") + plan_changes(tracks, "album")
    if not all_changes:
        print("No case inconsistencies found — nothing to do.")
        return

    # Bucket by (field, from, to) so the plan is readable even if many files share a rule.
    by_rule: dict[tuple[str, str, str], list[Path]] = defaultdict(list)
    for path, field, current, new in all_changes:
        by_rule[(field, current, new)].append(path)

    mode = "APPLYING" if args.apply else "DRY RUN"
    print(f"\n=== {mode}: {len(all_changes)} file(s), {len(by_rule)} rule(s) ===\n")
    for (field, current, new), paths in sorted(by_rule.items()):
        print(f"  {field:6s}  {current!r}  ->  {new!r}   ({len(paths)} files)")

    if not args.apply:
        print("\n(dry run — no files were modified. Re-run with --apply to write changes.)")
        return

    print("\nRewriting...")
    errors: list[tuple[Path, str]] = []
    for path, field, _, new in all_changes:
        err = apply_change(path, field, new)
        if err:
            errors.append((path, err))

    ok = len(all_changes) - len(errors)
    print(f"\nDone. {ok} succeeded, {len(errors)} failed.")
    for path, err in errors[:20]:
        print(f"  ERROR  {path.name}: {err}")
    if len(errors) > 20:
        print(f"  ...and {len(errors) - 20} more errors")


if __name__ == "__main__":
    main()
