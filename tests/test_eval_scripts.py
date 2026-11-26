"""infra/eval/*.sh - the operator's guarded scripts (WS7 slice 3; docs/WS7-RUNBOOK.md).

THE RULE these tests hold: no image is pushed unless check 5 has passed on that same
image, identified by its image ID, after its last build. And: the schedules' state
is changed by schedules.sh only.

NO TEST HERE CAN REACH A REAL docker OR A REAL aws. This machine and CI both have
them, and this machine has real credentials. So every script is run:

- from a copy in the test's own folder, never from the repository;
- with an environment built from nothing, not copied: no AWS_* variable but
  AWS_CONFIG_FILE and AWS_SHARED_CREDENTIALS_FILE, which point at two empty files;
- with DOCKER_HOST naming a socket that does not exist;
- with PATH starting at a folder of stubs named `docker`, `aws` and `git`.

The scripts call docker and aws by name only (a test reads them for a path to
either), so the stub is what runs. A stub answers only the calls a test planned: any
other call is recorded, answered with status 97, and FAILS THE TEST.

`git` is the real git behind a wrapper. The wrapper lets one thing differ: a clone of
the GitHub URL is given the test's own scratch repository instead, and a clone of
anything else is a failed test. Every repository, commit, account and digest here
is invented.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "infra" / "eval"
GUIDE = ROOT / "docs" / "WS7-ENTRYPOINT.md"
GITHUB = "https://github.com/shinatxo/ai-discharge-summary.git"
BRANCH = "feat/agentic-pipeline"
ACCOUNT = "000000000000"                         # invented
REGION = "eu-west-2"
IMAGE = "sha256:" + "ab" * 32                    # an invented image ID
OTHER_IMAGE = "sha256:" + "cd" * 32
URI = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/discharge-eval"
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:discharge-eval-alerts"
RUN_LIMIT_S = 120
REAL_GIT = shutil.which("git")
ANY, REST = "*", "..."

STUB = '''#!{python}
import json, os, signal, sys
tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
entry = {{"tool": tool, "args": args}}
def note():
    with open(os.environ["WS7_STUB_LOG"], "a") as log:
        log.write(json.dumps(entry) + "\\n")
if tool == "git":
    if "clone" in args:
        if args[-2] != {github!r}:
            entry["unexpected"] = True
            note()
            sys.exit(97)
        args[-2] = os.environ["WS7_STUB_REMOTE"]
    note()
    os.execv({git!r}, [{git!r}] + args)
with open(os.environ["WS7_STUB_PLAN"]) as plan:
    rules = json.load(plan)
def fits(rule):
    want = list(rule["args"])
    if rule["tool"] != tool:
        return False
    if want and want[-1] == "...":
        want = want[:-1]
        if len(args) < len(want):
            return False
    elif len(args) != len(want):
        return False
    return all(w == "*" or w == a for w, a in zip(want, args))
rule = next((r for r in rules if fits(r)), None)
if rule is None:
    entry["unexpected"] = True
    note()
    sys.stderr.write("stub: unplanned call: %s %s\\n" % (tool, " ".join(args)))
    sys.exit(97)
entry["rule"] = rule["name"]
if rule.get("do") == "context":
    root = args[-1]
    entry["context"] = sorted(os.path.relpath(os.path.join(d, f), root)
                              for d, _dirs, files in os.walk(root) for f in files)
if rule.get("do") == "stdin":
    entry["stdin"] = sys.stdin.read()
note()
if rule.get("do") == "sigterm":
    os.kill(os.getppid(), signal.SIGTERM)
sys.stdout.write(rule.get("out", ""))
sys.stderr.write(rule.get("err", ""))
sys.exit(rule.get("status", 0))
'''


def rule(name: str, tool: str, args: list, out: str = "", status: int = 0, **more) -> dict:
    return {"name": name, "tool": tool, "args": list(args),
            "out": out + ("\n" if out and not out.endswith("\n") else ""), "status": status, **more}


class Bench:
    """One test's world: the scripts' copy, the stubs, the environment and the log."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.repo = tmp_path / "copy"
        (self.repo / "infra" / "eval").mkdir(parents=True)
        for script in SCRIPTS.glob("*.sh"):
            shutil.copy2(script, self.repo / "infra" / "eval" / script.name)
        shutil.copy2(ROOT / "infra" / "eval-template.yaml", self.repo / "infra" / "eval-template.yaml")
        self.bin = tmp_path / "stubs"
        self.bin.mkdir()
        for name in ("docker", "aws", "git"):
            stub = self.bin / name
            stub.write_text(STUB.format(python=sys.executable, github=GITHUB, git=REAL_GIT))
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.work = tmp_path / "tmp"
        self.work.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        self.empty = [tmp_path / "aws-config", tmp_path / "aws-credentials"]
        for path in self.empty:
            path.write_bytes(b"")
        self.log, self.plan = tmp_path / "calls.jsonl", tmp_path / "plan.json"
        self.remote = tmp_path / "remote.git"
        self.socket = tmp_path / "no-such-docker.sock"
        # Built from nothing: nothing of the developer's or the runner's own environment.
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(self.work),
                    "LC_ALL": "C", "AWS_CONFIG_FILE": str(self.empty[0]),
                    "AWS_SHARED_CREDENTIALS_FILE": str(self.empty[1]),
                    "DOCKER_HOST": f"unix://{self.socket}",
                    "WS7_STUB_LOG": str(self.log), "WS7_STUB_PLAN": str(self.plan),
                    "WS7_STUB_REMOTE": str(self.remote)}

    def run(self, script: str, argv: list, rules: list, stdin: str = "", env: dict | None = None,
            allow_unplanned: bool = False):
        self.plan.write_text(json.dumps(rules))
        if self.log.exists():
            self.log.unlink()
        done = subprocess.run(["/bin/bash", str(self.repo / "infra" / "eval" / script), *argv],
                              input=stdin, capture_output=True, text=True, timeout=RUN_LIMIT_S,
                              env={**self.env, **(env or {})}, cwd=str(self.tmp))
        self.calls = [json.loads(line) for line in self.log.read_text().splitlines()] \
            if self.log.exists() else []
        if not allow_unplanned:
            unplanned = [c for c in self.calls if c.get("unexpected")]
            assert not unplanned, f"a stub was called with something no test planned: {unplanned}"
        return done

    def did(self, tool: str, *first: str) -> list:
        """The calls of `tool` whose arguments start with `first`."""
        return [c for c in self.calls if c["tool"] == tool and c["args"][:len(first)] == list(first)]

    def names(self) -> list:
        return [c.get("rule") for c in self.calls if c["tool"] != "git"]


