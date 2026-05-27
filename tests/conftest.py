"""Shared fixtures + minimal in-memory mocks for the Lambda unit tests.

Why hand-rolled mocks rather than `moto`: the Lambdas use `boto3.client('dynamodb')`
*and* one uses `boto3.client('lambda')`; for the small surface we exercise (PutItem,
GetItem, UpdateItem, TransactWriteItems, lambda.invoke) a tiny stub is simpler and
keeps the test suite hermetic / fast / no extra dependency. The mocks live in
this conftest so all test modules share them via fixtures.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

# Make every src/* package importable as a top-level module (so tests can do
# `import app` for whichever Lambda is under test, by manipulating sys.path).
ROOT = Path(__file__).resolve().parent.parent
SRC_DIRS = {
    "dispatcher": ROOT / "src" / "dispatcher",
    "status":     ROOT / "src" / "status",
    "generate":   ROOT / "src" / "generate",
}


# ---------------------------------------------------------------------------
# Tiny in-memory DynamoDB stub - just enough for our tests
# ---------------------------------------------------------------------------
class FakeDynamoDB:
    """In-memory DynamoDB stub. Items keyed by (TableName, PK, SK)."""

    def __init__(self):
        self.items: dict[tuple[str, str, str], dict] = {}
        # Spy hooks - test asserts inspect these
        self.put_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.transact_calls: list[list] = []

    @staticmethod
    def _key_of(item_or_key):
        pk = item_or_key["PK"]["S"]
        sk = item_or_key["SK"]["S"]
        return pk, sk

    # --- public surface mirroring boto3 client methods ----------------------
    def put_item(self, *, TableName, Item, ConditionExpression=None,
                  ExpressionAttributeValues=None, ExpressionAttributeNames=None):
        self.put_calls.append({"TableName": TableName, "Item": Item,
                                "ConditionExpression": ConditionExpression})
        pk, sk = self._key_of(Item)
        key = (TableName, pk, sk)
        if ConditionExpression == "attribute_not_exists(PK) AND attribute_not_exists(SK)":
            if key in self.items:
                self._raise("ConditionalCheckFailedException", "exists")
        self.items[key] = dict(Item)
        return {}

    def get_item(self, *, TableName, Key, ConsistentRead=False):
        self.get_calls.append({"TableName": TableName, "Key": Key,
                                "ConsistentRead": ConsistentRead})
        pk, sk = self._key_of(Key)
        item = self.items.get((TableName, pk, sk))
        return {"Item": dict(item)} if item else {}

    def update_item(self, *, TableName, Key, UpdateExpression,
                     ExpressionAttributeValues=None,
                     ExpressionAttributeNames=None,
                     ConditionExpression=None):
        self.update_calls.append({
            "TableName": TableName, "Key": Key,
            "UpdateExpression": UpdateExpression,
            "ExpressionAttributeValues": ExpressionAttributeValues,
            "ConditionExpression": ConditionExpression,
        })
        pk, sk = self._key_of(Key)
        key = (TableName, pk, sk)
        if key not in self.items:
            self._raise("ResourceNotFoundException", "no item")
        if ConditionExpression:
            # Tiny matcher for "#st = :pending" - enough for our worker tests.
            if "#st = :pending" in ConditionExpression:
                current_status = self.items[key].get("status", {}).get("S")
                expected = (ExpressionAttributeValues or {}).get(":pending", {}).get("S")
                if current_status != expected:
                    self._raise("ConditionalCheckFailedException", "wrong status")
        # Apply a very small subset of UpdateExpression - "SET attr = :val, ..."
        # parsed naively. Good enough for our use cases.
        self._apply_set(self.items[key], UpdateExpression,
                        ExpressionAttributeValues or {},
                        ExpressionAttributeNames or {})
        return {"Attributes": dict(self.items[key])}

    def transact_write_items(self, *, TransactItems):
        # Simulate atomicity: validate each ConditionExpression on a snapshot
        # before applying any item.
        self.transact_calls.append(TransactItems)
        cancellation = []
        for op in TransactItems:
            if "Put" not in op:
                continue
            put = op["Put"]
            cond = put.get("ConditionExpression")
            pk, sk = self._key_of(put["Item"])
            key = (put["TableName"], pk, sk)
            if cond == "attribute_not_exists(PK) AND attribute_not_exists(SK)" and key in self.items:
                cancellation.append("ConditionalCheckFailed")
            else:
                cancellation.append("None")
        if any(reason == "ConditionalCheckFailed" for reason in cancellation):
            err = ClientError(
                error_response={
                    "Error": {"Code": "TransactionCanceledException",
                              "Message": "ConditionalCheckFailed"},
                    "CancellationReasons": [{"Code": r} for r in cancellation],
                },
                operation_name="TransactWriteItems",
            )
            raise err
        for op in TransactItems:
            if "Put" not in op:
                continue
            put = op["Put"]
            pk, sk = self._key_of(put["Item"])
            self.items[(put["TableName"], pk, sk)] = dict(put["Item"])
        return {}

    # --- helpers ------------------------------------------------------------
    def _apply_set(self, item, expr, values, names):
        """Apply a comma-separated SET ... clause. Naive but sufficient."""
        # Strip leading "SET "
        body = expr.strip()
        if body.upper().startswith("SET "):
            body = body[4:]
        # Split on commas that aren't inside a quoted string (we don't quote).
        for clause in body.split(","):
            lhs, _, rhs = clause.strip().partition("=")
            attr = lhs.strip()
            placeholder = rhs.strip()
            # Resolve attribute name aliases (#st -> "status").
            if attr.startswith("#"):
                attr = names.get(attr, attr)
            value = values.get(placeholder)
            if value is None:
                continue
            item[attr] = value

    @staticmethod
    def _raise(code: str, msg: str):
        raise ClientError(
            error_response={"Error": {"Code": code, "Message": msg}},
            operation_name="DynamoDB",
        )


# ---------------------------------------------------------------------------
# Fake Lambda client - records invocations
# ---------------------------------------------------------------------------
class FakeLambda:
    def __init__(self):
        self.invocations: list[dict] = []
        # If set, invoke() will raise this ClientError. Test sets it for the
        # invoke-failure path.
        self.next_error: ClientError | None = None

    def invoke(self, *, FunctionName, InvocationType, Payload):
        if self.next_error is not None:
            err, self.next_error = self.next_error, None
            raise err
        self.invocations.append({
            "FunctionName": FunctionName,
            "InvocationType": InvocationType,
            "Payload": json.loads(Payload.decode("utf-8") if isinstance(Payload, bytes) else Payload),
        })
        return {"StatusCode": 202}


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_ddb():
    return FakeDynamoDB()


@pytest.fixture
def fake_lambda():
    return FakeLambda()


def _load_lambda_module(src_dir: Path, env: dict, monkeypatch,
                         fake_ddb: FakeDynamoDB | None = None,
                         fake_lambda: FakeLambda | None = None,
                         fake_bedrock: Any | None = None,
                         module_name: str = "app"):
    """Import (or reload) a Lambda's app.py with the given env + boto3 stubs.

    Each test gets a FRESH module to avoid cross-test state on the boto3
    clients (which the Lambdas create at import time)."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    # Patch boto3 BEFORE importing the module - the module creates its clients
    # at import time, so the patch must be in place by then.
    import boto3

    def fake_client(name, region_name=None, **kwargs):
        if name == "dynamodb":
            return fake_ddb
        if name == "lambda":
            return fake_lambda
        if name == "bedrock-runtime":
            return fake_bedrock or MagicMock()
        raise AssertionError(f"unexpected boto3.client({name!r}) in test")

    def fake_resource(name, region_name=None, **kwargs):
        # The generate Lambda uses a resource for the audit table (legacy path).
        # Return a thin shim that delegates put_item to the fake client.
        if name == "dynamodb":
            class FakeResource:
                def Table(self, table_name):
                    class TableShim:
                        def put_item(self, *, Item, ConditionExpression=None):
                            # Convert plain-Python item -> typed DDB item.
                            typed = {}
                            for k, v in Item.items():
                                if isinstance(v, bool):
                                    typed[k] = {"BOOL": v}
                                elif v is None:
                                    typed[k] = {"NULL": True}
                                elif isinstance(v, (int,)):
                                    typed[k] = {"N": str(v)}
                                elif isinstance(v, str):
                                    typed[k] = {"S": v}
                                elif isinstance(v, dict):
                                    # Treat as map of strings
                                    typed[k] = {"M": {kk: {"S": str(vv)} for kk, vv in v.items()}}
                                else:
                                    typed[k] = {"S": str(v)}
                            return fake_ddb.put_item(
                                TableName=table_name, Item=typed,
                                ConditionExpression=ConditionExpression,
                            )
                    return TableShim()
            return FakeResource()
        raise AssertionError(f"unexpected boto3.resource({name!r}) in test")

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(boto3, "resource", fake_resource)

    # Fresh import every time
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    if module_name in sys.modules:
        del sys.modules[module_name]
    module = importlib.import_module(module_name)
    return module


