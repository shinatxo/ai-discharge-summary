#!/usr/bin/env python3
"""
Regenerate src/canary/scenarios.json from the eval scenario markdown.

The synthetic-traffic canary (src/canary/app.py) ships a bundled JSON of the 18
scenario INPUT blocks so it has no runtime dependency on the evals/ tree. This
script is the single source of truth for that bundle: run it whenever the
scenario inputs change.

    python src/canary/build_scenarios.py            # writes src/canary/scenarios.json
    python src/canary/build_scenarios.py --check    # verify the bundle is current (CI)

The 18-scenario corpus, with canonical ids:
  - discharge-eval-scenarios.md : "## Scenario 1..4"  -> S1..S4
  - adversarial-scenarios.md    : "## Scenario A5/B6/C7" -> A5/B6/C7
  - eval-scenarios-expansion.md : "# S8..S18"         -> S8..S18
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCEN_DIR = REPO / "evals" / "scenarios"
OUT_PATH = Path(__file__).resolve().parent / "scenarios.json"

SOURCES = [
    "discharge-eval-scenarios.md",
    "adversarial-scenarios.md",
    "eval-scenarios-expansion.md",
]

# Heading that introduces a scenario, capturing its id token.
#   "## Scenario 1 — ..."   -> "1"
#   "## Scenario A5 — ..."  -> "A5"
#   "# S16 — ..."           -> "S16"
_HEADING = re.compile(r"^#{1,3}\s+(?:Scenario\s+)?([A-Z]?\d+)\b\s*(?:[—-]\s*(.*))?$")
_INPUT = re.compile(r"^###\s+INPUT\b")
_NEXT_SECTION = re.compile(r"^###\s+")


def _norm_id(tok: str) -> str:
    """'1' -> 'S1'; 'A5' -> 'A5'; 'S16' -> 'S16'."""
    return tok if tok[0].isalpha() else f"S{tok}"


def extract(md_path: Path):
    lines = md_path.read_text(encoding="utf-8").splitlines()
    out = []
    cur_id = cur_title = None
    i = 0
    while i < len(lines):
        line = lines[i]
        h = _HEADING.match(line)
        if h:
            cur_id = _norm_id(h.group(1))
            cur_title = (h.group(2) or "").strip()
            i += 1
            continue
        if _INPUT.match(line) and cur_id:
            # Collect everything until the next '### ' section.
            j = i + 1
            buf = []
            while j < len(lines) and not _NEXT_SECTION.match(lines[j]):
                buf.append(lines[j])
                j += 1
            notes = "\n".join(buf).strip()
            # Strip a leading fenced code block wrapper if present.
            if notes.startswith("```"):
                notes = re.sub(r"^```[^\n]*\n", "", notes)
                notes = re.sub(r"\n```\s*$", "", notes)
            if notes:
                out.append({"id": cur_id, "title": cur_title, "notes": notes.strip()})
            i = j
            continue
        i += 1
    return out


def build():
    scenarios = []
    seen = set()
    for name in SOURCES:
        for s in extract(SCEN_DIR / name):
            if s["id"] in seen:
                raise SystemExit(f"duplicate scenario id {s['id']} in {name}")
            seen.add(s["id"])
            scenarios.append(s)
    # Sort: numeric S-ids first by number, then lettered ids, stable.
    def sort_key(s):
        m = re.match(r"^([A-Z]?)(\d+)$", s["id"])
        prefix, num = m.group(1), int(m.group(2))
        return (0 if prefix in ("", "S") else 1, num, prefix)
    scenarios.sort(key=sort_key)
    return scenarios


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify scenarios.json matches the markdown (exit 1 if stale)")
    args = ap.parse_args()

    scenarios = build()
    payload = json.dumps({"scenarios": scenarios}, indent=2, ensure_ascii=False) + "\n"

    if args.check:
        current = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if current != payload:
            print(f"scenarios.json is STALE — run: python {Path(__file__).name}", file=sys.stderr)
            sys.exit(1)
        print(f"scenarios.json current ({len(scenarios)} scenarios).")
        return

    OUT_PATH.write_text(payload, encoding="utf-8")
    ids = ", ".join(s["id"] for s in scenarios)
    print(f"Wrote {OUT_PATH.relative_to(REPO)} — {len(scenarios)} scenarios: {ids}")


if __name__ == "__main__":
    main()