@pytest.fixture
def bench(tmp_path):
    assert REAL_GIT, "git is needed for these tests"
    return Bench(tmp_path)


# --------------------------------------------------------- a scratch remote

FILES = {"Dockerfile": "FROM scratch\n", ".dockerignore": "evals/runs\nevals/calibration\n",
         "requirements-eval.txt": "pytest==0\n", "src/app.py": "x = 1\n",
         "prompts/p.md": "invented\n", "evals/ws7_x.py": "y = 2\n",
         "evals/runs/saved.json": "{\"invented\": true}\n",
         "evals/calibration/sheet.json": "{\"invented\": true}\n",
         "tests/test_x.py": "def test_x():\n    pass\n", "docs/d.md": "doc\n", "infra/t.yaml": "t: 1\n"}
IMAGE_FILES = sorted(["Dockerfile", ".dockerignore", "requirements-eval.txt", "src/app.py",
                      "prompts/p.md", "evals/ws7_x.py", "tests/test_x.py"])
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin",
           "GIT_AUTHOR_NAME": "Invented", "GIT_AUTHOR_EMAIL": "invented@example.invalid",
           "GIT_COMMITTER_NAME": "Invented", "GIT_COMMITTER_EMAIL": "invented@example.invalid"}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run([REAL_GIT, *args], cwd=str(cwd), env=GIT_ENV, capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def make_remote(bench: Bench, change=None) -> dict:
    """A scratch repository with the image's paths and the protected folders, tracked.
    Returns the commits: `old` and `tip` on the base branch, `side` on a branch that
    was never merged, and `tree`, the tip's tree (40 hex that is not a commit)."""
    work = bench.tmp / "remote-work"
    work.mkdir()
    git(work, "init", "--quiet", "--initial-branch", BRANCH)
    for name, body in FILES.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(body)
    git(work, "add", "--all")
    git(work, "commit", "--quiet", "-m", "first")
    old = git(work, "rev-parse", "HEAD")
    (work / "src" / "app.py").write_text("x = 2\n")
    if change:
        change(work)
    git(work, "add", "--all")
    git(work, "commit", "--quiet", "-m", "second")
    tip = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "--quiet", "-b", "task/unmerged")
    (work / "src" / "app.py").write_text("x = 3\n")
    git(work, "commit", "--quiet", "-am", "unreviewed")
    side = git(work, "rev-parse", "HEAD")
    git(work, "clone", "--quiet", "--bare", str(work), str(bench.remote))
    return {"old": old, "tip": tip, "side": side, "tree": git(work, "rev-parse", f"{tip}^{{tree}}")}


# ------------------------------------------------------------ the push plan

def push_rules(commit: str, **change) -> list:
    """Every call a push that succeeds makes, by name. `change` replaces a rule's
    answer: `name={"out": ..., "status": ...}`."""
    tag = f"{commit}-{IMAGE[7:19]}"
    local = f"discharge-eval:push-{commit[:12]}"
    run = ["run", "--rm", "--network", "none"]
    env_lines = "PATH=/usr/local/bin\nPYTHONDONTWRITEBYTECODE=1\nWS7_GIT_COMMIT=" + commit
    rules = [
        rule("sts_account", "aws", ["sts", "get-caller-identity", "--region", REGION, "--query",
                                    "Account", "--output", "text"], ACCOUNT),
        rule("sts_arn", "aws", ["sts", "get-caller-identity", "--region", REGION, "--query", "Arn",
                                "--output", "text"], f"arn:aws:iam::{ACCOUNT}:user/invented"),
        rule("build", "docker", ["build", "--platform", "linux/arm64", "--build-arg",
                                 f"WS7_GIT_COMMIT={commit}", "-t", local, ANY], do="context"),
        rule("inspect_local", "docker", ["image", "inspect", "--format", "{{.Id}}", local], IMAGE),
        rule("tests", "docker", run + [IMAGE], "....\n4 passed in 0.10s"),
        rule("printenv", "docker", run + [IMAGE, "printenv", "WS7_GIT_COMMIT"], commit),
        rule("usage", "docker", run + [IMAGE, "python", "evals/ws7_entry.py"], status=2),
        rule("identity", "docker", run + ["-e", "WS7_IMAGE_DIGEST=sha256:" + "0" * 64, IMAGE,
                                          "python", "-c", ANY], commit),
        rule("arch", "docker", ["image", "inspect", "--format",
                                "{{.Architecture}} {{.Os}} {{.Config.User}}", IMAGE], "arm64 linux app"),
        rule("env", "docker", ["image", "inspect", "--format",
                               "{{range .Config.Env}}{{println .}}{{end}}", IMAGE], env_lines),
        rule("check5_id", "docker", ["image", "inspect", "--format", "{{.Id}}", IMAGE], IMAGE),
        rule("check5", "docker", run + [IMAGE, "sh", "-c", ANY, "check5", "/app"], "absent docs"),
        rule("repository", "aws", ["ecr", "describe-repositories", "--region", REGION, "--registry-id",
                                   ACCOUNT, "--repository-names", "discharge-eval", "--query",
                                   "repositories[0].repositoryUri", "--output", "text"], URI),
        rule("password", "aws", ["ecr", "get-login-password", "--region", REGION], "invented-password"),
        rule("login", "docker", ["login", "--username", "AWS", "--password-stdin", URI.split("/")[0]],
             do="stdin"),
        rule("tag", "docker", ["tag", IMAGE, f"{URI}:{tag}"]),
        rule("inspect_tag", "docker", ["image", "inspect", "--format", "{{.Id}}", f"{URI}:{tag}"], IMAGE),
        rule("push", "docker", ["push", f"{URI}:{tag}"]),
        rule("pushed", "aws", ["ecr", "describe-images", "--region", REGION, "--registry-id", ACCOUNT,
                               "--repository-name", "discharge-eval", "--image-ids", f"imageTag={tag}",
                               "--query", "imageDetails[0].imageDigest", "--output", "text"], IMAGE),
    ]
    assert set(change) <= {r["name"] for r in rules}, change
    for r in rules:
        r.update(change.get(r["name"], {}))
    return rules


PUSH_STEPS = ("repository", "password", "login", "tag", "inspect_tag", "push", "pushed")


def nothing_reached_the_registry(bench: Bench) -> None:
    assert not bench.did("docker", "push") and not bench.did("docker", "tag")
    assert not bench.did("docker", "login") and not bench.did("aws", "ecr")


