"""
degrade_brief.py

Produce a degraded copy of a research run for testing whether the eval
judges actually discriminate. Takes a run dir, copies its result.json to a
target dir, and strips named sections out of the brief by heading.

This is a test harness, not part of the tool. The point is to confirm the
coverage judge can score below 3 when a brief is genuinely worse. If a
gutted brief still scores 3, the judge is stuck and no staged regression
will show anything.

Usage:
  # See the section headings available to cut
  python degrade_brief.py --run-dir runs/20260627_233402 --list

  # Cut one section into a new run dir
  python degrade_brief.py --run-dir runs/20260627_233402 \
      --out runs/test_degraded --cut "The 5 Core Competitors"

  # Cut several
  python degrade_brief.py --run-dir runs/20260627_233402 \
      --out runs/test_degraded \
      --cut "The 5 Core Competitors" --cut "Biggest Gaps & Opportunities"

Then score the copy:
  python -m evals.run_evals --run-dir runs/test_degraded --skip-langfuse
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path


def find_headings(brief: str) -> list[str]:
    """Return the text of every markdown heading in the brief."""
    return [
        m.group().lstrip("#").strip()
        for m in re.finditer(r"^#{1,4}\s+.*$", brief, re.MULTILINE)
    ]


def cut_section(brief: str, heading_text: str) -> str:
    """
    Remove one section from the brief: the heading line whose text contains
    heading_text, plus everything under it up to the next heading of the
    same or higher level (or end of brief).

    Matching is substring and case-insensitive, so you can pass "Core
    Competitors" instead of the full "The 5 Core Competitors" with its emoji
    and exact spacing.
    """
    lines = brief.splitlines(keepends=True)
    out = []
    i = 0
    cut_count = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m and heading_text.lower() in m.group(2).lower():
            level = len(m.group(1))
            cut_count += 1
            # Skip this heading and everything under it until the next
            # heading at the same or higher level.
            i += 1
            while i < len(lines):
                nxt = re.match(r"^(#{1,4})\s+", lines[i])
                if nxt and len(nxt.group(1)) <= level:
                    break
                i += 1
            continue
        out.append(line)
        i += 1

    if cut_count == 0:
        print(f"  WARNING: no heading matched '{heading_text}' — nothing cut")
    return "".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", "-r", required=True, help="Source run dir containing result.json")
    parser.add_argument("--out", "-o", help="Target run dir for the degraded copy")
    parser.add_argument("--cut", action="append", default=[], help="Heading text to cut (repeatable)")
    parser.add_argument("--list", action="store_true", help="List heading texts in the brief and exit")
    args = parser.parse_args()

    src = Path(args.run_dir)
    result_path = src / "result.json"
    if not result_path.exists():
        print(f"No result.json in {src}")
        sys.exit(1)

    result = json.loads(result_path.read_text(encoding="utf-8"))
    brief = result["brief"]

    if args.list:
        print(f"Headings in {result_path}:")
        for h in find_headings(brief):
            print(f"  - {h}")
        return

    if not args.out:
        print("--out is required unless using --list")
        sys.exit(1)
    if not args.cut:
        print("Pass at least one --cut")
        sys.exit(1)

    original_len = len(brief)
    for heading in args.cut:
        brief = cut_section(brief, heading)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Copy the whole run dir first so tool_calls, trace ids, etc. carry over,
    # then overwrite the brief in the copy. This keeps groundedness source
    # material intact so only the brief content changed.
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, out / item.name)

    result["brief"] = brief
    # Drop the langfuse trace id so the degraded run's scores don't overwrite
    # the real run's trace when posted.
    result.pop("langfuse_trace_id", None)
    (out / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"Wrote degraded copy to {out / 'result.json'}")
    print(f"Brief length: {original_len} -> {len(brief)} chars")


if __name__ == "__main__":
    main()
