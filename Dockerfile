# syntax=docker/dockerfile:1
# Eval-harness image: scores saved runs and runs the test suite, offline.
# No AWS calls, no credentials. Saved runs are mounted at run time, never baked.
FROM python:3.13.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is rebuilt only when the pins change.
COPY requirements-eval.txt .
RUN pip install -r requirements-eval.txt

# Non-root user that will run everything (no home, no login shell).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

# Code + pinned reference data (gold, corpus, prompts). Folders named explicitly.
# --chmod: root owns the files; everyone (incl. app) can read them and enter folders,
# whatever the permissions happened to be on the build machine.
COPY --chmod=u=rwX,go=rX src/ src/
COPY --chmod=u=rwX,go=rX prompts/ prompts/
COPY --chmod=u=rwX,go=rX evals/ evals/
COPY --chmod=u=rwX,go=rX tests/ tests/

USER app

# Default: the test suite. Override the command to score a mounted batch.
CMD ["python", "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"]