def test_the_scripts_run_against_the_stubs_and_nothing_else(bench):
    """The control for this whole file. In the environment every script is given, `docker`,
    `aws` and `git` are the stubs; no AWS_* variable is set but the two that point at
    empty files; and DOCKER_HOST names a socket that is not there."""
    done = subprocess.run(["/bin/bash", "-c", "command -v docker; command -v aws; command -v git"],
                          env=bench.env, capture_output=True, text=True)
    assert done.stdout.split() == [str(bench.bin / name) for name in ("docker", "aws", "git")]
    aws_names = sorted(name for name in bench.env if name.startswith("AWS_"))
    assert aws_names == ["AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"]
    assert all(path.read_bytes() == b"" for path in bench.empty)
    assert bench.env["DOCKER_HOST"] == f"unix://{bench.socket}" and not bench.socket.exists()
    assert not any(name.startswith(("DOCKER_CONFIG", "GIT_")) for name in bench.env)
    # A call nobody planned is answered 97 and recorded as unexpected.
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", "0" * 40], [],
                     allow_unplanned=True)
    assert done.returncode == 1 and [c for c in bench.calls if c.get("unexpected")]
    with pytest.raises(AssertionError, match="no test planned"):
        bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", "0" * 40], [])


def paths_to(text: str) -> list:
    """Every word that reaches docker or aws by a path, not by name."""
    words = re.split(r"[\s\"'`;|&()<>=]+", text)
    return [w for w in words if "/" in w and w.rsplit("/", 1)[1] in ("docker", "aws")]


def test_the_scripts_call_docker_and_aws_by_name_only():
    assert paths_to("x=$(/usr/local/bin/docker ps); /opt/homebrew/bin/aws s3 ls; ./aws") == \
        ["/usr/local/bin/docker", "/opt/homebrew/bin/aws", "./aws"]                # the control
    scripts = sorted(SCRIPTS.glob("*.sh"))
    assert [s.name for s in scripts] == ["check5.sh", "deploy.sh", "guard.sh", "push-image.sh",
                                         "schedules.sh"]
    for script in scripts:
        text = script.read_text()
        assert paths_to(text) == [], script.name
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for way_round in ("command -v", "which docker", "which aws", "type -p", "env docker",
                          "env aws", "exec -a", "PATH="):
            assert way_round not in code, (script.name, way_round)


def test_check5_in_the_script_is_part_6s_check_5_byte_for_byte():
    """One check 5: the control in the push script is the text the guide documents."""
    guide = GUIDE.read_text(encoding="utf-8")
    start = guide.index("CHECK5_SCRIPT='\n")
    documented = guide[start:guide.index('check5 $IMG; echo "check 5 status $?"', start)].rstrip("\n")
    script = (SCRIPTS / "check5.sh").read_text(encoding="utf-8")
    ours = script[script.index("# --- begin part 6 check 5 ---\n") + len("# --- begin part 6 check 5 ---\n"):
                  script.index("\n# --- end part 6 check 5 ---")]
    assert ours == documented and "check5() {" in ours and "[Rr][Uu][Nn][Ss]" in ours
    assert script.count("docker ") == ours.count("docker ")           # nothing else here calls docker


# ================================================================ the push ==

def test_a_push_checks_the_image_by_its_id_and_pushes_that_id(bench):
    c = make_remote(bench)["tip"]
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c))
    assert done.returncode == 0, done.stderr
    names = bench.names()
    # The guard first; the build; every check; check 5 last of the checks; then the push.
    assert names == ["sts_account", "sts_arn", "build", "inspect_local", "tests", "printenv", "usage",
                     "identity", "arch", "env", "check5_id", "check5", *PUSH_STEPS]
    assert done.stdout.splitlines()[-4:] == [f"PUSHED {URI}@{IMAGE}", f"commit {c}",
                                             f"tag    {c}-{IMAGE[7:19]}",
                                             f"digest {IMAGE}   (the ImageDigest for infra/eval/deploy.sh)"]
    # What the builder was given: the commit's image files and nothing else.
    built = bench.did("docker", "build")[0]
    assert built["context"] == IMAGE_FILES
    assert built["args"][-1].startswith(str(bench.work))               # the export, never the checkout
    assert bench.did("docker", "login")[0]["stdin"] == "invented-password\n"
    assert list(bench.work.iterdir()) == []                            # the temporary folder is gone


def test_the_commit_comes_from_github_with_this_machines_git_configuration_left_out(bench):
    c = make_remote(bench)["tip"]
    hostile = bench.tmp / "hostile.gitconfig"
    hostile.write_text("[core]\n\tautocrlf = true\n[url \"/nowhere/\"]\n\tinsteadOf = https://github.com/\n")
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c),
                     env={"GIT_DIR": str(ROOT / ".git"), "GIT_WORK_TREE": str(ROOT),
                          "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.autocrlf",
                          "GIT_CONFIG_VALUE_0": "true", "XDG_CONFIG_HOME": str(bench.tmp)})
    assert done.returncode == 0, done.stderr
    gits = bench.did("git")
    clone = gits[0]["args"]
    assert clone == ["clone", "--quiet", "--bare", "--single-branch", "--branch", BRANCH,
                     str(bench.remote), clone[-1]]                     # the wrapper saw GitHub's URL
    assert clone[-1].startswith(str(bench.work))
    # Every later git call names the clone; none can read the local checkout.
    assert len(gits) > 5 and all(g["args"][0] == f"--git-dir={clone[-1]}" for g in gits[1:])
    script = (SCRIPTS / "push-image.sh").read_text()
    assert f"REMOTE={GITHUB}\n" in script and f"BRANCH={BRANCH}\n" in script
    assert "export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1" in script
    assert bench.did("docker", "build")[0]["context"] == IMAGE_FILES


@pytest.mark.parametrize("which, says", [
    ("old", None),                                   # an ancestor of the branch: allowed
    # A clone of a local folder copies every object, so the unmerged commit is there and
    # the ancestor rule refuses it. Over HTTPS GitHub sends the branch only, and the
    # refusal is the one the next line has.
    ("side", "nor an ancestor of it"),
    ("tree", "is not a commit on feat/agentic-pipeline as fetched"),
    ("short", "must be the full 40-character commit"),
    ("upper", "must be the full 40-character commit"),
    ("branch", "must be the full 40-character commit"),
    ("unknown", "is not a commit on feat/agentic-pipeline as fetched")])
