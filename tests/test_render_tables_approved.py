"""Item 3 R1 step 3 — the SHIPPED renderer tables (src/generate/pipeline/render_tables/), approved by Shina
(author/CSO) on 11 Nov 2026: D1–D12 of the step-3 report, Round 4 (docs/_local/r1/tables/).

Every line here is invented (tests/fixtures/render_tables_approved_cases.json — never the 90 saved records):
  forms  — every form of every glossary / drug-class / phrase entry on an invented frame: the entry must apply,
           the C line must be the reviewed Round 4 line, and audit() must be clean;
  paired — the paired invented controls: each ambiguity's intended and alternative reading side by side, the
           seek-help normal / joined / spaced forms, the instruction lines, the cross-drop signpost;
  hard   — the classified hard cases (substitution / drop / refusal / copy).
The seek-help property is proved for EVERY permitted match: each pattern must be a finite concatenation of
literal alternatives (parsed from the shipped table, then rebuilt and compared), and every member of that finite
language, in four letter cases and edge contexts, holds a match of the gate's _SIGNPOST inside each match.
"""

from __future__ import annotations

import itertools
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline import render_patient as R  # noqa: E402
from pipeline.safety_net_gate import _SIGNPOST  # noqa: E402

CASES = json.loads((ROOT / "tests" / "fixtures" / "render_tables_approved_cases.json").read_text(encoding="utf-8"))
PC = "presenting_complaint"
APPROVED_BY = "Shina (author/CSO) — D1–D12 adopted 11 Nov 2026, 07:45–07:52 Europe/London"


@pytest.fixture(scope="module")
def T():
    return R.load_tables()                     # the pins are enforced here: a changed byte refuses


def inp(heading, text, audience="adult"):
    secs = [] if heading == PC else [{"section_id": "sections.0", "heading": PC, "text": "Unwell at home."}]
    secs.append({"section_id": f"sections.{len(secs)}", "heading": heading, "text": text})
    return {"audience": audience, "sections": secs}


def last_line(out, heading):
    sec = next((s["text"] for s in out["sections"] if s["heading"] == R.C_OF[heading]), None)
    return sec.split("\n")[-1] if sec else None


def ops_of(out):
    return [o for x in out["ledger"]["lines"] for o in x["ops"]]


# --- the set itself -------------------------------------------------------------------------------------------

def test_shipped_tables_are_the_approved_pinned_valid_set(T):
    assert T.status == "approved" and T.sha256 == R.TABLE_SHA256
    raw = {t: json.loads((R.TABLES_DIR / f"{t}.json").read_text(encoding="utf-8")) for t in R.JSON_TABLES}
    words = (R.TABLES_DIR / R.WORDS_FILE).read_text(encoding="utf-8")
    assert R.validate_tables(raw, words) == []
    assert {(d["status"], d["approved_by"], d["approved_at"]) for d in raw.values()} == \
        {("approved", APPROVED_BY, "2026-11-11")}
    assert words.startswith("# status: approved\n")
    assert all(e["text"] == "" for e in raw["lead_ins"]["entries"])                  # D2 (a): all six empty
    assert raw["home_medicine"]["entries"] == []                                      # D4 (c): empty
    assert R.RENDERER_VERSION == 2


# --- every form, every control, every hard case -----------------------------------------------------------------

def test_every_form_applies_and_gives_the_reviewed_line(T):
    bad = []
    for f in CASES["forms"]:
        i = inp(f["heading"], f["text"], f["audience"])
        out = R.render_patient(i, T)
        applied = any(o["op"] == "sub" and o.get("entry") == f["id"] for o in ops_of(out))
        if not applied or last_line(out, f["heading"]) != f["out"] or R.audit(i, out, T):
            bad.append((f["table"], f["id"], f["form"], f["audience"]))
    assert len(CASES["forms"]) == 1105 and bad == []


