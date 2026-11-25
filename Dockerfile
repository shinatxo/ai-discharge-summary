# syntax=docker/dockerfile:1
# Eval image (ADR-011 D5, D11; docs/WS7-ENTRYPOINT.md, "Part 6: the image").
# - The default command runs the nine WS7 test files (the full suite runs in CI),
#   offline: a bare `docker run` makes no AWS call and needs no network.
# - The ECS task (slice 3) overrides the command to run evals/ws7_entry.py. That job
#   calls Bedrock, S3, SSM, CloudWatch and SNS, with the task role.
# - No credentials are ever baked in. Saved runs and calibration folders are never
#   baked in either (.dockerignore); a saved batch is mounted at run time.
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

# The commit the image was built from (ADR-011 D11):
#   docker build --build-arg WS7_GIT_COMMIT="$(git rev-parse HEAD)" ...
# There is NO default. Built without the argument, the variable is empty and the job
# refuses to run (ws7_identity_missing). The task definition must not set it.
# Declared after every RUN and COPY, so a new commit reinstalls and recopies nothing.
ARG WS7_GIT_COMMIT
ENV WS7_GIT_COMMIT=${WS7_GIT_COMMIT}

USER app

# Default: the nine WS7 test files (the full suite runs in CI), offline. The task
# definition gives the ws7_entry.py command. tests/test_image_files.py checks this
# list against tests/test_ws7_*.py.
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", \
     "tests/test_ws7_compare.py", "tests/test_ws7_reserve.py", "tests/test_ws7_client.py", \
     "tests/test_ws7_store.py", "tests/test_ws7_ledger.py", "tests/test_ws7_manifest.py", \
     "tests/test_ws7_summary.py", "tests/test_ws7_publish.py", "tests/test_ws7_entry.py"]