def test_only_a_commit_on_the_base_branch_as_fetched_is_built(bench, which, says):
    """An image is only ever of a reviewed, merged commit: the branch as fetched, or an
    ancestor of it. A tree's SHA, an unmerged commit and a name are refused."""
    made = make_remote(bench)
    commit = {"short": made["tip"][:12], "upper": made["tip"].upper(), "branch": BRANCH,
              "unknown": "1" * 40}.get(which) or made[which]
    plan = push_rules(commit if len(commit) == 40 else made["tip"])
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", commit], plan)
    if says is None:
        assert done.returncode == 0 and bench.did("docker", "push")
        return
    assert done.returncode == 1 and says in done.stderr and "Nothing was pushed." in done.stderr
    assert not bench.did("docker") and not bench.did("aws", "ecr")
    assert list(bench.work.iterdir()) == []


def test_a_commit_that_is_fetched_but_is_not_an_ancestor_of_the_branch_is_refused(bench):
    """The ancestor rule alone: the commit is in the clone (a tag reaches it), it is a
    commit, and it is not the branch or an ancestor of it."""
    made = make_remote(bench)
    git(bench.remote, "update-ref", f"refs/heads/{BRANCH}", made["old"])   # the branch, one behind
    git(bench.remote, "tag", "reachable", made["tip"])
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", made["tip"]],
                     push_rules(made["tip"]))
    assert done.returncode == 1 and "nor an ancestor of it" in done.stderr
    assert not bench.did("docker")
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", made["old"]],
                     push_rules(made["old"]))                              # the partner
    assert done.returncode == 0


def _a_link(work: Path) -> None:
    os.symlink("test_x.py", work / "tests" / "linked.py")


def _an_export_ignore(work: Path) -> None:
    (work / ".gitattributes").write_text("")                             # not exported: not an image path
    (work / "tests" / ".gitattributes").write_text("test_x.py export-ignore\n")


def _an_export_subst(work: Path) -> None:
    (work / "src" / ".gitattributes").write_text("app.py export-subst\n")
    (work / "src" / "app.py").write_text("x = '$Format:%H$'\n")


def _another_spelling(work: Path) -> None:
    # Taken out of the index first: on a volume that ignores case git would otherwise
    # keep the name it already has.
    git(work, "rm", "-r", "--quiet", "--cached", "evals/runs")
    shutil.rmtree(work / "evals" / "runs")
    (work / "evals" / "Runs").mkdir()
    (work / "evals" / "Runs" / "saved.json").write_text("{}\n")


def _a_name_that_is_not_ascii(work: Path) -> None:
    (work / "evals" / "runſ.py").write_text("z = 1\n")


@pytest.mark.parametrize("change, says", [
    (_a_link, "holds a link or an unusual file"),
    (_an_export_ignore, "the export is missing a file of the commit"),
    (_an_export_subst, "the export's bytes are not the commit's"),
    (_another_spelling, "a protected folder under another spelling"),
    (_a_name_that_is_not_ascii, "is not plain ASCII")])
def test_an_export_that_is_not_the_commits_files_byte_for_byte_is_never_built(bench, change, says):
    """The commit label must be true of every copied file. Whatever makes the export
    differ from the commit (an attribute that drops or rewrites a file, a link, a
    name the Mac's volume would fold) stops the build. The partner is the plain commit."""
    c = make_remote(bench, change)["tip"]
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c))
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("docker") and not bench.did("aws", "ecr")
    assert list(bench.work.iterdir()) == []


# Each check that must stop the push, by the answer that fails it.
REFUSALS = {
    "the build fails": ({"build": {"status": 1}}, "the build failed"),
    "no image id": ({"inspect_local": {"status": 1}}, "the built image's ID could not be read"),
    "an empty image id": ({"inspect_local": {"out": ""}}, "the built image's ID could not be read"),
    "an id that is not a digest": ({"inspect_local": {"out": "discharge-eval:latest\n"}},
                                   "is not a sha256 digest"),
    "a short digest": ({"inspect_local": {"out": "sha256:abcd\n"}}, "is not a sha256 digest"),
    "check 3 fails": ({"tests": {"status": 1, "out": "1 failed, 3 passed in 0.1s\n"}}, "check 3"),
    "check 3 skips a test": ({"tests": {"out": "3 passed, 1 skipped in 0.1s\n"}}, "check 3"),
    "check 3 prints no summary": ({"tests": {"out": ""}}, "check 3"),
    "check 3 collects nothing": ({"tests": {"out": "no tests ran in 0.01s\n"}}, "check 3"),
    "check 4 cannot read the commit": ({"printenv": {"status": 1}}, "check 4"),
    "check 4 reads an empty commit": ({"printenv": {"out": ""}}, "check 4"),
    "check 4 reads another commit": ({"printenv": {"out": "f" * 40 + "\n"}}, "check 4"),
    "check 6 exits 0": ({"usage": {"status": 0}}, "check 6"),
    "check 6 exits 1": ({"usage": {"status": 1}}, "check 6"),
    "check 8 fails": ({"identity": {"status": 1, "out": ""}}, "check 8"),
    "check 8 names another commit": ({"identity": {"out": "f" * 40 + "\n"}}, "check 8"),
    "check 9 another architecture": ({"arch": {"out": "amd64 linux app\n"}}, "check 9"),
    "check 9 runs as root": ({"arch": {"out": "arm64 linux \n"}}, "check 9"),
    "check 9 fails": ({"arch": {"status": 1}}, "check 9"),
    "check 10 a credential": ({"env": {"out": "AWS_ACCESS_KEY_ID=invented\nWS7_GIT_COMMIT=x\n"}},
                              "check 10"),
    "check 10 the digest baked in": ({"env": {"out": "WS7_GIT_COMMIT=x\nWS7_IMAGE_DIGEST=y\n"}},
                                     "check 10"),
    "check 10 the hash seed baked in": ({"env": {"out": "WS7_GIT_COMMIT=x\nPYTHONHASHSEED=0\n"}},
                                        "check 10"),
    "check 10 no commit": ({"env": {"out": "PATH=/bin\n"}}, "check 10"),
    "check 10 fails": ({"env": {"status": 1}}, "check 10"),
    "check 5 finds a protected folder": ({"check5": {"status": 1, "out": "PRESENT evals/Runs\n"}},
                                         "check 5 did not pass"),
    "check 5 cannot run": ({"check5": {"status": 125, "out": ""}}, "check 5 did not pass"),
    "check 5 has no image": ({"check5_id": {"status": 1}}, "check 5 did not pass"),
}


