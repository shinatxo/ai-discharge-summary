"""
AI Discharge Summary Assistant - synthetic-traffic canary (Phase 3, Wave 3).

A scheduled Lambda that drives the DEPLOYED async path exactly as a signed-in
user would, and reports health as CloudWatch custom metrics. Two jobs:

  1. Baseline traffic for Wave 4 alarm calibration (a quiet stack emits nothing).
  2. Nightly regression detection across the 18 eval scenarios through the real
     Cognito -> API Gateway -> dispatcher -> worker -> Bedrock -> DynamoDB path.

Design + rationale: docs/WAVE3_SYNTHETIC_TRAFFIC_DESIGN.md.

Flow:
  1. Read the synthetic user's password from SSM Parameter Store (SecureString).
  2. Cognito InitiateAuth (USER_PASSWORD_AUTH) -> IdToken (the JWT the API
     authoriser accepts; `aud` = app client id is on the IdToken, not access).
  3. FAN OUT: POST /generate for every scenario, collecting job_ids. Because the
     API is async (202 in <1s), this returns almost immediately - no per-job wait.
  4. POLL: GET /generations/{id} for all in-flight jobs until each is terminal
     (complete/failed/expired) or the run hits its poll-timeout budget.
  5. Score per scenario: end-to-end latency, success, parse_ok, Bedrock throttle,
     and a soft structural-drift signal.
  6. PutMetricData -> namespace DischargeAssistant/Canary (per-scenario + ALL).

No PHI in logs (the inputs are synthetic, but we still log ids/metrics only, not
generated text). The canary talks ONLY HTTP to the API as a normal user; it holds
no DynamoDB / Bedrock / S3 permissions of its own.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# --- configuration (injected by the template) --------------------------------
REGION = os.environ.get("AWS_REGION", "eu-west-2")
API_BASE = os.environ["API_BASE"].rstrip("/")            # HttpApiEndpoint output
USER_POOL_CLIENT_ID = os.environ["USER_POOL_CLIENT_ID"]  # SPA app client id
CANARY_USERNAME = os.environ["CANARY_USERNAME"]
CANARY_PASSWORD_PARAM = os.environ["CANARY_PASSWORD_PARAM"]  # SSM SecureString name
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "DischargeAssistant/Canary")
# Optional comma-separated subset of scenario ids; empty = all 18.
CANARY_SCENARIOS = os.environ.get("CANARY_SCENARIOS", "").strip()
# Per-run poll budget and cadence.
POLL_TIMEOUT_S = int(os.environ.get("POLL_TIMEOUT_S", "540"))
POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "3"))
HTTP_TIMEOUT_S = float(os.environ.get("HTTP_TIMEOUT_S", "15"))

# Bound concurrent in-flight generations. Firing all 18 at once self-inflicts
# Bedrock on-demand throttling (observed 2026-05-30: 1/18 succeeded, rest
# throttled). A sliding window keeps the canary representative of real traffic
# (no real user sends 18 simultaneous requests) and within account quotas.
MAX_CONCURRENCY = int(os.environ.get("CANARY_MAX_CONCURRENCY", "5"))
# Small stagger between submissions to smooth the request rate (seconds).
SUBMIT_STAGGER_S = float(os.environ.get("SUBMIT_STAGGER_S", "1.0"))
# Throttling is the canonical transient error - retry a throttled job this many
# times before scoring it a failure (the throttle is still recorded as a metric).
THROTTLE_RETRIES = int(os.environ.get("CANARY_THROTTLE_RETRIES", "1"))

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Reuse clients across warm invocations.
_idp = boto3.client("cognito-idp", region_name=REGION)
_ssm = boto3.client("ssm", region_name=REGION)
_cw = boto3.client("cloudwatch", region_name=REGION)

_SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.json"


# -----------------------------------------------------------------------------
# Scenarios
# -----------------------------------------------------------------------------
def _load_scenarios():
    data = json.loads(_SCENARIOS_PATH.read_text(encoding="utf-8"))
    scenarios = data["scenarios"]
    if CANARY_SCENARIOS:
        wanted = {s.strip() for s in CANARY_SCENARIOS.split(",") if s.strip()}
        scenarios = [s for s in scenarios if s["id"] in wanted]
    return scenarios


# -----------------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------------
def _get_password():
    resp = _ssm.get_parameter(Name=CANARY_PASSWORD_PARAM, WithDecryption=True)
    return resp["Parameter"]["Value"]


def _authenticate(password: str) -> str:
    """USER_PASSWORD_AUTH -> IdToken. Raises on auth failure (caller marks the
    whole run failed: CanaryRunOk=0)."""
    resp = _idp.initiate_auth(
        ClientId=USER_POOL_CLIENT_ID,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": CANARY_USERNAME, "PASSWORD": password},
    )
    result = resp.get("AuthenticationResult") or {}
    id_token = result.get("IdToken")
    if not id_token:
        # e.g. a NEW_PASSWORD_REQUIRED challenge if the user wasn't set up with a
        # permanent password - surface it clearly for the bootstrap runbook.
        raise RuntimeError(
            f"no IdToken in auth result (challenge={resp.get('ChallengeName')})"
        )
    return id_token


# -----------------------------------------------------------------------------
# HTTP (stdlib only - no requests dependency)
# -----------------------------------------------------------------------------
def _http(method: str, url: str, id_token: str, body: dict | None = None,
          idempotency_key: str | None = None):
    """Return (status_code, parsed_json_or_None). Network/HTTP errors are caught
    and returned as a status with a small error dict, never raised - one bad
    scenario must not abort the whole run."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"authorization": f"Bearer {id_token}"}
    if data is not None:
        headers["content-type"] = "application/json"
    if idempotency_key:
        headers["idempotency-key"] = idempotency_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = {"error": "non_json", "raw": raw[:200]}
        return exc.code, parsed
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, {"error": "network", "message": str(exc.reason if hasattr(exc, "reason") else exc)}


