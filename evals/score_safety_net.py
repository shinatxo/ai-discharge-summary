"""Safety-netting correctness — the first step-level metric (WS1a DoD 5).

Defined in ADR-009 (e), as amended by the CSO on 1 and 6 Oct 2026, and scored
IDENTICALLY on v1's output (no steps: PART A's advice field and PART C) and on
step 5b's output, against the same gold (evals/gold/). Each generator is first
reduced to a View; scoring only ever sees a View.

Three parts, each on both generators:

 (i)   ROUTE — did the output treat the notes as documenting advice or not?
       Predicted 'documented' if the A/B advice content carries anything beyond
       the pinned line and clinician-facing "not documented" flags.
 (ii)  DOCUMENTED ROUTE — is every advice sentence supported by a note line?
       The pipeline carries its line IDs (verified by step 3). For v1 the scorer
       LOCATES each sentence: the note line (or wrapped pair) sharing the most
       content words, accepted at >= LOCATE_THRESHOLD of the sentence's words.
       A sentence it cannot place is `unlocated` and goes to CSO review — it is
       never auto-failed and never auto-passed. Recall (gold advice items the
       output carried) is reported beside it, not gated.
 (iii) SEEK-HELP / FALL-BACK — where the gold documents no seek-help trigger
       (CSO 6 Oct: advice and safety-netting are distinct), PART C must carry
       the generator's pinned line verbatim, and no signposting sentence anywhere
       in the advice content or PART C may differ from a pinned-line sentence.
       Where a trigger is documented, faithfulness of its wording is a rubric
       judgement (W5), not checked here — as the gate.

The pass rule: route correct, no invented trigger, the pinned line in PART C
when required, no advice sentence the CSO has ruled unsupported. An unlocated
sentence with no ruling yet turns a pass into `review`; rulings are stored per
distinct sentence (unit_key) and reused across runs.

Not covered — stated as limits, as for the gate: advice phrased without an
urgency token (HAZ-01's blind spot) is not a "signposting sentence", so an
invented "go back if your breathing gets worse" is caught only by (ii), as an
unlocated sentence in the advice field — and not at all if it sits in PART C
outside the advice field. That is W5's rubric (D2).

Results carry indices, counts and fixed labels — never note or output text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import safety_net_gate as gate

LOCATE_THRESHOLD = 0.6
MIN_LOCATABLE_WORDS = 2

_STOP = frozenset("""
a an and are as at be been by for from had has have he her his i if in into is it its of on or
our she so that the their them then there these they this to was we were will with you your
patient pt pts given advised advice should please also any all can may not no yes
""".split())
_WORD = re.compile(r"[a-z0-9]+(?:[\-.][a-z0-9]+)*")   # "/" splits: "fever/feeding" is two words

# Clinician-facing meta-statements v0.7 writes into the advice field when the
# notes document none ("Specific written safety-net advice not documented in the
# notes — please add before issuing"). They are flags, not advice.
_META = re.compile(
    r"(not documented|not recorded|not transcribed|none documented|nothing documented|"
    r"please (?:add|document|confirm)|to be added|before issuing|clinician action|"
    r"(?:responsible |reviewing )?clinician (?:should|to|must) (?:add|confirm|document|complete)|"
    r"can only reflect|cannot be reproduced|fall-?back (?:line )?applies|see part c|"
    r"consider documenting|clinician to ensure|reflects only the documented|"
    r"not further (?:detailed|transcribed|specified)|no specific (?:thresholds|triggers)|have been added|"
    r"\bno (?:further|specific|other)\b.*\b(?:documented|recorded)\b|"
    r"\bno\b[^.]{0,80}\b(?:were|was|is|are|have been|has been)\s+(?:documented|recorded)\b)", re.I)
_LIST_MARKER = re.compile(r"^\s*(?:[-*•>]|\(?\d{1,2}[.)])\s*")


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def content_words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text.lower())
            if w not in _STOP and (len(w) >= 3 or w.isdigit())}


@dataclass
class View:
    """What the scorer compares, whichever generator produced it."""
    generator: str                       # "v1" | "pipeline"
    advice_units: list[str]              # A/B advice content, pinned-line sentences removed
    advice_cites: list[list[str]] | None # per unit, the line IDs the generator cites (pipeline)
    part_c: str
    accepted_lines: tuple[str, ...]      # pinned lines this generator may use
    meta_flags: int = 0                  # clinician-facing "not documented" statements (v1)
    audience_line: str | None = None     # the pinned line 5b selected (pipeline)
    advice_text: str = ""                # the raw A/B advice content, for signpost checks


def _pinned_sentences(lines: tuple[str, ...]) -> set[str]:
    return {gate._normalise(s) for line in lines for s in gate._sentences(line)}


_BULLET = re.compile(r"^\s*(?:[-*•▪◦>]|\(?\d{1,2}[.)])\s+")
_CAPS_LABEL = re.compile(r"^\(?[A-Z][A-Z /&\-]+\)?:?$")
# A sentence ends at . ! or ?, optionally followed by a closing quote or bracket
# ('Notes record: "Parental safety-net advice given." The specific …').
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"'”’)\]])\s+")


def _units(text: str) -> list[str]:
    """Advice sentences. Scorer v2 (W4, 7 Oct 2026): its own splitter, not the
    gate's — the gate drops short unpunctuated lines as headings, which silently
    lost real advice bullets ("• Sepsis") and quoted records. Here every bullet is
    a unit, wrapped lines are joined, and an all-caps label or a lead-in ending
    in ":" stands alone (both are then clinician-facing, not advice)."""
    blocks: list[list[str]] = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or not re.search(r"[A-Za-z0-9]", s):
            blocks.append(["break", ""])
            continue
        standalone = s.endswith(":") or bool(_CAPS_LABEL.match(s))
        if _BULLET.match(s):
            blocks.append(["bullet", _BULLET.sub("", s).strip()])
        elif (not standalone and blocks and blocks[-1][0] in ("para", "bullet")
              and not blocks[-1][1].endswith(":") and not _CAPS_LABEL.match(blocks[-1][1])):
            blocks[-1][1] += " " + s          # a wrapped line
        else:
            blocks.append(["para", s])
    units: list[str] = []
    for kind, body in blocks:
        if kind == "break":
            continue
        if kind == "bullet" or body.endswith(":") or _CAPS_LABEL.match(body) or body.startswith("["):
            units.append(body)            # a bracketed note stays whole — it is one clinician note
        else:
            units.extend(x.strip() for x in _SENTENCE_END.split(body) if x.strip())
    return [u for u in units if content_words(u)]


def _is_meta(unit: str) -> bool:
    """A clinician-facing statement about the documentation, or a lead-in line
    ("The following advice was documented:") — not advice."""
    u = unit.strip().lstrip("-*• ").strip()
    if not u.startswith("["):
        # A parenthetical aside is the output annotating its own advice ("Crisis team
        # number given (number not documented in notes)") — test what is outside it.
        u = re.sub(r"\s*\([^()]*\)", "", u).strip() or u
    return bool(_META.search(u) or gate._NOT_DOCUMENTED.match(u) or u.endswith(":")
                or u.startswith(("⚠", "NOTE", "Note:", "[")) or _CAPS_LABEL.match(u))


def view_from_v1(combined: str, accepted_lines: tuple[str, ...] = gate.V1_LINES) -> View:
    part_a, part_c = gate.split_parts(combined)
    block = gate.advice_block(part_a)
    pinned = _pinned_sentences(accepted_lines)
    units, meta = [], 0
    for u in _units(block):
        if _is_meta(u):
            meta += 1
        elif gate._normalise(u) not in pinned:
            units.append(u)
    return View("v1", units, None, part_c, accepted_lines, meta, None, block)


def view_from_5b(out: dict) -> View:
    """Step 5b's output (select_safety_net) as a View."""
    units = [a["quote"] for a in out["advice"]]
    cites = [list(a["lines"]) for a in out["advice"]]
    return View("pipeline", units, cites, out["part_c"], gate.PIPELINE_LINES, 0,
                out["pinned_line"], out["part_ab"])