@pytest.mark.parametrize("case", list(REFUSALS))
def test_no_image_is_pushed_unless_every_check_and_then_check_5_passed_on_it(bench, case):
    """THE RULE. Each check that fails stops the script: nothing is tagged, no login is
    made and nothing is pushed. The partner is the push test above."""
    change, says = REFUSALS[case]
    c = make_remote(bench)["tip"]
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c, **change))
    assert done.returncode == 1, done.stdout
    assert says in done.stderr and "Nothing was pushed." in done.stderr
    assert "PUSHED" not in done.stdout
    nothing_reached_the_registry(bench)
    assert list(bench.work.iterdir()) == []


def test_check_5_is_judged_by_its_status_and_not_by_what_it_printed(bench):
    """Part 6's note for this script. Lines that look like a pass, with a status that is
    not 0, push nothing; no lines at all, with status 0, is a pass."""
    c = make_remote(bench)["tip"]
    looks_fine = "absent evals/runs\nabsent evals/calibration\nabsent docs\nabsent infra\n"
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c],
                     push_rules(c, check5={"status": 1, "out": looks_fine}))
    assert done.returncode == 1 and "check 5 did not pass" in done.stderr
    nothing_reached_the_registry(bench)
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c],
                     push_rules(c, check5={"status": 0, "out": ""}))
    assert done.returncode == 0 and bench.did("docker", "push")


@pytest.mark.parametrize("change, says, pushed", [
    ({"repository": {"status": 255, "out": ""}}, "the eval repository could not be read", False),
    ({"repository": {"out": f"111111111111.dkr.ecr.{REGION}.amazonaws.com/discharge-eval\n"}},
     "not where this script expects it", False),
    ({"password": {"status": 255, "out": ""}}, "the ECR login password could not be read", False),
    ({"login": {"status": 1}}, "the login to ECR failed", False),
    ({"tag": {"status": 1}}, "the image could not be tagged", False),
    ({"inspect_tag": {"out": OTHER_IMAGE + "\n"}}, "the tag does not name the image the checks ran on",
     False),
    ({"push": {"status": 1}}, "the push failed", True),
    ({"pushed": {"out": OTHER_IMAGE + "\n"}}, "DO NOT DEPLOY IT", True),
    ({"pushed": {"status": 255, "out": ""}}, "DO NOT DEPLOY IT", True),
    ({"pushed": {"out": "None\n"}}, "DO NOT DEPLOY IT", True)])
def test_what_is_pushed_is_the_image_the_checks_ran_on_or_no_digest_is_given(bench, change, says,
                                                                             pushed):
    """After the checks: a tag that no longer names the checked image is not pushed, and
    a digest ECR reports that is not the checked image's ID is never printed for a deploy."""
    c = make_remote(bench)["tip"]
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c, **change))
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert "PUSHED" not in done.stdout and "digest sha256" not in done.stdout
    assert bool(bench.did("docker", "push")) is pushed
    assert list(bench.work.iterdir()) == []


@pytest.mark.parametrize("env, change, argv_account, says", [
    ({}, {"sts_account": {"out": "999999999999\n"}}, ACCOUNT, "another account"),
    ({}, {"sts_account": {"status": 255, "out": ""}}, ACCOUNT, "identity could not be read"),
    ({}, {"sts_account": {"out": ""}}, ACCOUNT, "identity could not be read"),
    ({"AWS_REGION": "us-east-1"}, {}, ACCOUNT, "set for region us-east-1"),
    ({"AWS_DEFAULT_REGION": "eu-west-1"}, {}, ACCOUNT, "set for region eu-west-1"),
    ({"AWS_ENDPOINT_URL": "http://127.0.0.1:9"}, {}, ACCOUNT, "AWS_ENDPOINT_URL is set"),
    ({}, {}, "12345", "12-digit AWS account"),
    ({}, {}, "", "12-digit AWS account")])
def test_the_identity_and_region_check_comes_before_everything(bench, env, change, argv_account, says):
    c = make_remote(bench)["tip"]
    done = bench.run("push-image.sh", ["--account", argv_account, "--commit", c],
                     push_rules(c, **change), env=env)
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("git") and not bench.did("docker") and not bench.did("aws", "ecr")
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c),
                     env={"AWS_REGION": REGION})                       # the partner
    assert done.returncode == 0


def test_check_only_builds_and_checks_and_makes_no_aws_call(bench):
    c = make_remote(bench)["tip"]
    done = bench.run("push-image.sh", ["--check-only", "--commit", c], push_rules(c))
    assert done.returncode == 0, done.stderr
    assert bench.names()[-1] == "check5" and not bench.did("aws")
    nothing_reached_the_registry(bench)
    assert done.stdout.splitlines()[-1] == \
        f"check-only: built and checked {IMAGE} from commit {c}. Nothing was pushed."
    done = bench.run("push-image.sh", ["--check-only", "--account", ACCOUNT, "--commit", c], push_rules(c))
    assert done.returncode == 1 and not bench.calls


def test_the_temporary_folder_is_removed_when_the_script_is_killed(bench):
    c = make_remote(bench)["tip"]
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c],
                     push_rules(c, build={"do": "sigterm"}))
    assert done.returncode == 143 and bench.did("docker", "build")
    assert not bench.did("docker", "push") and list(bench.work.iterdir()) == []


# ============================================================== the deploy ==

def cfn(*args: str) -> list:
    return ["cloudformation", "--region", REGION, *args]


GUARD_RULES = [r for r in push_rules("0" * 40) if r["name"] in ("sts_account", "sts_arn")]
NO_STACK = ("\nAn error occurred (ValidationError) when calling the DescribeStacks operation: "
            "Stack with id discharge-eval does not exist\n")
SET = ["--stack-name", "discharge-eval", "--change-set-name", ANY]
STATE_QUERY = "Parameters[?ParameterKey=='ScheduleState'].ParameterValue | [0]"
DETAIL_QUERY = ("length(Changes[?ResourceChange.ResourceType=='AWS::Scheduler::Schedule']"
                ".ResourceChange.Details[] | [?Target.Name=='State'])")