def _post_generate(id_token: str, notes: str):
    """POST /generate -> (job_id|None, http_status, error_str|None)."""
    status, body = _http(
        "POST", f"{API_BASE}/generate", id_token,
        body={"notes": notes}, idempotency_key=str(uuid.uuid4()),
    )
    if status in (200, 202) and isinstance(body, dict) and body.get("job_id"):
        return body["job_id"], status, None
    err = (body or {}).get("error") if isinstance(body, dict) else f"http_{status}"
    return None, status, err or f"http_{status}"


def _get_status(id_token: str, job_id: str):
    status, body = _http("GET", f"{API_BASE}/generations/{job_id}", id_token)
    return status, (body if isinstance(body, dict) else {})


# -----------------------------------------------------------------------------
# Scoring helpers
# -----------------------------------------------------------------------------
def _is_throttle(status_body: dict) -> bool:
    """Bedrock throttling surfaces as a failed job whose error_message carries
    the AWS code. Capacity signal for the Wave 4 throttle alarm."""
    msg = f"{status_body.get('error_code', '')} {status_body.get('error_message', '')}".lower()
    return "throttl" in msg


def _structural_ok(status_body: dict) -> bool:
    """Soft regression signal: parse_ok true AND all three outputs present and
    non-empty. Intentionally NOT a byte-hash check (temperature 0 still has minor
    non-determinism - see design doc 5)."""
    if not status_body.get("parse_ok"):
        return False
    outputs = status_body.get("outputs") or {}
    return all((outputs.get(k) or "").strip() for k in ("summary", "gp_letter", "patient"))


# -----------------------------------------------------------------------------
# Metrics
# -----------------------------------------------------------------------------
def _metric(name, value, scenario, unit="Count"):
    return {
        "MetricName": name,
        "Dimensions": [{"Name": "Scenario", "Value": scenario}],
        "Value": float(value),
        "Unit": unit,
    }


def _emit(metric_data):
    """PutMetricData in chunks of 20 (API limit per request)."""
    for i in range(0, len(metric_data), 20):
        _cw.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=metric_data[i:i + 20])


def _scenario_metrics(scenario_id, res):
    """Per-scenario metric set from one scored result."""
    m = [
        _metric("EndToEndSuccess", 1 if res["success"] else 0, scenario_id),
        _metric("ParseOk", 1 if res["structural_ok"] else 0, scenario_id),
        _metric("JobFailed", 1 if res["failed"] else 0, scenario_id),
        _metric("BedrockThrottle", 1 if res["throttled"] else 0, scenario_id),
    ]
    if res["latency_ms"] is not None:
        m.append(_metric("EndToEndLatencyMs", res["latency_ms"], scenario_id, unit="Milliseconds"))
    return m