@pytest.fixture
def load_dispatcher(monkeypatch, fake_ddb, fake_lambda):
    """Returns a callable that imports dispatcher/app.py with env + stubs."""
    def _load(extra_env=None):
        env = {
            "AUDIT_TABLE_NAME":    "discharge-audit-audit",
            "WORKER_FUNCTION_NAME": "discharge-audit-generate",
            "IDEM_TTL_HOURS":      "24",
        }
        if extra_env:
            env.update(extra_env)
        sys.path = [p for p in sys.path if not p.endswith("dispatcher")
                                       and not p.endswith("status")
                                       and not p.endswith("generate")]
        return _load_lambda_module(SRC_DIRS["dispatcher"], env, monkeypatch,
                                    fake_ddb=fake_ddb, fake_lambda=fake_lambda)
    return _load


@pytest.fixture
def load_status(monkeypatch, fake_ddb):
    def _load(extra_env=None):
        env = {
            "AUDIT_TABLE_NAME":   "discharge-audit-audit",
            "RESULTS_TABLE_NAME": "discharge-audit-results",
        }
        if extra_env:
            env.update(extra_env)
        sys.path = [p for p in sys.path if not p.endswith("dispatcher")
                                       and not p.endswith("status")
                                       and not p.endswith("generate")]
        return _load_lambda_module(SRC_DIRS["status"], env, monkeypatch,
                                    fake_ddb=fake_ddb)
    return _load