def deploy_rules(exists: bool, state: str = "DISABLED", /, **change) -> list:
    rules = GUARD_RULES + [
        rule("exists", "aws", cfn("describe-stacks", "--stack-name", "discharge-eval", "--query",
                                  "Stacks[0].StackStatus", "--output", "text"),
             "UPDATE_COMPLETE" if exists else "", 0 if exists else 254,
             err="" if exists else NO_STACK),
        rule("state_now", "aws", cfn("describe-stacks", "--stack-name", "discharge-eval", "--query",
                                     "Stacks[0]." + STATE_QUERY, "--output", "text"), state),
        rule("image", "aws", ["ecr", "describe-images", "--region", REGION, "--registry-id", ACCOUNT,
                              "--repository-name", "discharge-eval", "--image-ids",
                              f"imageDigest={IMAGE}", "--query", "imageDetails[0].imageDigest",
                              "--output", "text"], IMAGE),
        rule("create_set", "aws", cfn("create-change-set", REST)),
        rule("wait_set", "aws", cfn("wait", "change-set-create-complete", *SET)),
        rule("table", "aws", cfn("describe-change-set", *SET, "--query",
                                 "Changes[].ResourceChange.[Action,LogicalResourceId,ResourceType,Replacement]",
                                 "--output", "table"), "| Add | Bucket |"),
        rule("set_state", "aws", cfn("describe-change-set", *SET, "--query", STATE_QUERY, "--output",
                                     "text"), state),
        rule("set_details", "aws", cfn("describe-change-set", *SET, "--query", DETAIL_QUERY,
                                       "--output", "text"), "0"),
        rule("reason", "aws", cfn("describe-change-set", *SET, "--query", "StatusReason", "--output",
                                  "text"), "invented reason"),
        rule("discard", "aws", cfn("delete-change-set", *SET)),
        rule("execute", "aws", cfn("execute-change-set", *SET)),
        rule("wait_create", "aws", cfn("wait", "stack-create-complete", "--stack-name", "discharge-eval")),
        rule("wait_update", "aws", cfn("wait", "stack-update-complete", "--stack-name", "discharge-eval")),
        rule("protect", "aws", cfn("update-termination-protection", "--stack-name", "discharge-eval",
                                   "--enable-termination-protection")),
        rule("outputs", "aws", cfn("describe-stacks", "--stack-name", "discharge-eval", "--query",
                                   "Stacks[0].Outputs[].[OutputKey,OutputValue]", "--output", "table")),
    ]
    assert set(change) <= {r["name"] for r in rules}, change
    for r in rules:
        r.update(change.get(r["name"], {}))
    return rules


def parameters(bench: Bench) -> dict:
    """The parameters of the one change set a script asked for, key to what was passed."""
    made = bench.did("aws", *cfn("create-change-set"))
    assert len(made) == 1
    args = made[0]["args"]
    given = args[args.index("--parameters") + 1:]
    out = {}
    for item in given:
        key, _, rest = item.partition(",")
        out[key.removeprefix("ParameterKey=")] = rest
    return out


EMAIL = {"WS7_ALERT_EMAIL": "invented@example.invalid"}
DEPLOY = ["--account", ACCOUNT, "--target", "v1"]


def test_the_first_deploy_creates_the_stack_with_no_image_and_the_schedules_disabled(bench):
    done = bench.run("deploy.sh", DEPLOY, deploy_rules(False), stdin="discharge-eval\n", env=EMAIL)
    assert done.returncode == 0, done.stderr
    assert parameters(bench) == {"Target": "ParameterValue=v1", "ImageDigest": "ParameterValue=",
                                 "ScheduleState": "ParameterValue=DISABLED",
                                 "AlertEmail": "ParameterValue=invented@example.invalid"}
    made = bench.did("aws", *cfn("create-change-set"))[0]["args"]
    assert made[made.index("--change-set-type") + 1] == "CREATE"
    assert made[made.index("--template-body") + 1] == \
        f"file://{bench.repo}/infra/eval/../eval-template.yaml"
    assert "--template-url" not in made and "--capabilities" in made
    assert bench.names()[-5:] == ["set_details", "execute", "wait_create", "protect", "outputs"]
    assert "invented@example.invalid" not in done.stdout + done.stderr


@pytest.mark.parametrize("state", ["DISABLED", "ENABLED"])
def test_an_update_hands_the_schedules_state_back_unchanged(bench, state):
    """Otherwise every image release after slice 6 would switch both schedules off."""
    done = bench.run("deploy.sh", DEPLOY + ["--image-digest", IMAGE], deploy_rules(True, state),
                     stdin="discharge-eval\n")
    assert done.returncode == 0, done.stderr
    assert parameters(bench) == {"Target": "ParameterValue=v1", "ScheduleState": "UsePreviousValue=true",
                                 "ImageDigest": f"ParameterValue={IMAGE}",
                                 "AlertEmail": "UsePreviousValue=true"}
    assert bench.names()[-4:] == ["execute", "wait_update", "protect", "outputs"]
    # (The prompt has no line end, so the last line starts with it.)
    assert done.stdout.endswith(f"deploy: done. The schedules are {state}.\n")


def test_deploy_never_names_enabled():
    text = (SCRIPTS / "deploy.sh").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "ParameterKey=ScheduleState,ParameterValue=DISABLED" in code
    assert "ParameterKey=ScheduleState,UsePreviousValue=true" in code
    assert code.count("ParameterKey=ScheduleState") == 2 and "ParameterValue=ENABLED" not in code


@pytest.mark.parametrize("exists, state, change, says", [
    (True, "ENABLED", {"set_state": {"out": "DISABLED\n"}}, "would set ScheduleState to DISABLED"),
    (True, "DISABLED", {"set_state": {"out": "ENABLED\n"}}, "would set ScheduleState to ENABLED"),
    (False, "DISABLED", {"set_state": {"out": "ENABLED\n"}}, "would set ScheduleState to ENABLED"),
    (True, "ENABLED", {"set_details": {"out": "1\n"}}, "would change a schedule's State"),
    (True, "ENABLED", {"set_details": {"out": "2\n"}}, "would change a schedule's State"),
    (True, "ENABLED", {"set_state": {"status": 255, "out": ""}}, "could not be read"),
    (True, "ENABLED", {"set_details": {"status": 255, "out": ""}}, "could not be read"),
    (True, "ENABLED", {"set_details": {"out": ""}}, "could not be read"),
    (True, "ENABLED", {"table": {"status": 255}}, "could not be read"),
    (True, "ENABLED", {"wait_set": {"status": 255}}, "the change set was not created")])
def test_a_change_set_that_would_change_a_schedules_state_is_never_executed(bench, exists, state,
                                                                            change, says):
    done = bench.run("deploy.sh", DEPLOY, deploy_rules(exists, state, **change),
                     stdin="discharge-eval\n", env=EMAIL)
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("aws", *cfn("execute-change-set"))
    assert bench.did("aws", *cfn("delete-change-set"))                 # and it is not left behind