def _paired_ok(row, T):
    i = inp(row["heading"], row["text"])
    kind, _, arg = row["expect"].partition(":")
    try:
        out = R.render_patient(i, T)
    except R.RenderRefusal as exc:
        return kind == "refuse" and exc.code == arg
    if kind == "refuse":
        return False
    ch = R.C_OF[row["heading"]]
    flags = {c["check"] for c in out["checks"] if c.get("heading") == ch and c["check"] == "signpost_outside_box"}
    line = last_line(out, row["heading"]) or ""
    ops = [o for o in out["ledger"]["lines"][-1]["ops"]]
    if kind == "sub":
        ok = any(o["op"] == "sub" and o.get("entry") == arg for o in ops)
    elif kind == "copy":
        ok = arg in line and not any(o["op"] == "sub" and arg.lower() in row["text"][o["a"]:o["b"]].lower()
                                     for o in ops)
    elif kind == "drop":
        ok = any(o["op"] == "drop" and (not arg or o.get("entry") == arg) for o in ops)
    else:
        return False
    return ok and not flags and R.audit(i, out, T) == []          # a signpost in the letter is never a quiet pass


def test_paired_invented_controls(T):
    bad = [(r["group"], r["text"], r["expect"]) for r in CASES["paired"] if not _paired_ok(r, T)]
    assert len(CASES["paired"]) == 131 and bad == []


def test_hard_cases_are_classified_as_reviewed(T):
    bad = []
    for c in CASES["hard"]:
        i = inp(c["heading"], c["text"], c["audience"])
        try:
            out = R.render_patient(i, T)
        except R.RenderRefusal:
            got = "refusal"
        else:
            ops = ops_of(out)
            got = ("drop" if any(o["op"] == "drop" for o in ops) else
                   "substitution" if any(o["op"] == "sub" for o in ops) else "copy")
            if R.audit(i, out, T):
                got = "audit_failed"
        if got != c["expect"]:
            bad.append((c["id"], got, c["expect"]))
    assert len(CASES["hard"]) == 55 and bad == []


# --- named behaviours ----------------------------------------------------------------------------------------

def test_the_emergency_department_treatment_line_still_refuses(T):
    with pytest.raises(R.RenderRefusal) as e:
        R.render_patient(inp("treatment", "- Wound repaired in the emergency department."), T)
    assert e.value.code == "signpost_not_isolable"


def test_the_inferred_phrase_is_kept_exactly_and_keeps_its_normal_flags(T):
    out = R.render_patient(inp("treatment", "- Antibiotics given (inferred — not documented, confirm)"), T)
    assert last_line(out, "treatment") == "- Antibiotics given " + R.ADOPTED_INFERRED
    assert ("advice_shaped", "what_we_did") in {(c["check"], c.get("heading")) for c in out["checks"]}


@pytest.mark.parametrize("text, code", [
    ("- Advised to rest, NHS 111 or 999 advice given.", "instruction_overlap"),    # instruction protection kept
    ("- NHS  111 advice given.", "signpost_not_isolable"),                          # spaced out, no entry
    ("Seen in the emergency (GRACE score 142) department.", "signpost_not_isolable"),   # D11, across a drop
])
def test_named_refusals(T, text, code):
    heading = PC if text.startswith("Seen") else "follow_up"
    with pytest.raises(R.RenderRefusal) as e:
        R.render_patient(inp(heading, text), T)
    assert e.value.code == code


def test_a_score_dropped_from_a_diagnosis_line_stays_permitted(T):
    out = R.render_patient(inp("diagnosis", "Primary: NSTEMI (GRACE score 142)"), T)
    assert last_line(out, "diagnosis") == "Main problem: heart attack (NSTEMI)"
    assert [d["entry"] for d in out["ledger"]["drops"]] == ["w_grace"]


# --- seek-help: every permitted match holds a signpost (proved on the finite language) -------------------------