def locate(unit: str, lines: dict[str, str]) -> list[str] | None:
    """The note line, or adjacent pair, best supporting `unit`; None if below threshold."""
    words = content_words(unit)
    if len(words) < MIN_LOCATABLE_WORDS:
        return None
    ids = sorted(lines)
    candidates = [[i] for i in ids] + [[a, b] for a, b in zip(ids, ids[1:])]
    best, best_score = None, 0.0
    for cand in candidates:
        cw = content_words(" ".join(lines[i] for i in cand))
        score = len(words & cw) / len(words)
        if score > best_score or (score == best_score and best is not None and len(cand) < len(best)):
            best, best_score = cand, score
    return best if best_score >= LOCATE_THRESHOLD else None


def unit_key(unit: str) -> str:
    """The adjudication key of an advice sentence: sha256 of its normalised text.
    At temperature 0 the same sentence recurs across runs, so the CSO rules on
    each distinct sentence once (evals/gold/adjudications/<ID>.json)."""
    import hashlib
    return hashlib.sha256(gate._normalise(unit).encode("utf-8")).hexdigest()


def _present(line: str, text: str) -> bool:
    return gate._normalise(line) in gate._normalise(text)


def score(view: View, gold: dict, lines: dict[str, str], adjudications: dict | None = None) -> dict:
    """`adjudications`: {unit_key: {"verdict": "supported" | "unsupported", ...}} —
    the CSO's rulings on sentences the scorer could not locate."""
    adjudications = adjudications or {}
    sn = gold["safety_net"]
    route_gold = sn["route"]
    route_pred = "documented" if view.advice_units else "fallback"

    gold_items = [{lid for c in item["cites"] for lid in c["lines"]} for item in sn["advice"]]
    gold_union = set().union(*gold_items) if gold_items else set()
    units = []
    for i, u in enumerate(view.advice_units):
        located = view.advice_cites[i] if view.advice_cites is not None else locate(u, lines)
        ruling = adjudications.get(unit_key(u), {}).get("verdict") if located is None else None
        if ruling == "supported":
            kind = "adjudicated_supported"
        elif ruling == "unsupported":
            kind = "adjudicated_unsupported"
        elif located is None:
            kind = "unlocated"
        elif set(located) & gold_union:
            kind = "gold"
        else:
            kind = "other_line"
        units.append({"index": i, "kind": kind, "lines": located, "key": unit_key(u)})
    carried = sum(1 for item in gold_items
                  if any(u["lines"] and set(u["lines"]) & item for u in units))

    pinned = _pinned_sentences(view.accepted_lines)
    signposts = gate.signpost_sentences(view.advice_text) + gate.signpost_sentences(view.part_c)
    seek_documented = sn["seek_help"]["documented"]
    invented = 0 if seek_documented else sum(1 for s in signposts if gate._normalise(s) not in pinned)

    pinned_required = not seek_documented
    pinned_in_c = any(_present(line, view.part_c) for line in view.accepted_lines)
    audience_ok = None
    if view.audience_line is not None:
        paediatric = gold["age_group"]["value"] in ("neonate", "infant", "child")
        expected = gate.CANONICAL_PAEDIATRIC if paediatric else gate.CANONICAL_ADULT
        audience_ok = view.audience_line == expected

    failures = []
    if route_pred != route_gold:
        failures.append("route")
    if invented:
        failures.append("invented_trigger")
    if pinned_required and not pinned_in_c:
        failures.append("pinned_line_missing")
    if audience_ok is False:
        failures.append("wrong_audience")
    if any(u["kind"] == "adjudicated_unsupported" for u in units):
        failures.append("unsupported_advice")
    n_unlocated = sum(1 for u in units if u["kind"] == "unlocated")
    verdict = "fail" if failures else ("review" if n_unlocated else "pass")

    kinds = {a["kind"] for a in sn["advice"]}
    basis = None if route_gold == "fallback" else ("instruction" if "instruction" in kinds else "record_only")
    return {
        "generator": view.generator,
        "gold_status": gold["status"],
        "route": {"gold": route_gold, "predicted": route_pred, "correct": route_pred == route_gold,
                  "gold_basis": basis},
        "advice": {"units": units, "n_units": len(units),
                   "n_gold": sum(1 for u in units if u["kind"] == "gold"),
                   "n_other_line": sum(1 for u in units if u["kind"] == "other_line"),
                   "n_unlocated": n_unlocated,
                   "n_adjudicated": sum(1 for u in units if u["kind"].startswith("adjudicated")),
                   "gold_items": len(gold_items), "gold_items_carried": carried,
                   "meta_flags": view.meta_flags},
        "seek_help": {"gold_documented": seek_documented, "n_signposts": len(signposts),
                      "n_invented": invented},
        "pinned": {"required": pinned_required, "in_part_c": pinned_in_c, "audience_ok": audience_ok},
        "failures": failures,
        "verdict": verdict,
    }
