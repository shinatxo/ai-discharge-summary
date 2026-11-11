#!/usr/bin/env python3
"""Item 3 R1, step 3 (D4) — the ONE input-side coverage pass for the renderer's tables.

    python evals/render_coverage.py <batch> --inputs <inputs.json> --tables <tables_dir> --out <coverage.json>

What it reads, per record, and nothing else: exactly
    compose_patient.patient_input(record["steps"]["compose_clinical"])["sections"]
— step 7's A-only input (the six given PART A headings; "Not documented" sections excluded). No other
field of a record (notes, facts, step 7's or 8b's outputs, scores) is read for vocabulary, and
render_patient is never called: nothing is rendered.

The batch is read ONCE and checked against <inputs.json> (render_offline.py inputs) first, so the scan
covers exactly the hashed inputs; any mismatch refuses before a word is read.

Word rules = the renderer's own (render_patient._render's unknown-word rule): words are
render_patient.WORD tokens; a token is UNKNOWN when it is purely alphabetic and its lower case is in none
of the general word list, meaning.py's stop list and the renderer's fillers — and (coverage) it lies in no
match of a glossary, drug-class or person / literal phrase form, no inferred bracket and no diagnosis
label. Tokens with a letter that are not purely alphabetic ("CURB-65", "co-amoxiclav", "x2") are never
flagged by that rule; uncovered ones are listed separately, for information. Tokens with no letter are
ignored. Drop tables are NOT consulted (no count of drops, refusals, forms or screening hits — the
refusal rate is what the run measures, Round 1, 10 Nov).

Output: terms and counts only — never a sentence, a line, a position or a record id:
    {"unknown": [{"term", "forms", "occurrences", "records", "scenarios", "headings"}], "other_tokens": [...],
     counts, the tables' sha256, the inputs file's sha256}. The console prints counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "evals"))

import render_offline as RO  # noqa: E402
from pipeline import compose_patient as CP7  # noqa: E402
from pipeline import meaning as MN  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402

COVERAGE_VERSION = 1


class CoverageError(ValueError):
    pass


def covered_spans(text: str, heading: str, tables: RP.Tables) -> list[tuple[int, int]]:
    """Spans of one LINE that a table entry would transform: glossary / drug forms, person / literal
    phrase forms, the inferred bracket, a diagnosis label at the line start."""
    spans = [(m.start(), m.end()) for ent in tables.glossary + tables.drug_classes for rx in ent.regexes
             for m in rx.finditer(text)]
    spans += [(m.start(), m.end()) for ent in tables.phrases for rx in ent.regexes for m in rx.finditer(text)]
    spans += [(m.start(), m.end()) for m in RP.INFERRED_BRACKET.finditer(text)]
    if heading == "diagnosis":
        m = RP.LABEL.match(text)
        if m:
            spans.append((0, m.end()))
    return spans


def classify(word: str, tables: RP.Tables) -> str | None:
    """'unknown' / 'other' / None (ignored) for one renderer WORD token, outside every covered span."""
    if not any(ch.isalpha() for ch in word):
        return None
    if word.isalpha():
        lw = word.lower()
        if lw in tables.general_words or lw in MN._STOP or lw in RP.FILLERS:
            return None
        return "unknown"
    return "other"


def scan_sections(sections: list[dict], tables: RP.Tables):
    """(kind, surface word, heading) for every uncovered word of the given sections."""
    for sec in sections:
        heading = sec["heading"]
        for line in sec["text"].split("\n"):
            spans = covered_spans(line, heading, tables)
            for m in RP.WORD.finditer(line):
                if any(a <= m.start() and m.end() <= b for a, b in spans):
                    continue
                kind = classify(m.group(0), tables)
                if kind:
                    yield kind, m.group(0), heading


def coverage(src: Path, inputs_path: Path, tables: RP.Tables) -> dict:
    src = src.resolve()
    manifest, _index, mb, bb, rels, rbs = RO._read_source(src)        # read ONCE (render_offline's own reader)
    try:
        ib = inputs_path.read_bytes()
        inputs = json.loads(ib)
    except (OSError, ValueError):
        raise CoverageError("inputs file not readable") from None
    if inputs != RO._inputs_from(manifest, mb, bb, rels, rbs):
        raise CoverageError("the inputs file does not match this batch (records or hashes changed)")
    occ: dict[tuple[str, str], Counter] = defaultdict(Counter)
    recs: dict[tuple[str, str], set] = defaultdict(set)
    scen: dict[tuple[str, str], set] = defaultdict(set)
    forms: dict[tuple[str, str], set] = defaultdict(set)
    n_with, n_sections, n_words = 0, 0, 0
    for rel, rb, gi in zip(rels, rbs, inputs["generations"]):
        if gi["input_sha256"] is None:
            continue
        rec = json.loads(rb)
        sections = CP7.patient_input(rec["steps"]["compose_clinical"])["sections"]    # exactly step 7's input
        n_with += 1
        n_sections += len(sections)
        sid = rel.split("/", 1)[1].rsplit(".", 1)[0]
        for sec in sections:
            n_words += sum(1 for _ in RP.WORD.finditer(sec["text"]))
        for kind, w, heading in scan_sections(sections, tables):
            key = (kind, w.lower())
            occ[key][heading] += 1
            recs[key].add(rel)
            scen[key].add(sid)
            forms[key].add(w)
    rows: dict[str, list] = {"unknown": [], "other": []}
    for (kind, term), hc in occ.items():
        rows[kind].append({"term": term, "forms": sorted(forms[(kind, term)]), "occurrences": sum(hc.values()),
                           "records": len(recs[(kind, term)]), "scenarios": len(scen[(kind, term)]),
                           "headings": dict(sorted(hc.items()))})
    for k in rows:
        rows[k].sort(key=lambda r: (-r["records"], -r["occurrences"], r["term"]))
    found = {"coverage_version": COVERAGE_VERSION, "source_batch": manifest.get("batch_id"),
             "inputs_sha256": hashlib.sha256(ib).hexdigest(), "tables": dict(sorted(tables.sha256.items())),
             "tables_status": tables.status,
             "counts": {"records": len(rels), "records_with_input": n_with, "sections_scanned": n_sections,
                        "words_scanned": n_words, "unknown_terms": len(rows["unknown"]),
                        "unknown_occurrences": sum(r["occurrences"] for r in rows["unknown"]),
                        "other_tokens": len(rows["other"])},
             "unknown": rows["unknown"], "other_tokens": rows["other"]}
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("batch", type=Path)
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument("--tables", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        if a.out.exists():
            raise CoverageError("output file already exists — never written over")
        tables = RP.load_tables(a.tables, pins=None)
        data = coverage(a.batch, a.inputs, tables)
    except (CoverageError, RO.HarnessError, RP.RenderRefusal) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    a.out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    c = data["counts"]
    print(f"coverage: {c['records']} records, {c['records_with_input']} with a step-7 input, "
          f"{c['unknown_terms']} unknown terms ({c['unknown_occurrences']} occurrences), "
          f"{c['other_tokens']} other tokens -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
