"""infra/template.yaml — TraceTable (WS1a checklist item 6, 6 Nov 2026). A test per clause of
the done-means line: in the template; CMK; 30-day TTL; worker PutItem only — plus deletion
protection, no stream / no PITR, and nobody else holding any grant on it. Text-block
parsing (no YAML parser in the test environment, as the other template tests)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "infra" / "template.yaml"
sys.path.insert(0, str(ROOT / "src" / "generate"))

from pipeline import orchestrate as O  # noqa: E402


def resources(text: str) -> dict[str, str]:
    """Top-level resource blocks (two-space indent) of the Resources section, by logical id."""
    body = text.split("\nResources:\n", 1)[1].split("\nOutputs:\n", 1)[0]
    parts = re.split(r"\n  ([A-Za-z0-9]+):\n", "\n" + body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def code_only(block: str) -> str:
    """The block without comment lines (comments may name TraceTable)."""
    return "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))


def trace_table(text: str) -> str:
    return code_only(resources(text)["TraceTable"])


def test_trace_table_is_in_the_template_with_its_keys_and_name():
    t = trace_table(TEMPLATE.read_text())
    assert "Type: AWS::DynamoDB::Table" in t
    assert 'TableName: !Sub "${AWS::StackName}-traces"' in t
    assert "BillingMode: PAY_PER_REQUEST" in t
    assert re.search(r"- AttributeName: PK\n\s+KeyType: HASH", t)
    assert re.search(r"- AttributeName: SK\n\s+KeyType: RANGE", t)
    assert re.search(r"- AttributeName: PK\n\s+AttributeType: S", t)
    assert re.search(r"- AttributeName: SK\n\s+AttributeType: S", t)


def test_encrypted_with_the_stacks_cmk():
    t = trace_table(TEMPLATE.read_text())
    assert re.search(r"SSESpecification:\n\s+SSEEnabled: true\n\s+SSEType: KMS\n\s+KMSMasterKeyId: !Ref AuditKey", t)
    assert "ExistingCmkArn" not in code_only(TEMPLATE.read_text())  # 28 Sep decision: no parameter


def test_ttl_on_ttl_and_the_period_is_30_days():
    t = trace_table(TEMPLATE.read_text())
    assert re.search(r"TimeToLiveSpecification:\n\s+AttributeName: ttl\n\s+Enabled: true", t)
    assert O.TRACE_TTL_S == 30 * 86400 and O.trace_ttl(1000.0) == 1000 + 30 * 86400


def test_deletion_protection_on_and_no_stream_no_pitr():
    t = trace_table(TEMPLATE.read_text())
    assert "DeletionProtectionEnabled: true" in t
    assert "StreamSpecification" not in t and "PointInTimeRecovery" not in t
    assert "DeletionPolicy" not in t                     # deleted with the stack once protection is off


def _grants(text: str) -> dict[str, list[str]]:
    """Every resource whose (non-comment) body references the trace table."""
    return {lid: [l.strip() for l in code_only(b).splitlines() if "TraceTable" in l]
            for lid, b in resources(text).items() if lid != "TraceTable" and "TraceTable" in code_only(b)}


def test_only_the_worker_role_and_function_reference_the_table():
    refs = _grants(TEMPLATE.read_text())
    assert set(refs) == {"GenerateFunctionRole", "GenerateFunction"}
    assert refs["GenerateFunction"] == ["TRACE_TABLE_NAME: !Ref TraceTable"]
    assert refs["GenerateFunctionRole"] == ["Resource: !GetAtt TraceTable.Arn"]
    text = TEMPLATE.read_text()
    assert "-traces" not in code_only(text).replace('TableName: !Sub "${AWS::StackName}-traces"', "")


def test_worker_gets_putitem_only_with_returnvalues_none():
    role = code_only(resources(TEMPLATE.read_text())["GenerateFunctionRole"])
    pol = role.split("- PolicyName: trace-table-put-only", 1)[1].split("- PolicyName:", 1)[0]
    actions = re.findall(r"\n\s+- (dynamodb:\w+)", pol)
    assert actions == ["dynamodb:PutItem"]
    assert "Resource: !GetAtt TraceTable.Arn" in pol and "Effect: Allow" in pol
    assert re.search(r"StringEqualsIfExists:\n\s+dynamodb:ReturnValues: NONE", pol)
    assert "Deny" not in pol and "*" not in pol


def test_no_role_holds_wildcard_dynamodb_data_access():
    text = code_only(TEMPLATE.read_text())
    assert not re.search(r"dynamodb:\*", text)
    assert ":table/" not in text                                    # no hand-built (wildcard) table ARNs
    for lid, block in resources(text).items():
        if "AWS::IAM::Role" not in block:
            continue
        for stmt in re.split(r"\n\s+- Sid: ", block)[1:]:
            if 'Resource: "*"' in stmt or "Resource: '*'" in stmt:
                acts = re.findall(r"(dynamodb:\w+)", stmt)
                assert set(acts) <= {"dynamodb:ListStreams"}, (lid, acts)


def test_the_inherited_returnvalues_comment_is_corrected():
    text = TEMPLATE.read_text()
    assert "ReturnValuesOnConditionCheckFailure has no IAM" not in text
    assert "condition key; no code sets it" not in text


def test_runbook_teardown_switches_off_protection_on_all_three_tables():
    rb = (ROOT / "infra" / "EPHEMERAL_STACK_RUNBOOK.md").read_text()
    assert "for t in audit results traces; do aws dynamodb update-table" in rb
