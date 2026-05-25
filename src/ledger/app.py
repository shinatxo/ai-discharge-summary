"""
AI Discharge Summary Assistant - ledger consumer Lambda (Phase 2, slice 3).

This is the tamper-evidence half of ADR-002. The audit table (slice 1) is
hash-only and write-once, and its execution role (slice 2) has no DeleteItem -
but those are controls a sufficiently privileged principal could still work
around (disable PITR, rewrite an item via a wider policy, etc.). This function
turns "we log usage" into "we can PROVE the log was not tampered with":

  DynamoDB Stream (every change event)  ->  THIS Lambda  ->  S3 Object Lock (WORM)

Every INSERT / MODIFY / REMOVE on the audit table emits a stream record. We copy
each record verbatim into an S3 bucket that has Object Lock enabled, so each
written object is immutable for its retention period - it cannot be overwritten
or deleted, not even by an admin (Compliance mode) or by normal principals
(Governance mode). The result is an append-only, independently-verifiable ledger
of every change the audit table ever saw.

No PHI is involved: the audit table is hash-only by design (ADR-002), so the
stream images - and therefore the ledger objects - contain only SHA-256 hashes,
ids, timestamps, and operational metadata. Never raw clinical content.

The execution role (see infra/template.yaml) can do exactly:
  - read THIS table's stream            (GetRecords/GetShardIterator/DescribeStream)
  - s3:PutObject to the ledger bucket   (no Get, no Delete, no retention-bypass)
  - kms:GenerateDataKey/Decrypt on the CMK *via S3 only*
  - write to this function's own log group.
Note the deliberate absence of s3:DeleteObject, s3:PutObjectRetention and
s3:BypassGovernanceRetention: the writer itself cannot shorten or remove a lock.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "eu-west-2")
LEDGER_BUCKET = os.environ["LEDGER_BUCKET_NAME"]
LEDGER_SCHEMA_VERSION = int(os.environ.get("LEDGER_SCHEMA_VERSION", "1"))

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Reuse the client across warm invocations (created at import time).
_s3 = boto3.client("s3", region_name=REGION)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_key(record: dict, captured_at: datetime) -> str:
    """Build a unique, browsable S3 key for one stream record.

    The DynamoDB stream eventID is globally unique per change event, so it
    guarantees we never collide two different events onto one key. We also
    Hive-partition by date (year=/month=/day=) so the ledger is cheap to scan
    by period later (and friendly to Athena/S3 Select if we ever query it).
    """
    event_id = record.get("eventID", "no-event-id")
    return (
        "ledger/"
        f"year={captured_at:%Y}/month={captured_at:%m}/day={captured_at:%d}/"
        f"{captured_at:%Y%m%dT%H%M%S}Z_{event_id}.json"
    )


def _envelope(record: dict, captured_at: datetime) -> dict:
    """Wrap the raw stream record in a small, self-describing envelope.

    We keep the DynamoDB-typed images (Keys/NewImage/OldImage) VERBATIM rather
    than deserialising them - the faithful, byte-for-byte record is what makes
    the ledger evidentially useful. Because the table is hash-only, these images
    carry no PHI.
    """
    ddb = record.get("dynamodb", {})
    return {
        "ledger_schema_version": LEDGER_SCHEMA_VERSION,
        "captured_at": captured_at.isoformat(),
        "event_id": record.get("eventID"),
        "event_name": record.get("eventName"),          # INSERT | MODIFY | REMOVE
        "aws_region": record.get("awsRegion"),
        "source_arn": record.get("eventSourceARN"),
        "sequence_number": ddb.get("SequenceNumber"),
        "approximate_creation_time": ddb.get("ApproximateCreationDateTime"),
        "size_bytes": ddb.get("SizeBytes"),
        "keys": ddb.get("Keys"),
        "new_image": ddb.get("NewImage"),
        "old_image": ddb.get("OldImage"),
    }


def lambda_handler(event, context):
    """Consume a batch of DynamoDB stream records and append each to the WORM
    ledger. Returns partial-batch failures so Lambda only retries the records
    that actually failed (the event source mapping is configured with
    FunctionResponseTypes: ReportBatchItemFailures).
    """
    records = event.get("Records", [])
    failures = []
    written = 0

    for record in records:
        seq = record.get("dynamodb", {}).get("SequenceNumber")
        try:
            captured_at = datetime.now(timezone.utc)
            envelope = _envelope(record, captured_at)
            body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")

            # A self-check hash of exactly what we stored, surfaced as object
            # metadata - a cheap integrity anchor for later verification.
            body_hash = _sha256_bytes(body)

            _s3.put_object(
                Bucket=LEDGER_BUCKET,
                Key=_object_key(record, captured_at),
                Body=body,
                ContentType="application/json",
                # Bucket default encryption (SSE-KMS via the CMK) and the bucket's
                # default Object Lock retention apply automatically - we do not
                # set retention per-object, so the writer can't choose a weaker
                # lock than the bucket policy mandates.
                Metadata={
                    "ledger-body-sha256": body_hash,
                    "event-id": str(record.get("eventID", "")),
                    "event-name": str(record.get("eventName", "")),
                },
            )
            written += 1
        except ClientError as exc:
            # Log the error class only (no record content), and mark THIS record
            # for retry. DynamoDB stream batches are ordered per shard, so we
            # report the failure and let Lambda redrive from this point.
            logger.error("ledger_put_failed: %s seq=%s",
                         exc.response.get("Error", {}).get("Code", "Unknown"), seq)
            if seq:
                failures.append({"itemIdentifier": seq})

    logger.info(json.dumps({
        "event": "ledger_batch_complete",
        "records_in": len(records),
        "written": written,
        "failed": len(failures),
        "bucket": LEDGER_BUCKET,
    }))

    # The contract Lambda expects when ReportBatchItemFailures is enabled.
    return {"batchItemFailures": failures}