# -----------------------------------------------------------------------------
# Handler
# -----------------------------------------------------------------------------
def lambda_handler(event, context):
    run_started = time.time()
    scenarios = _load_scenarios()

    # --- auth (a failure here fails the whole run) ---------------------------
    try:
        password = _get_password()
        id_token = _authenticate(password)
    except (ClientError, RuntimeError, KeyError) as exc:
        logger.error(json.dumps({"event": "canary_auth_failed", "error": str(exc)[:200]}))
        # Heartbeat: the canary ran but could not operate -> alarmable.
        _emit([_metric("CanaryRunOk", 0, "ALL")])
        return {"ok": False, "error": "auth_failed"}

    # --- bounded-concurrency submit + poll -----------------------------------
    # Sliding window: keep at most MAX_CONCURRENCY generations in flight; as each
    # finishes, submit the next. Throttled jobs are requeued (up to THROTTLE_RETRIES)
    # rather than scored as failures, but the throttle is still recorded.
    deadline = run_started + POLL_TIMEOUT_S
    queue = list(scenarios)                 # scenarios waiting to be submitted (FIFO)
    by_id = {s["id"]: s for s in scenarios}
    retries_left = {s["id"]: THROTTLE_RETRIES for s in scenarios}
    inflight = {}                           # sid -> {"job_id", "t_post"}
    results = {}                            # sid -> scored result
    throttle_seen = set()                   # sids that hit a throttle on any attempt

    def _submit_next():
        if not queue:
            return
        s = queue.pop(0)
        job_id, http_status, err = _post_generate(id_token, s["notes"])
        if job_id:
            inflight[s["id"]] = {"job_id": job_id, "t_post": time.time()}
        else:
            if str(http_status) == "429":
                throttle_seen.add(s["id"])
            results[s["id"]] = {
                "success": False, "failed": True, "throttled": s["id"] in throttle_seen,
                "structural_ok": False, "latency_ms": None, "status": f"dispatch_error:{err}",
            }
            logger.warning(json.dumps({"event": "canary_dispatch_failed",
                                        "scenario": s["id"], "http": http_status, "error": err}))
        if SUBMIT_STAGGER_S:
            time.sleep(SUBMIT_STAGGER_S)

    def _fill_window():
        while queue and len(inflight) < MAX_CONCURRENCY and time.time() < deadline:
            _submit_next()

    _fill_window()
    while inflight and time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        for sid in list(inflight.keys()):
            job = inflight[sid]
            _http_status, body = _get_status(id_token, job["job_id"])
            state = body.get("status")
            if state not in ("complete", "failed", "expired"):
                continue
            throttled = _is_throttle(body)
            if throttled:
                throttle_seen.add(sid)
            # Retry a throttled failure instead of scoring it - requeue once.
            if throttled and state in ("failed", "expired") and retries_left[sid] > 0:
                retries_left[sid] -= 1
                del inflight[sid]
                queue.append(by_id[sid])
                logger.info(json.dumps({"event": "canary_retry_throttled", "scenario": sid}))
            else:
                results[sid] = {
                    "success": state == "complete",
                    "failed": state in ("failed", "expired"),
                    "throttled": sid in throttle_seen,
                    "structural_ok": _structural_ok(body) if state == "complete" else False,
                    "latency_ms": int((time.time() - job["t_post"]) * 1000),
                    "status": state,
                }
                del inflight[sid]
            _fill_window()

    # --- timeouts: anything still in-flight OR never submitted ---------------
    for sid, job in inflight.items():
        results[sid] = {
            "success": False, "failed": True, "throttled": sid in throttle_seen,
            "structural_ok": False, "latency_ms": int((time.time() - job["t_post"]) * 1000),
            "status": "poll_timeout",
        }
    for s in queue:
        results.setdefault(s["id"], {
            "success": False, "failed": True, "throttled": s["id"] in throttle_seen,
            "structural_ok": False, "latency_ms": None, "status": "not_submitted",
        })

    # --- emit metrics (per scenario + ALL aggregate) -------------------------
    metric_data = []
    latencies = []
    n_success = n_parse_ok = n_throttle = 0
    for s in scenarios:
        res = results[s["id"]]
        metric_data += _scenario_metrics(s["id"], res)
        if res["latency_ms"] is not None and res["success"]:
            latencies.append(res["latency_ms"])
        n_success += 1 if res["success"] else 0
        n_parse_ok += 1 if res["structural_ok"] else 0
        n_throttle += 1 if res["throttled"] else 0

    total = len(scenarios)
    metric_data += [
        _metric("EndToEndSuccess", n_success, "ALL"),
        _metric("ParseOk", n_parse_ok, "ALL"),
        _metric("JobFailed", total - n_success, "ALL"),
        _metric("BedrockThrottle", n_throttle, "ALL"),
        _metric("SuccessRatePct", (100.0 * n_success / total) if total else 0, "ALL", unit="Percent"),
        _metric("CanaryRunOk", 1, "ALL"),
    ]
    if latencies:
        latencies.sort()
        p90 = latencies[min(len(latencies) - 1, int(0.9 * len(latencies)))]
        metric_data.append(_metric("EndToEndLatencyMs", p90, "ALL", unit="Milliseconds"))

    _emit(metric_data)

    summary = {
        "event": "canary_run_complete",
        "scenarios": total,
        "success": n_success,
        "parse_ok": n_parse_ok,
        "throttled": n_throttle,
        "run_ms": int((time.time() - run_started) * 1000),
    }
    logger.info(json.dumps(summary))
    return {"ok": True, **summary}
