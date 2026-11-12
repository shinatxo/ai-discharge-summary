"""Item 3 R1 step 4 — the fresh rulings store (decision-2 Round 3, D3'; session 3's spec; step-4
Rounds 1-3).

The R1 candidate is ruled in its OWN store, so no p0.4 ruling can attach to a renderer sentence:

    python evals/r1_rulings_store.py build --out <store> --batch <renderer batch>
    python evals/r1_rulings_store.py check <store>

`build` runs AFTER the two renders and BEFORE the first scoring (Codex 17:51 / 18:02). It reads only
the batch's manifest.json and batch.json (the scenario list and identities) — no record, no output.
For each scenario it reads the historical file evals/gold/adjudications/<ID>.json ONCE, records its
sha256, and writes <store>/<ID>.json holding `scenario_id`, the three upstream families (`rulings`,
`med_rulings`, `compose_rulings`) copied unchanged, and `patient_rulings: {}`. Not copied:
`patient_rulings`, `rubric_rulings`, every `*_meta`. <store>/STORE.json records the store version,
the batch (id, manifest and batch.json sha256), the freeze sha256 the batch carries and each source
file's sha256.
It refuses if <store> exists or is the historical folder or inside it. The historical store is
never written. A scenario id becomes a file name, so every id is checked BEFORE any historical read or
any output is created (Codex 12 Nov 06:42): a plain name only (letters, digits, "-" and "_"; no path
separator, no dot, not absolute, not STORE / RULINGS), no two ids differing only by case, and each
file path must resolve directly inside its own folder. The same scenario id on several runs is one
store file, as before.

`check` -> problems ([] = clean): every store file's copied families still equal the historical
file at the recorded sha256; `scenario_id` matches; no key beyond `scenario_id`, the three
families, `patient_rulings` and `patient_rulings_meta`; every patient ruling is acceptable / defect.

score_batch opens only <store>/<ID>.json, so STORE.json (and RULINGS.json, written later by
`r1_go_stop.py bind-rulings`) never reach the scorer. The console prints counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORICAL = ROOT / "evals" / "gold" / "adjudications"
STORE_VERSION = 1
FAMILIES = ("rulings", "med_rulings", "compose_rulings")          # copied unchanged
ALLOWED_KEYS = {"scenario_id", *FAMILIES, "patient_rulings", "patient_rulings_meta"}
RULINGS = ("acceptable", "defect")
SCENARIO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")       # a scenario id is used as a file name
RESERVED = frozenset({"store", "rulings"})                        # STORE.json / RULINGS.json live beside the ids
NOTE = "patient_rulings empty at creation — rulings entered by Shina (author/CSO) only"


class StoreError(ValueError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def safe_scenario_id(sid) -> bool:
    """True if `sid` can only ever name one file directly inside a folder."""
    return isinstance(sid, str) and SCENARIO_ID.fullmatch(sid) is not None and sid.lower() not in RESERVED


def scenario_ids(index: dict) -> list[str]:
    """The batch's scenario ids, each checked before it is used as a file name. Repeats (the same
    scenario on several runs) are one id."""
    sids = set()
    for g in index["generations"]:
        sid = g.get("scenario_id") if isinstance(g, dict) else None
        if not safe_scenario_id(sid):
            raise StoreError("a scenario id in batch.json is not a plain name (letters, digits, - and _ only)")
        sids.add(sid)
    if len({s.lower() for s in sids}) != len(sids):
        raise StoreError("two scenario ids in batch.json differ only by case")
    return sorted(sids)


def _inside(folder: Path, sid: str) -> Path:
    """<folder>/<sid>.json, refused unless it resolves to a file directly inside <folder>."""
    p = folder / f"{sid}.json"
    if p.resolve().parent != folder:
        raise StoreError("a scenario file would resolve outside its folder")
    return p


def build(out: Path, batch_dir: Path, historical: Path = HISTORICAL, freeze_sha256: str | None = None) -> dict:
    out, historical = out.resolve(), historical.resolve()
    if out.exists():
        raise StoreError("the store already exists — never written over")
    if out == historical or historical in out.parents:
        raise StoreError("the store must not be the historical folder or inside it")
    try:
        mb = (batch_dir / "manifest.json").read_bytes()
        bb = (batch_dir / "batch.json").read_bytes()
        manifest, index = json.loads(mb), json.loads(bb)
        gens = index["generations"]
        if not isinstance(gens, list):
            raise TypeError
    except (OSError, ValueError, KeyError, TypeError):
        raise StoreError("not a readable batch (manifest.json / batch.json)") from None
    if (manifest.get("derived_from") or {}).get("kind") != "offline_renderer":
        raise StoreError("not a renderer batch")
    sids = scenario_ids(index)                                       # checked before any read or write below
    paths = {sid: (_inside(historical, sid), _inside(out, sid)) for sid in sids}
    files, sources = {}, {}
    for sid in sids:
        p = paths[sid][0]
        doc = {"scenario_id": sid}
        if p.is_file():
            hb = p.read_bytes()                                      # read once: the bytes hashed are the bytes copied
            hist = json.loads(hb)
            copied = [f for f in FAMILIES if f in hist]
            for f in copied:
                doc[f] = hist[f]
            sources[sid] = {"source_sha256": _sha(hb), "families_copied": copied,
                            "not_copied": sorted(k for k in hist if k not in FAMILIES and k != "scenario_id")}
        else:
            sources[sid] = {"source_sha256": None, "families_copied": [], "not_copied": []}
        doc["patient_rulings"] = {}
        files[sid] = doc
    out.mkdir(parents=True)
    for sid, doc in files.items():
        paths[sid][1].write_text(_dump(doc), encoding="utf-8")
    store = {"store_version": STORE_VERSION, "batch_id": manifest.get("batch_id"),
             "manifest_sha256": _sha(mb), "batch_sha256": _sha(bb),
             "freeze_sha256": freeze_sha256 if freeze_sha256 is not None
             else (manifest.get("derived_from") or {}).get("freeze_sha256"),
             "note": NOTE, "scenarios": sources}
    (out / "STORE.json").write_text(_dump(store), encoding="utf-8")
    return store


def load_store(store: Path) -> dict:
    try:
        return json.loads((store / "STORE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise StoreError("STORE.json not readable") from None


def check(store: Path, historical: Path = HISTORICAL) -> list[str]:
    meta = load_store(store)
    probs: list[str] = []
    if meta.get("store_version") != STORE_VERSION or not isinstance(meta.get("scenarios"), dict):
        return ["STORE.json:shape"]
    if not all(safe_scenario_id(s) for s in meta["scenarios"]):
        return ["STORE.json:scenario_id"]
    present = {p.stem for p in store.glob("*.json")} - {"STORE", "RULINGS"}
    probs += [f"{s}:file_missing" for s in sorted(set(meta["scenarios"]) - present)]
    probs += [f"{s}:file_not_in_STORE" for s in sorted(present - set(meta["scenarios"]))]
    for sid in sorted(set(meta["scenarios"]) & present):
        src = meta["scenarios"][sid]
        try:
            doc = json.loads((store / f"{sid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            probs.append(f"{sid}:not_readable")
            continue
        if not isinstance(doc, dict) or doc.get("scenario_id") != sid:
            probs.append(f"{sid}:scenario_id")
            continue
        probs += [f"{sid}:key_not_allowed:{k}" for k in sorted(set(doc) - ALLOWED_KEYS)]
        pr = doc.get("patient_rulings")
        if not isinstance(pr, dict):
            probs.append(f"{sid}:patient_rulings")
        else:
            probs += [f"{sid}:ruling_value:{k}" for k, v in sorted(pr.items()) if v not in RULINGS]
        hp = historical / f"{sid}.json"
        if src.get("source_sha256") is None:
            if hp.is_file() or any(f in doc for f in FAMILIES):
                probs.append(f"{sid}:historical_source")
            continue
        try:
            hb = hp.read_bytes()
        except OSError:
            probs.append(f"{sid}:historical_missing")
            continue
        if _sha(hb) != src["source_sha256"]:
            probs.append(f"{sid}:historical_changed")
            continue
        hist = json.loads(hb)
        for f in FAMILIES:
            if (f in hist) != (f in doc) or (f in hist and hist[f] != doc[f]):
                probs.append(f"{sid}:family_differs:{f}")
    return probs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--batch", type=Path, required=True)
    c = sub.add_parser("check")
    c.add_argument("store", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "build":
            s = build(a.out, a.batch)
            n = len(s["scenarios"])
            copied = sum(1 for x in s["scenarios"].values() if x["source_sha256"])
            print(f"store: {n} scenarios, {copied} with upstream families copied, patient_rulings empty -> {a.out}")
            return 0
        probs = check(a.store)
    except StoreError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for p in probs:
        print(p)
    print(f"store check: {len(probs)} problem(s).")
    return 0 if not probs else 1


if __name__ == "__main__":
    sys.exit(main())