def parse_literal_alternations(pattern: str) -> list[list[str]]:
    """The pattern as a sequence of (?:a|b|...) groups of escaped literals; anything else -> ValueError."""
    parts, i = [], 0
    while i < len(pattern):
        if not pattern.startswith("(?:", i):
            raise ValueError(f"not a group at {i}")
        j, buf, alts = i + 3, "", []
        while True:
            c = pattern[j]
            if c == "\\":
                buf += pattern[j + 1]
                j += 2
            elif c == "|":
                alts.append(buf)
                buf, j = "", j + 1
            elif c == ")":
                alts.append(buf)
                j += 1
                break
            elif c in "()[]{}*+?.^$":
                raise ValueError(f"metacharacter {c!r} at {j}")
            else:
                buf += c
                j += 1
        parts.append(alts)
        i = j
    return parts


def rebuild(parts):
    return "".join("(?:" + "|".join(re.escape(a) for a in sorted(alts, key=lambda a: (-len(a), a))) + ")"
                   for alts in parts)


NEGATIVE = ["111or999 advice given", "111and999 advice given", "NHS 111or999 advice given", "111 or999 advice given",
            "999and999 advice", "safety-netting re 999and999", "safety-netting re 999or111", "A&Eor999 advice given",
            "A&Eand999 advice given", "emergency  department advice given", "emergency\tdepartment advice given",
            "emergency\ndepartment advice", "NHS  111 advice given", "NHS\t111 advice given", "A  &  E advice given",
            "999  advice given", "999\tadvice given", "111/999  advice given", "safety-netting  re 999",
            "111 advice given", "safety-netting advice re 111", "111 safety-netting advice"]


def test_every_permitted_seek_help_match_holds_a_signpost(T):
    raw = json.loads((R.TABLES_DIR / "seek_help.json").read_text(encoding="utf-8"))["entries"]
    total_members = total_spans = 0
    for e in raw:
        parts = parse_literal_alternations(e["pattern"])
        assert rebuild(parts) == e["pattern"], e["id"]          # the pattern IS this finite concatenation
        rx = R._drop_regex(e["pattern"])
        for combo in itertools.product(*parts):
            m = "".join(combo)
            total_members += 1
            for v in dict.fromkeys((m, m.upper(), m.lower(), m.title())):
                assert rx.fullmatch(v), (e["id"], v)
                for left, right in itertools.product(["", " ", "- ", "/", "x"], ["", ".", " ", ")", "x"]):
                    line = left + v + right
                    for x in rx.finditer(line):
                        total_spans += 1
                        a, b = x.start(), x.end()
                        assert _SIGNPOST.search(line[a:b]) and _SIGNPOST.search(line, a, b), (e["id"], line)
    assert total_members == 12000 and total_spans == 940416
    for probe in NEGATIVE:
        assert not any(rx.search(probe) for en in T.seek_help for rx in en.regexes), probe


def test_the_language_check_catches_a_widened_pattern():
    widened = rebuild([["NHS 111", "111"], ["/", " or ", "or"], ["999"], [" advice"]])     # joined "or" planted
    rx = R._drop_regex(widened)
    assert rx.search("111or999 advice") and not _SIGNPOST.search("111or999 advice")
    with pytest.raises(ValueError):
        parse_literal_alternations(r"(?:A\s*&\s*E)(?:\ advice)")


# --- the fixture lines are invented -----------------------------------------------------------------------------

def test_fixture_lines_are_not_copied_from_the_scenario_files():
    scen = " ".join(" ".join(p.read_text(encoding="utf-8").split()).lower()
                    for p in sorted((ROOT / "evals" / "scenarios").glob("*.md")))
    lines = {r["text"] for k in ("forms", "paired", "hard") for r in CASES[k]}
    long_lines = [ln for ln in lines if len(ln.split()) >= 5]
    hits = [ln for ln in long_lines if " ".join(ln.lstrip("- ").split()).lower() in scen]
    assert len(long_lines) > 100 and hits == []
