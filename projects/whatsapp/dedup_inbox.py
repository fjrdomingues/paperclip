#!/usr/bin/env python3
"""
Safe inbox.jsonl deduplication utility.

Deduplicates projects/whatsapp/data/inbox.jsonl by Twilio SID (field: "sid"),
preserving first-seen record order.

Modes:
  --dry-run (default): Operate on a temp copy; print before/after stats. No live file mutation.
  --apply: Write the deduped output to the live inbox.jsonl (atomic rename).

Usage:
  python dedup_inbox.py [--dry-run] [--input PATH] [--output PATH]
  python dedup_inbox.py --apply
"""

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DEFAULT_INBOX = SCRIPT_DIR / "data" / "inbox.jsonl"


def dedup_jsonl(input_path: Path) -> tuple[list[str], list[str], dict]:
    """
    Read JSONL, deduplicate by 'sid', preserve first-seen order.

    Returns:
        original_lines: all raw lines (stripped, non-empty)
        deduped_lines: lines after dedup
        stats: counts for reporting
    """
    original_lines = []
    deduped_lines = []
    seen_sids = set()
    no_sid_count = 0
    dup_count = 0

    with open(input_path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue
            original_lines.append(line)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Keep malformed lines as-is (don't silently drop)
                deduped_lines.append(line)
                continue
            sid = msg.get("sid", "")
            if not sid:
                no_sid_count += 1
                deduped_lines.append(line)
                continue
            if sid in seen_sids:
                dup_count += 1
                continue
            seen_sids.add(sid)
            deduped_lines.append(line)

    stats = {
        "total_before": len(original_lines),
        "total_after": len(deduped_lines),
        "removed": len(original_lines) - len(deduped_lines),
        "unique_sids": len(seen_sids),
        "duplicate_rows_removed": dup_count,
        "no_sid_rows": no_sid_count,
    }
    return original_lines, deduped_lines, stats


def write_lines(lines: list[str], dest: Path) -> None:
    """Write lines to dest atomically via a .tmp file."""
    tmp = dest.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    tmp.rename(dest)


def main():
    parser = argparse.ArgumentParser(description="Deduplicate inbox.jsonl by Twilio SID")
    parser.add_argument("--apply", action="store_true",
                        help="Write deduped output to the live inbox.jsonl (atomic). Without this flag, runs dry.")
    parser.add_argument("--input", default=str(DEFAULT_INBOX),
                        help=f"Input JSONL path (default: {DEFAULT_INBOX})")
    parser.add_argument("--output", default=None,
                        help="Output path for dry-run (default: <input>.deduped.jsonl)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Input:  {input_path}")

    original_lines, deduped_lines, stats = dedup_jsonl(input_path)

    print(f"\nBefore: {stats['total_before']} rows, {stats['unique_sids']} unique SIDs + {stats['no_sid_rows']} no-SID rows")
    print(f"After:  {stats['total_after']} rows")
    print(f"Removed {stats['duplicate_rows_removed']} duplicate rows")

    if stats["removed"] == 0:
        print("\nNo duplicates found — inbox.jsonl is already clean.")
        return

    if args.apply:
        write_lines(deduped_lines, input_path)
        print(f"\nApplied: {input_path} updated in-place (atomic rename).")
        print(f"  {stats['total_before']} rows → {stats['total_after']} rows ({stats['removed']} removed)")
    else:
        # Dry-run: write to a temp copy for inspection
        if args.output:
            out_path = Path(args.output)
        else:
            out_path = input_path.with_suffix(".deduped.jsonl")
        write_lines(deduped_lines, out_path)
        print(f"\nDry-run: deduped copy written to {out_path}")
        print("Re-run with --apply to update the live file.")


if __name__ == "__main__":
    main()