@pytest.mark.parametrize("typed", ["", "yes\n", "discharge-eval \n", "DISCHARGE-EVAL\n", "discharge-audit\n"])
def test_a_change_set_is_executed_only_when_the_stack_name_is_typed(bench, typed):
    done = bench.run("deploy.sh", DEPLOY, deploy_rules(True), stdin=typed)
    assert done.returncode == 1 and "not confirmed" in done.stderr
    assert not bench.did("aws", *cfn("execute-change-set")) and bench.did("aws", *cfn("delete-change-set"))


@pytest.mark.parametrize("argv, env, change, says", [
    (["--account", ACCOUNT, "--target", "main"], EMAIL, {}, "--target must be v1 or pipeline"),
    (["--account", ACCOUNT], EMAIL, {}, "--target must be v1 or pipeline"),
    (DEPLOY + ["--image-digest", "latest"], EMAIL, {}, "--image-digest must be sha256:"),
    (DEPLOY + ["--image-digest", "sha256:abc"], EMAIL, {}, "--image-digest must be sha256:"),
    (DEPLOY + ["--schedule-state", "ENABLED"], EMAIL, {}, "usage: deploy.sh"),
    (DEPLOY, {}, {"exists": {"status": 254, "err": NO_STACK, "out": ""}}, "set WS7_ALERT_EMAIL"),
    (DEPLOY, EMAIL, {"exists": {"status": 254, "err": "AccessDenied\n", "out": ""}},
     "the stack's state could not be read"),
    (DEPLOY, EMAIL, {"state_now": {"out": "None\n"}}, "neither DISABLED nor ENABLED"),
    (DEPLOY, EMAIL, {"state_now": {"status": 255, "out": ""}}, "ScheduleState could not be read"),
    (DEPLOY + ["--image-digest", IMAGE], EMAIL, {"image": {"status": 254, "out": ""}},
     "not an image in the eval repository"),
    (DEPLOY + ["--image-digest", IMAGE], EMAIL, {"image": {"out": OTHER_IMAGE + "\n"}},
     "not an image in the eval repository")])
def test_deploy_stops_before_any_change_set_when_it_is_given_or_finds_something_wrong(
        bench, argv, env, change, says):
    done = bench.run("deploy.sh", argv, deploy_rules(True, **change), stdin="discharge-eval\n", env=env)
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("aws", *cfn("create-change-set"))


def test_a_template_over_the_inline_limit_stops_the_deploy_and_no_bucket_is_chosen(bench):
    template = bench.repo / "infra" / "eval-template.yaml"
    body = template.read_bytes()
    template.write_bytes(body + b"#" * (51_200 - len(body)))            # exactly at the limit
    done = bench.run("deploy.sh", DEPLOY, deploy_rules(True), stdin="discharge-eval\n")
    assert done.returncode == 0, done.stderr
    template.write_bytes(body + b"#" * (51_201 - len(body)))            # one byte over
    done = bench.run("deploy.sh", DEPLOY, deploy_rules(True), stdin="discharge-eval\n")
    assert done.returncode == 1 and "is 51201 bytes" in done.stderr and "no bucket is chosen" in done.stderr
    assert bench.calls == []                                            # before the guard, even
    text = (SCRIPTS / "deploy.sh").read_text()
    assert "--template-url" not in text and "s3://" not in text and " s3 " not in text


# =========================================================== the schedules ==

ALARMS = ["cloudwatch", "describe-alarms", "--region", REGION, "--alarm-name-prefix", "discharge-eval-",
          "--alarm-types", "MetricAlarm", "--query"]
ONLY_SCHEDULES = ("length(Changes[?ResourceChange.ResourceType=='AWS::Scheduler::Schedule' && "
                  "ResourceChange.Action=='Modify' && (ResourceChange.LogicalResourceId=='RunSchedule' "
                  "|| ResourceChange.LogicalResourceId=='CheckSchedule')])")


def schedule_rules(now: str, after: str, /, **change) -> list:
    def stack(query: str) -> list:
        return cfn("describe-stacks", "--stack-name", "discharge-eval", "--query", query, "--output", "text")

    def state(name: str) -> list:
        return ["scheduler", "get-schedule", "--region", REGION, "--group-name", "discharge-eval",
                "--name", name, "--query", "State", "--output", "text"]
    rules = GUARD_RULES + [
        rule("state_now", "aws", stack("Stacks[0]." + STATE_QUERY), now),
        rule("digest", "aws", stack("Stacks[0].Parameters[?ParameterKey=='ImageDigest'].ParameterValue | [0]"),
             IMAGE),
        rule("topic", "aws", stack("Stacks[0].Outputs[?OutputKey=='AlertsTopicArn'].OutputValue | [0]"),
             TOPIC),
        rule("alarms_total", "aws", ALARMS + ["length(MetricAlarms)", "--output", "text"], "26"),
        rule("alarms_armed", "aws", ALARMS + ["length(MetricAlarms[?ActionsEnabled==`true`])",
                                              "--output", "text"], "26"),
        rule("alarms_wired", "aws", ALARMS + [
            f"length(MetricAlarms[?length(AlarmActions)==`1` && AlarmActions[0]=='{TOPIC}'])",
            "--output", "text"], "26"),
        rule("subscribed", "aws", ["sns", "list-subscriptions-by-topic", "--region", REGION,
                                   "--topic-arn", TOPIC, "--query",
                                   "length(Subscriptions[?Protocol=='email' && "
                                   "starts_with(SubscriptionArn, 'arn:aws:sns:')])", "--output", "text"], "1"),
        rule("create_set", "aws", cfn("create-change-set", REST)),
        rule("wait_set", "aws", cfn("wait", "change-set-create-complete", *SET)),
        rule("set_all", "aws", cfn("describe-change-set", *SET, "--query", "length(Changes)",
                                   "--output", "text"), "2"),
        rule("set_ours", "aws", cfn("describe-change-set", *SET, "--query", ONLY_SCHEDULES,
                                    "--output", "text"), "2"),
        rule("discard", "aws", cfn("delete-change-set", *SET)),
        rule("execute", "aws", cfn("execute-change-set", *SET)),
        rule("wait_update", "aws", cfn("wait", "stack-update-complete", "--stack-name", "discharge-eval")),
        rule("run_state", "aws", state("discharge-eval-ws7-run"), after),
        rule("check_state", "aws", state("discharge-eval-ws7-check"), after),
    ]
    assert set(change) <= {r["name"] for r in rules}, change
    for r in rules:
        r.update(change.get(r["name"], {}))
    return rules


