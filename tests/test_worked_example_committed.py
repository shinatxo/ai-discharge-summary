"""The committed worked example checks MET under its own fixed binding (WS1a step build piece 8,
DoD 7; 9 Nov 2026). Skips only when evals/runs/ is absent altogether (the eval image does not
bake it); fails if evals/runs/ exists but the example does not."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
import check_worked_example as C  # noqa: E402

pytestmark = pytest.mark.skipif(not (ROOT / "evals" / "runs").is_dir(), reason="evals/runs not present (eval image)")


def test_the_committed_example_exists_and_checks_met():
    assert C.DEFAULT_DIR.is_dir(), f"missing: {C.DEFAULT_DIR.relative_to(ROOT)}"
    fails, info = C.check(C.DEFAULT_DIR)
    assert fails == [], fails
    assert info["trace_items"] == 13


def test_the_cli_says_met():
    assert C.main([]) == 0


def test_the_committed_trail_uses_the_frozen_rules():
    t = json.loads((C.DEFAULT_DIR / "trail.json").read_text())
    assert t["rules_pipeline_version"] == C.RULES_PIPELINE_VERSION == C.BINDING["pipeline_version"]


@pytest.mark.parametrize("name,edit,code", [
    ("release.json", lambda o: o.update(summary=o["summary"] + "."), "hash_audit"),
    ("audit.json", lambda o: o["output_sha256"].update(gp_letter="0" * 64), "hash_audit"),
    ("trace.json", lambda o: o.reverse(), "trace_bytes"),
    ("provenance.json", lambda o: o["binding"].update(job_id="01OTHER"), "binding"),
    ("release.json", lambda o: o.update(units_json=json.dumps({"user_sub": "x"})), "privacy_user_sub_key"),
])
def test_a_changed_committed_file_fails(tmp_path, name, edit, code):
    d = tmp_path / C.DEFAULT_DIR.name
    shutil.copytree(C.DEFAULT_DIR, d)
    o = json.loads((d / name).read_text())
    edit(o)
    (d / name).write_text(json.dumps(o, indent=1))
    assert code in C.check(d)[0]
