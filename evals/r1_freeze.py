"""Item 3 R1 step 4 — the freeze record (decision D6 (a), Shina 11 Nov 2026).

A freeze record pins, by sha256, everything the one measurement run and its go / stop decision
depend on: the pipeline package (`pipeline_version` hashes every .py in it — the renderer and all it
imports), the nine approved tables, every evals/*.py (harness, evaluator, locator, store builder,
scorer v4 and what it imports), gold and the historical rulings store, the scenario corpus, the case
list, the measurement inputs file and the R1 tests / fixtures.

    make(cases)   -> the record (a dict); written by `r1_go_stop.py freeze`
    check(path)   -> (sha256 of the record file, the record, problems) — problems == [] when every
                     pin matches the files on disk now

Used by evals/render_offline.py (it refuses the measurement batch without a matching record and
writes the record's sha256 into the batch) and by evals/r1_go_stop.py (it refuses a batch whose
record does not match).

What a matching record does NOT show: that it was committed and pushed (the guarded commit's
receipt shows that), or that nothing rendered the 90 before it was written. A direct call to
`render_patient` goes round the harness.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline import render_patient as RP  # noqa: E402

FREEZE_VERSION = 1
MEASUREMENT_BATCH = "w6-pipeline-e09-m01-c02-p04-x5"      # D3': the 90 saved p0.4 records
MEASUREMENT_INPUTS = "evals/r1/inputs.json"               # their record + input hashes (no text)
MEASUREMENT_INPUTS_SHA256 = "028b6ae2d276225b5fc526bb5ae576809dee48fa461f11edec8a20276f98d8c1"
MEASUREMENT_CASES = "evals/r1/cases.json"
PIN_GLOBS = ("evals/*.py", "evals/gold/*.json", "evals/gold/adjudications/*.json", "evals/scenarios/*.md",
             MEASUREMENT_INPUTS, "tests/test_r1_*.py", "tests/r1_fixture.py", "tests/test_render_offline.py",
             "tests/test_pipeline_render_patient.py", "tests/test_render_tables_approved.py",
             "tests/fixtures/render_tables_*.json")


class FreezeError(ValueError):
    pass


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_file(p: Path) -> str:
    return sha_bytes(p.read_bytes())


def file_pins(root: Path = ROOT) -> dict:
    out = {}
    for pattern in PIN_GLOBS:
        for p in sorted(root.glob(pattern)):
            if p.is_file():
                out[p.relative_to(root).as_posix()] = sha_file(p)
    return dict(sorted(out.items()))


def identity() -> dict:
    """The code and tables as they are on disk now. Raises (tables_changed) if a table is not the approved one."""
    from pipeline import orchestrate as O
    shas, statuses = {}, set()
    for name in RP.TABLE_FILES:                       # the bytes on disk, without compiling the tables
        try:
            b = (RP.TABLES_DIR / name).read_bytes()
        except OSError:
            raise RP.RenderRefusal("tables_invalid") from None
        shas[name] = sha_bytes(b)
        if name.endswith(".json"):
            try:
                statuses.add(json.loads(b).get("status"))
            except ValueError:
                raise RP.RenderRefusal("tables_invalid") from None
    if shas != RP.TABLE_SHA256:
        raise RP.RenderRefusal("tables_changed")
    return {"pipeline_version": O.pipeline_version(),
            "renderer": {"renderer": RP.RENDERER_NAME, "version": RP.RENDERER_VERSION,
                         "module_sha256": RP.module_sha256(), "tables": dict(sorted(shas.items())),
                         "tables_status": statuses.pop() if len(statuses) == 1 else "mixed"}}


def _cases_ref(cases: Path, root: Path) -> str:
    cases = cases.resolve()
    return cases.relative_to(root).as_posix() if root in cases.parents else str(cases)


def make(cases: Path, root: Path = ROOT) -> dict:
    root = root.resolve()
    if not cases.is_file():
        raise FreezeError("the case list does not exist")
    inputs = root / MEASUREMENT_INPUTS
    if not inputs.is_file() or sha_file(inputs) != MEASUREMENT_INPUTS_SHA256:
        raise FreezeError("the measurement inputs file is missing or is not the pinned one")
    d = json.loads(inputs.read_text(encoding="utf-8"))
    return {"freeze_version": FREEZE_VERSION,
            "measurement": {"batch_id": MEASUREMENT_BATCH, "inputs": MEASUREMENT_INPUTS,
                            "inputs_sha256": MEASUREMENT_INPUTS_SHA256,
                            "manifest_sha256": d["manifest_sha256"], "batch_sha256": d["batch_sha256"],
                            "records": len(d["generations"])},
            **identity(),
            "cases": {"path": _cases_ref(cases, root), "sha256": sha_file(cases)},
            "files": file_pins(root)}


def dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def write(path: Path, record: dict) -> str:
    if path.exists():
        raise FreezeError("freeze record already exists — never written over")
    path.parent.mkdir(parents=True, exist_ok=True)
    b = dump(record).encode("utf-8")
    path.write_bytes(b)
    return sha_bytes(b)


def check(path: Path, root: Path = ROOT) -> tuple[str, dict, list[str]]:
    """(sha256 of the record's bytes, the record, problems). Problems name files and fields only."""
    root = root.resolve()
    try:
        b = path.read_bytes()
        rec = json.loads(b)
    except (OSError, ValueError):
        raise FreezeError("freeze record not readable") from None
    probs: list[str] = []
    if not isinstance(rec, dict) or rec.get("freeze_version") != FREEZE_VERSION:
        return sha_bytes(b), rec if isinstance(rec, dict) else {}, ["freeze_version"]
    try:
        now = identity()
    except RP.RenderRefusal as exc:
        now = {"pipeline_version": None, "renderer": None}
        probs.append(f"tables:{exc.code}")
    for k in ("pipeline_version", "renderer"):
        if rec.get(k) != now[k]:
            probs.append(k)
    m = rec.get("measurement") or {}
    inputs = root / MEASUREMENT_INPUTS
    if (m.get("batch_id") != MEASUREMENT_BATCH or m.get("inputs_sha256") != MEASUREMENT_INPUTS_SHA256
            or not inputs.is_file() or sha_file(inputs) != MEASUREMENT_INPUTS_SHA256):
        probs.append("measurement")
    c = rec.get("cases") or {}
    cp = Path(c.get("path") or "")
    cp = cp if cp.is_absolute() else root / cp
    if not c.get("path") or not cp.is_file() or sha_file(cp) != c.get("sha256"):
        probs.append("cases")
    pinned, disk = rec.get("files") or {}, file_pins(root)
    probs += [f"file_changed:{k}" for k in sorted(pinned) if k in disk and disk[k] != pinned[k]]
    probs += [f"file_missing:{k}" for k in sorted(pinned) if k not in disk]
    probs += [f"file_not_pinned:{k}" for k in sorted(disk) if k not in pinned]
    return sha_bytes(b), rec, probs


def cases_path(rec: dict, root: Path = ROOT) -> Path:
    p = Path((rec.get("cases") or {}).get("path") or "")
    return p if p.is_absolute() else root.resolve() / p