@pytest.fixture
def load_worker(monkeypatch, fake_ddb):
    """Loads generate/app.py with a fake Bedrock that returns a canned PART A/B/C."""
    def _load(extra_env=None, bedrock_text=None, bedrock_error=None):
        env = {
            "AUDIT_TABLE_NAME":   "discharge-audit-audit",
            "RESULTS_TABLE_NAME": "discharge-audit-results",
            "BEDROCK_MODEL_ID":   "anthropic.claude-sonnet-4-6",
        }
        if extra_env:
            env.update(extra_env)
        sys.path = [p for p in sys.path if not p.endswith("dispatcher")
                                       and not p.endswith("status")
                                       and not p.endswith("generate")]
        fake_bedrock = MagicMock()
        if bedrock_error:
            fake_bedrock.converse.side_effect = bedrock_error
        else:
            fake_bedrock.converse.return_value = {
                "output": {"message": {"content": [
                    {"text": bedrock_text or _DEFAULT_BEDROCK_TEXT}
                ]}},
                "usage": {"inputTokens": 123, "outputTokens": 456},
            }
        return _load_lambda_module(SRC_DIRS["generate"], env, monkeypatch,
                                    fake_ddb=fake_ddb, fake_bedrock=fake_bedrock)
    return _load


_DEFAULT_BEDROCK_TEXT = """PART A - DISCHARGE SUMMARY

Patient: synthetic
Diagnosis: NSTEMI

PART B - GP LETTER

Dear GP,
NSTEMI inpatient stay; medication reconciled.

PART C - PATIENT VERSION

You came in with a heart attack. Take your tablets as written.
"""


# ---------------------------------------------------------------------------
# Convenience event builders
# ---------------------------------------------------------------------------
def http_api_event(user_sub: str, body: dict | str | None = None,
                    headers: dict | None = None,
                    path_params: dict | None = None,
                    method: str = "POST", path: str = "/generate") -> dict:
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "requestContext": {
            "http": {"method": method, "path": path, "sourceIp": "127.0.0.1"},
            "authorizer": {"jwt": {"claims": {"sub": user_sub, "token_use": "id"}}},
        },
        "headers": headers or {},
        "pathParameters": path_params,
        "body": json.dumps(body) if isinstance(body, dict) else body,
    }
