"""StepContext — what a model step needs besides its JSON input (ADR-009, W2 part 2).

run_step(name, input, *, ctx=None): the input is JSON (it round-trips through
json), so a Bedrock client cannot travel in it. The context travels beside it,
keyword-only. Code steps never see it; a model step without one fails closed
with StepError("no_client").

The caller builds the client. Its botocore Config carries the read timeout and
must set max_attempts=1 — retries are the step's job, and only for throttling
(ADR-009 Q3 (3)). Step 2 wants read_timeout=90. Nothing in this package
creates a client.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class StepContext:
    bedrock: Any                     # a bedrock-runtime client, or a fake with .converse(**kw)
    model_id: str
    prompt_caching: bool = False     # add a cachePoint after the system prompt (live stack: on)
    sleep: Callable[[float], None] = field(default=time.sleep)          # injectable for tests
    jitter: Callable[[float, float], float] = field(default=random.uniform)
