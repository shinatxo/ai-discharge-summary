"""The record_facts schema stays inside Bedrock's structured-output limits and
DPIA Annex C.2 (field names and fixed enums only).

`limit_violations` below is written from the rules, not from the schema
helpers, so it would catch a hand edit to schemas.py. The mutation tests at the
bottom prove it: each injects one forbidden construct and asserts the checker
reports it — a checker that never fails is worth nothing.

No jsonschema dependency: CI installs only pytest, boto3 and cfn-lint.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline.schemas import FIELD_NAMES, record_facts_schema, shape_errors  # noqa: E402

ALLOWED_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum"}
ALLOWED_TYPES = {"object", "array", "string", "boolean"}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def limit_violations(schema) -> list[str]:
    """Every way `schema` breaks the rules, as '<path>: <problem>'."""
    out: list[str] = []

    def walk(node, path):
        if not isinstance(node, dict):
            out.append(f"{path}: schema node is not an object")
            return
        for kw in node:
            if kw not in ALLOWED_KEYWORDS:
                # Catches description/title/examples/default (Annex C.2),
                # minLength/maxLength/minimum/maximum/pattern/minItems (Bedrock),
                # $ref/$defs (recursion), anyOf/oneOf/allOf/const (unions).
                out.append(f"{path}: keyword {kw!r} not allowed")
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in ALLOWED_TYPES:
            # A list here, e.g. ["string", "null"], is a union type.
            out.append(f"{path}: type must be one of {sorted(ALLOWED_TYPES)}, got {kind!r}")
            return
        if kind == "object":
            props = node.get("properties")
            if not isinstance(props, dict) or not props:
                out.append(f"{path}: object without properties")
                props = {}
            if node.get("additionalProperties") is not False:
                out.append(f"{path}: additionalProperties must be false")
            required = node.get("required")
            if not isinstance(required, list) or sorted(required) != sorted(props) \
                    or len(set(required)) != len(required):
                out.append(f"{path}: every property must be required, exactly once")
            for name, sub in props.items():
                if not IDENTIFIER.match(name):
                    out.append(f"{path}: property name {name!r} is not a field identifier")
                walk(sub, f"{path}.{name}")
        elif "properties" in node or "required" in node or "additionalProperties" in node:
            out.append(f"{path}: object keywords on a {kind}")
        if kind == "array":
            if "items" not in node:
                out.append(f"{path}: array without items")
            else:
                walk(node["items"], f"{path}[]")
        elif "items" in node:
            out.append(f"{path}: items on a {kind}")
        if "enum" in node:
            vals = node["enum"]
            if kind != "string":
                out.append(f"{path}: enum only on strings")
            elif not isinstance(vals, list) or not vals or len(set(vals)) != len(vals):
                out.append(f"{path}: enum must be a non-empty list of unique values")
            else:
                for v in vals:
                    if not isinstance(v, str) or not IDENTIFIER.match(v):
                        out.append(f"{path}: enum value {v!r} is not a fixed identifier")

    walk(schema, "$")
    return out


# --- the real schema ----------------------------------------------------------

def test_schema_is_inside_the_limits():
    assert limit_violations(record_facts_schema()) == []


def test_every_string_in_the_schema_is_an_identifier():
    """Annex C.2 in one assertion: no sentence, example or note fragment can
    hide anywhere in the schema, because every string in it is a snake_case
    identifier (or false/true, which are not strings)."""
    strings = []

    def collect(x):
        if isinstance(x, dict):
            for k, v in x.items():
                strings.append(k)
                collect(v)
        elif isinstance(x, list):
            for v in x:
                collect(v)
        elif isinstance(x, str):
            strings.append(x)

    collect(record_facts_schema())
    bad = [s for s in strings if not IDENTIFIER.match(s) and s != "additionalProperties"]
    assert bad == []


def test_top_level_shape_matches_adr_009():
    s = record_facts_schema()
    assert list(s["properties"]) == [
        "fields", "medications", "resus", "documented_advice",
        "age_group", "contradictions", "suspicious_text"]
    assert list(s["properties"]["fields"]["properties"]) == list(FIELD_NAMES)
    assert s["properties"]["resus"]["properties"]["status_documented"]["enum"][-1] == "not_documented"
    assert "child" in s["properties"]["age_group"]["enum"]


def test_no_patient_identifier_fields():
    dumped = json.dumps(record_facts_schema())
    for word in ("nhs", "dob", "name", "hospital_number", "address", "postcode"):
        assert word not in dumped


def test_schema_is_json_and_its_fingerprint_is_stable():
    # pipeline_version will hash this; the hash must not wobble between calls.
    a = json.dumps(record_facts_schema(), sort_keys=True)
    b = json.dumps(record_facts_schema(), sort_keys=True)
    assert hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(b.encode()).hexdigest()


def test_callers_get_a_fresh_copy():
    s = record_facts_schema()
    s["properties"]["age_group"]["enum"].append("teenager")
    assert "teenager" not in record_facts_schema()["properties"]["age_group"]["enum"]


# --- the checker fails when it should (mutation tests) ------------------------

def _mutate(fn):
    s = record_facts_schema()
    fn(s)
    return limit_violations(s)


@pytest.mark.parametrize("label, mutation", [
    ("description (Annex C.2)",
     lambda s: s["properties"]["resus"].__setitem__("description", "e.g. DNACPR signed 10/05")),
    ("examples (Annex C.2)",
     lambda s: s["properties"]["age_group"].__setitem__("examples", ["adult"])),
    ("additionalProperties true",
     lambda s: s["properties"]["resus"].__setitem__("additionalProperties", True)),
    ("additionalProperties missing",
     lambda s: s["properties"]["medications"].pop("additionalProperties")),
    ("optional property",
     lambda s: s["properties"]["resus"]["required"].remove("changed")),
    ("minLength",
     lambda s: s["properties"]["fields"]["properties"]["allergies"]["properties"]["items"]["items"]
               ["properties"]["value"].__setitem__("minLength", 1)),
    ("maxItems",
     lambda s: s["properties"]["documented_advice"].__setitem__("maxItems", 10)),
    ("nullable union type",
     lambda s: s["properties"]["age_group"].__setitem__("type", ["string", "null"])),
    ("anyOf",
     lambda s: s["properties"].__setitem__("age_group", {"anyOf": [{"type": "string"}]})),
    ("$ref (recursion)",
     lambda s: s["properties"]["contradictions"].__setitem__("items", {"$ref": "#"})),
    ("prose enum value",
     lambda s: s["properties"]["age_group"]["enum"].append("Aged 76, frail")),
    ("prose property name",
     lambda s: s["properties"]["fields"]["properties"].__setitem__("Day 3 DNACPR", {"type": "string"})),
    ("integer type",
     lambda s: s["properties"]["age_group"].__setitem__("type", "integer")),
])
def test_checker_catches(label, mutation):
    assert _mutate(mutation) != [], label


# --- shape_errors (used by validate_facts on fixture inputs) ------------------

def _empty_field():
    return {"status": "not_documented", "items": []}


def minimal_facts():
    return {
        "fields": {name: _empty_field() for name in FIELD_NAMES},
        "medications": {"pre_admission": [], "discharge": [], "discharge_status": "not_documented"},
        "resus": {"form_or_discussion_documented": False, "status_documented": "not_documented",
                  "changed": "not_documented", "cites": []},
        "documented_advice": [],
        "age_group": "not_documented",
        "contradictions": [],
        "suspicious_text": [],
    }


def test_minimal_facts_object_conforms():
    assert shape_errors(minimal_facts()) == []


def test_shape_errors_report_paths_never_values():
    f = minimal_facts()
    f["age_group"] = "76-year-old man, DNACPR"          # not in enum
    f["fields"]["allergies"]["items"] = [{"value": 5, "cites": []}]   # wrong type
    del f["resus"]["changed"]                            # missing
    f["resus"]["Patient Smith"] = True                   # unexpected key
    errors = shape_errors(f)
    assert "$.age_group: not in enum" in errors
    assert "$.fields.allergies.items[0].value: expected string" in errors
    assert "$.resus.changed: missing" in errors
    assert "$.resus: unexpected key" in errors
    joined = " ".join(errors)
    assert "76-year-old" not in joined and "Smith" not in joined
