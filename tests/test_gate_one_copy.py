"""One copy of the safety-net gate (ADR-009 [A3]; WS1a step build piece 4, CSO E1 (a),
5 Nov 2026).

Ownership:
  - the two pinned lines are DEFINED in src/generate/pipeline/safety_net.py (step 5b);
  - the urgency pattern `_SIGNPOST` and the gate logic (check, advice_block,
    signpost_sentences) are DEFINED in src/generate/pipeline/safety_net_gate.py;
  - everyone else imports them.
The scan is scoped to those canonical symbols and to the gate's own `check(notes, ...)`
signature: imports, test assertions and unrelated functions named `check` are allowed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "evals"))

from pipeline import compose_patient, safety_net, safety_net_gate  # noqa: E402

GATE = ROOT / "src" / "generate" / "pipeline" / "safety_net_gate.py"
LINES_OWNER = ROOT / "src" / "generate" / "pipeline" / "safety_net.py"
SOURCES = [p for d in ("src", "evals", "tests") for p in (ROOT / d).rglob("*.py")
           if "__pycache__" not in p.parts and p.resolve() != Path(__file__).resolve()]   # not this file

PINNED_TEXT = ("use that first. Otherwise, if you are",)        # a fragment of both pinned lines
GATE_DEFS = (re.compile(r"^_SIGNPOST\s*=", re.M),
             re.compile(r"^def check\(\s*notes\b", re.M),
             re.compile(r"^def advice_block\(", re.M),
             re.compile(r"^def signpost_sentences\(", re.M))


def test_the_old_eval_copy_is_gone():
    assert not (ROOT / "evals" / "safety_net_gate.py").exists(), \
        "evals/safety_net_gate.py must be removed (git rm) — the gate lives in the package"


def test_pinned_lines_are_written_only_in_step_5b():
    owners = [p for p in SOURCES if any(t in p.read_text(encoding="utf-8") for t in PINNED_TEXT)]
    assert owners == [LINES_OWNER]


def test_gate_logic_and_signpost_are_defined_only_in_the_package_gate():
    for pat in GATE_DEFS:
        owners = [p for p in SOURCES if pat.search(p.read_text(encoding="utf-8"))]
        assert owners == [GATE], (pat.pattern, owners)


def test_users_share_the_one_object():
    import haz03_check
    import run_cold_eval
    import score_patient
    import score_safety_net
    assert safety_net_gate.CANONICAL_ADULT is safety_net.ADULT_LINE
    assert safety_net_gate.CANONICAL_PAEDIATRIC is safety_net.PAEDIATRIC_LINE
    assert compose_patient._SIGNPOST is safety_net_gate._SIGNPOST
    for mod in (haz03_check.gate, score_patient.gate, score_safety_net.gate, run_cold_eval.safety_net_gate):
        assert mod is safety_net_gate