ON, OFF = ["--account", ACCOUNT, "on"], ["--account", ACCOUNT, "off"]
ALARM_CHECKS = ["topic", "alarms_total", "alarms_armed", "alarms_wired", "subscribed"]


def test_switching_on_changes_one_parameter_after_every_alarm_is_shown_to_be_armed(bench):
    done = bench.run("schedules.sh", ON, schedule_rules("DISABLED", "ENABLED"), stdin="ENABLE\n")
    assert done.returncode == 0, done.stderr
    assert bench.names() == ["sts_account", "sts_arn", "state_now", "digest", *ALARM_CHECKS,
                             "create_set", "wait_set", "set_all", "set_ours", "execute", "wait_update",
                             "run_state", "check_state"]
    assert parameters(bench) == {"Target": "UsePreviousValue=true", "ImageDigest": "UsePreviousValue=true",
                                 "AlertEmail": "UsePreviousValue=true",
                                 "ScheduleState": "ParameterValue=ENABLED"}
    made = bench.did("aws", *cfn("create-change-set"))[0]["args"]
    assert "--use-previous-template" in made and "--template-body" not in made
    assert done.stdout.splitlines()[-1] == "schedules: done. Both are ENABLED."


@pytest.mark.parametrize("change, says", [
    ({"alarms_total": {"out": "25\n"}}, "the stack should have 26 alarms; 25 were found"),
    ({"alarms_total": {"out": "27\n"}}, "the stack should have 26 alarms; 27 were found"),
    ({"alarms_total": {"status": 255, "out": ""}}, "the alarms could not be read"),
    ({"alarms_armed": {"out": "25\n"}}, "only 25 of 26 alarms have their actions on"),
    ({"alarms_armed": {"out": "0\n"}}, "only 0 of 26 alarms have their actions on"),
    ({"alarms_armed": {"out": ""}}, "the alarms could not be read"),
    ({"alarms_wired": {"out": "25\n"}}, "only 25 of 26 alarms have the eval topic as their one action"),
    ({"alarms_wired": {"status": 255, "out": ""}}, "the alarms could not be read"),
    ({"subscribed": {"out": "0\n"}}, "no email subscription to the eval topic is confirmed"),
    ({"subscribed": {"out": "None\n"}}, "no email subscription to the eval topic is confirmed"),
    ({"subscribed": {"status": 255, "out": ""}}, "subscriptions could not be read"),
    ({"topic": {"out": f"arn:aws:sns:{REGION}:{ACCOUNT}:discharge-audit-alerts\n"}},
     "not the one this script expects"),
    ({"topic": {"status": 255, "out": ""}}, "the eval topic's ARN could not be read"),
    ({"digest": {"out": "\n"}}, "the stack has no image yet"),
    ({"digest": {"out": "None\n"}}, "the stack has no image yet")])
def test_the_schedules_are_not_switched_on_while_any_alarm_is_silent_or_nobody_is_listening(
        bench, change, says):
    """Ruling 3e. Every alarm's actions are on, each alarm's one action is the eval topic,
    and an email subscription is confirmed, or no change set is even made."""
    done = bench.run("schedules.sh", ON, schedule_rules("DISABLED", "ENABLED", **change), stdin="ENABLE\n")
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("aws", *cfn("create-change-set")) and not bench.did("aws", *cfn("execute-change-set"))


@pytest.mark.parametrize("action, typed, change, says", [
    (ON, "ENABLE\n", {"set_all": {"out": "3\n"}}, "not the two schedules and nothing else"),
    (ON, "ENABLE\n", {"set_ours": {"out": "1\n"}}, "not the two schedules and nothing else"),
    (ON, "ENABLE\n", {"set_all": {"out": "1\n"}, "set_ours": {"out": "1\n"}},
     "not the two schedules and nothing else"),
    (ON, "ENABLE\n", {"set_all": {"status": 255, "out": ""}}, "the change set could not be read"),
    (ON, "ENABLE\n", {"set_ours": {"out": ""}}, "the change set could not be read"),
    (ON, "ENABLE\n", {"wait_set": {"status": 255}}, "the change set was not created"),
    (ON, "enable\n", {}, "not confirmed"), (ON, "\n", {}, "not confirmed"), (ON, "", {}, "not confirmed"),
    (ON, "DISABLE\n", {}, "not confirmed"), (OFF, "ENABLE\n", {}, "not confirmed")])
def test_a_schedule_change_set_is_executed_only_if_it_is_the_two_schedules_and_confirmed(
        bench, action, typed, change, says):
    now, after = ("DISABLED", "ENABLED") if action is ON else ("ENABLED", "DISABLED")
    done = bench.run("schedules.sh", action, schedule_rules(now, after, **change), stdin=typed)
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("aws", *cfn("execute-change-set")) and bench.did("aws", *cfn("delete-change-set"))


def test_switching_off_needs_no_alarm_check_and_is_confirmed_by_what_the_schedules_say(bench):
    done = bench.run("schedules.sh", OFF, schedule_rules("ENABLED", "DISABLED"), stdin="DISABLE\n")
    assert done.returncode == 0, done.stderr
    assert not set(bench.names()) & set(ALARM_CHECKS)
    assert parameters(bench)["ScheduleState"] == "ParameterValue=DISABLED"
    done = bench.run("schedules.sh", OFF, schedule_rules("ENABLED", "ENABLED"), stdin="DISABLE\n")
    assert done.returncode == 1 and "is ENABLED, not DISABLED" in done.stderr     # believed only if seen


@pytest.mark.parametrize("argv, now", [(["--account", ACCOUNT, "status"], "DISABLED"),
                                       (ON, "ENABLED"), (OFF, "DISABLED")])
def test_status_and_a_switch_to_the_state_it_is_in_change_nothing(bench, argv, now):
    done = bench.run("schedules.sh", argv, schedule_rules(now, now))
    assert done.returncode == 0, done.stderr
    assert bench.names() == ["sts_account", "sts_arn", "state_now", "digest", "run_state", "check_state"]
    assert f"schedule discharge-eval-ws7-run is {now}" in done.stdout


def test_only_schedules_sh_can_set_the_schedules_state():
    setters = [s.name for s in sorted(SCRIPTS.glob("*.sh"))
               if "ParameterKey=ScheduleState,ParameterValue=$WANT" in s.read_text()]
    assert setters == ["schedules.sh"]
    for script in SCRIPTS.glob("*.sh"):
        text = script.read_text()
        assert "update-schedule" not in text and "create-schedule" not in text, script.name
        assert "disable-alarm-actions" not in text and "set-alarm-state" not in text, script.name
