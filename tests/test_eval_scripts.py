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
the GitHub URL is given the test's own scratch repository instead, fetched as a
remote is (`--no-local`: only what the branch reaches), and a clone of anything else
is a failed test. Every repository, commit, account and digest here
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
if tool == "aws":
    entry["ignores_endpoints"] = os.environ.get("AWS_IGNORE_CONFIGURED_ENDPOINT_URLS")
if tool == "git":
    entry["environment"] = sorted(os.environ)
def note():
    with open({log!r}, "a") as log:
        log.write(json.dumps(entry) + "\\n")
if tool == "git":
    if "clone" in args:
        if args[-2] != {github!r}:
            entry["unexpected"] = True
            note()
            sys.exit(97)
        note()
        # The scratch remote in GitHub's place, fetched as a remote is: only what the
        # branch reaches, never a copy of every object in the folder.
        args[-2] = {remote!r}
        args.insert(args.index("clone") + 1, "--no-local")
        os.execv({git!r}, [{git!r}] + args)
    note()
    os.execv({git!r}, [{git!r}] + args)
with open({plan!r}) as plan:
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
        self.log, self.plan = tmp_path / "calls.jsonl", tmp_path / "plan.json"
        self.remote = tmp_path / "remote.git"
        # The stubs are told where their log, plan and remote are when they are written,
        # not through the environment: the script runs git with an environment of its own.
        for name in ("docker", "aws", "git"):
            stub = self.bin / name
            stub.write_text(STUB.format(python=sys.executable, github=GITHUB, git=REAL_GIT,
                                        log=str(self.log), plan=str(self.plan),
                                        remote=str(self.remote)))
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.work = tmp_path / "tmp"
        self.work.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        self.empty = [tmp_path / "aws-config", tmp_path / "aws-credentials"]
        for path in self.empty:
            path.write_bytes(b"")
        self.socket = tmp_path / "no-such-docker.sock"
        # Built from nothing: nothing of the developer's or the runner's own environment.
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(self.work),
                    "LC_ALL": "C", "AWS_CONFIG_FILE": str(self.empty[0]),
                    "AWS_SHARED_CREDENTIALS_FILE": str(self.empty[1]),
                    "DOCKER_HOST": f"unix://{self.socket}"}

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

    def left_behind(self) -> list:
        """The push script's work folders still in TMPDIR. Other files there are not
        the script's: on a Mac /usr/bin/git itself leaves an `xcrun_db` behind."""
        return sorted(path.name for path in self.work.glob("ws7-push.*"))


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
    """A scratch repository with the image's paths and the protected folders, tracked,
    and a history shaped like this repository's: a task branch of two commits, merged
    into the base branch with a merge commit. Returns:

    - `old` and `tip`: the base branch's first-parent chain. `tip` is the merge commit.
    - `merged_first` and `merged_head`: the task branch's own commits. Both are
      ancestors of the base branch, and neither is on its first-parent chain.
    - `side`: a commit on a branch that was never merged.
    - `tree`: the tip's tree (40 hex that is not a commit)."""
    work = bench.tmp / "remote-work"
    work.mkdir()
    git(work, "init", "--quiet", "--initial-branch", BRANCH)
    for name, body in FILES.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(body)
    git(work, "add", "--all")
    git(work, "commit", "--quiet", "-m", "first")
    old = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "--quiet", "-b", "task/merged")
    (work / "prompts" / "p.md").write_text("invented, a first try that review faulted\n")
    git(work, "commit", "--quiet", "-am", "a head that review found faults in")
    merged_first = git(work, "rev-parse", "HEAD")
    (work / "prompts" / "p.md").write_text("invented\n")
    (work / "src" / "app.py").write_text("x = 2\n")
    if change:
        change(work)
    git(work, "add", "--all")
    git(work, "commit", "--quiet", "-m", "second")
    merged_head = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "--quiet", BRANCH)
    git(work, "merge", "--quiet", "--no-ff", "-m", "Merge pull request (invented)", "task/merged")
    tip = git(work, "rev-parse", "HEAD")
    assert git(work, "rev-list", "--parents", "-n", "1", tip).split() == [tip, old, merged_head]
    git(work, "checkout", "--quiet", "-b", "task/unmerged")
    (work / "src" / "app.py").write_text("x = 3\n")
    git(work, "commit", "--quiet", "-am", "unreviewed")
    side = git(work, "rev-parse", "HEAD")
    git(work, "clone", "--quiet", "--bare", str(work), str(bench.remote))
    return {"old": old, "tip": tip, "merged_first": merged_first, "merged_head": merged_head,
            "side": side, "tree": git(work, "rev-parse", f"{tip}^{{tree}}")}


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
    assert not any(name.startswith(("DOCKER_CONFIG", "GIT_", "WS7_")) for name in bench.env)
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
        # The one PATH the scripts write is git's, and it is the caller's own, unchanged.
        assert code.count("PATH=") == code.count('PATH="$PATH"') == (1 if script.name == "push-image.sh" else 0)
        code = code.replace('PATH="$PATH"', "")
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
    assert bench.left_behind() == []                            # the temporary folder is gone


def test_the_commit_comes_from_github_with_this_machines_git_configuration_left_out(bench):
    """Two ways this machine could send the clone somewhere else, each tried alone: a
    global git configuration, and configuration passed in the environment. Each one
    rewrites the clone's source to a folder that does not exist. With the script's
    isolation neither is read, and the clone is the scratch remote's."""
    c = make_remote(bench)["tip"]
    elsewhere = f"[url \"{bench.tmp}/nowhere/\"]\n\tinsteadOf = {bench.remote}\n"
    (Path(bench.env["HOME"]) / ".gitconfig").write_text(elsewhere + "[core]\n\tautocrlf = true\n")
    by_environment = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{bench.tmp}/nowhere/.insteadOf",
                      "GIT_CONFIG_VALUE_0": str(bench.remote)}
    # The control: real git, without the script's isolation, does follow each of them.
    for env in ({"HOME": bench.env["HOME"]}, by_environment):
        moved = subprocess.run([REAL_GIT, "ls-remote", str(bench.remote)], capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin", "HOME": str(bench.tmp / "nohome"),
                                    "GIT_CONFIG_NOSYSTEM": "1", **env})
        assert moved.returncode != 0 and "nowhere" in moved.stderr, moved.stderr
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c),
                     env={"GIT_DIR": str(ROOT / ".git"), "GIT_WORK_TREE": str(ROOT), **by_environment})
    assert done.returncode == 0, done.stderr
    gits = bench.did("git")
    clone = gits[0]["args"]
    assert clone == ["clone", "--quiet", "--bare", "--single-branch", "--branch", BRANCH,
                     GITHUB, clone[-1]]                                # what the script asked git for
    assert clone[-1].startswith(str(bench.work))
    # Every later git call names the clone; none can read the local checkout.
    assert len(gits) > 5 and all(g["args"][0] == f"--git-dir={clone[-1]}" for g in gits[1:])
    script = (SCRIPTS / "push-image.sh").read_text()
    assert f"REMOTE={GITHUB}\n" in script and f"BRANCH={BRANCH}\n" in script
    assert "GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1" in script
    assert bench.did("docker", "build")[0]["context"] == IMAGE_FILES


GIT_ALLOWED = {"GIT_ATTR_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT",
               "HOME", "LC_ALL", "PATH", "TMPDIR"}
# Not inherited: macOS gives the first to every process, and the stub's own Python
# sets the second when it starts in the C locale.
ADDED_BY_THE_SYSTEM = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}


def _graft(bench, made) -> dict:
    """A graft file that swaps the merge's parents, so the task branch's head is the
    merge's FIRST parent. Returns the environment that names it."""
    grafts = bench.tmp / "grafts"
    grafts.write_text(f"{made['tip']} {made['merged_head']} {made['old']}\n")
    return {"GIT_GRAFT_FILE": str(grafts)}


INHERITED = {
    "GIT_GRAFT_FILE": _graft,
    "GIT_REPLACE_REF_BASE": lambda bench, made: {"GIT_REPLACE_REF_BASE": "refs/heads/"},
    "GIT_DIR": lambda bench, made: {"GIT_DIR": str(ROOT / ".git"), "GIT_WORK_TREE": str(ROOT)},
    "GIT_OBJECT_DIRECTORY": lambda bench, made: {
        "GIT_OBJECT_DIRECTORY": str(bench.tmp / "remote-work" / ".git" / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(bench.tmp / "remote-work" / ".git" / "objects")},
    "GIT_NAMESPACE and others": lambda bench, made: {
        "GIT_NAMESPACE": "elsewhere", "GIT_INDEX_FILE": str(bench.tmp / "an-index"),
        "GIT_CEILING_DIRECTORIES": "/", "GIT_EXEC_PATH": str(bench.tmp / "nowhere"),
        "GIT_SSH_COMMAND": "false", "GIT_TRACE": "1", "GIT_SHALLOW_FILE": str(bench.tmp / "shallow")},
}


def test_the_control_a_graft_in_the_environment_does_put_a_side_branch_commit_on_the_chain(bench):
    """Why the next test matters. With GIT_GRAFT_FILE inherited, plain git lists the task
    branch's head on the base branch's first-parent chain. Without it, it does not."""
    made = make_remote(bench)

    def chain(env: dict) -> list:
        done = subprocess.run([REAL_GIT, f"--git-dir={bench.remote}", "rev-list", "--first-parent", BRANCH],
                              capture_output=True, text=True, env={**GIT_ENV, **env})
        assert done.returncode == 0, done.stderr
        return done.stdout.split()
    assert made["merged_head"] not in chain({})
    assert made["merged_head"] in chain(_graft(bench, made))


@pytest.mark.parametrize("inherited", list(INHERITED))
def test_nothing_inherited_from_the_environment_changes_what_git_reads(bench, inherited):
    """The author's ruling H1 (26 Nov 2026). The class: anything in the caller's
    environment that changes what git reads. Every git command runs under `env -i`
    with a fixed list of variables, so none of these reaches git: the task branch's
    head is still refused, and the merge commit is still built from the clone."""
    made = make_remote(bench)
    env = INHERITED[inherited](bench, made)
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", made["merged_head"]],
                     push_rules(made["merged_head"]), env=env)
    assert done.returncode == 1 and NOT_ON_THE_CHAIN in done.stderr, done.stderr
    assert not bench.did("docker") and bench.left_behind() == []
    # What every git call was given, by name: the allowed list and nothing else.
    gits = bench.did("git")
    assert len(gits) >= 4
    for call in gits:
        assert set(call["environment"]) - ADDED_BY_THE_SYSTEM == GIT_ALLOWED, call["environment"]

    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", made["tip"]],
                     push_rules(made["tip"]), env=env)                 # the partner
    assert done.returncode == 0, done.stderr
    assert bench.did("docker", "build")[0]["context"] == IMAGE_FILES


def test_gits_home_and_temporary_folder_are_inside_the_work_folder_and_go_with_it(bench):
    c = make_remote(bench)["tip"]
    script = (SCRIPTS / "push-image.sh").read_text()
    assert 'env -i PATH="$PATH" HOME="$WORK/home" TMPDIR="$WORK/tmp" LC_ALL=C' in script
    code = [line for line in script.splitlines()
            if not line.lstrip().startswith("#") and re.search(r"\bgit ", line)]
    assert code == ['    git "$@"']                    # the one place git is called: inside g()
    assert "unset GIT_" not in script
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c))
    assert done.returncode == 0 and bench.left_behind() == []


NOT_ON_THE_CHAIN = "is not on the first-parent chain of feat/agentic-pipeline as fetched"
NOT_IN_THE_CLONE = "is not a commit on feat/agentic-pipeline as fetched"


@pytest.mark.parametrize("which, says", [
    ("tip", None),                                   # the merge commit: the branch's head
    ("old", None),                                   # where the branch itself was before it
    # The task branch's own commits. Each is in the clone and is an ANCESTOR of the
    # branch, and is refused: it is not on the first-parent chain.
    ("merged_head", NOT_ON_THE_CHAIN),
    ("merged_first", NOT_ON_THE_CHAIN),
    # Commits that are not in the clone at all: a branch that was never merged, and a
    # SHA nothing has.
    ("side", NOT_IN_THE_CLONE),
    ("unknown", NOT_IN_THE_CLONE),
    ("tree", NOT_IN_THE_CLONE),                      # 40 hex that is not a commit
    ("short", "must be the full 40-character commit"),
    ("upper", "must be the full 40-character commit"),
    ("branch", "must be the full 40-character commit")])
def test_only_a_commit_on_the_base_branchs_first_parent_chain_is_built(bench, which, says):
    """An image is only ever of a reviewed, merged commit (the author's ruling, 26 Nov
    2026). A merge keeps every commit of the task branch in the history, and one of
    them can be a head that review found faults in. So the commit must be on the
    branch's first-parent chain in the fresh clone: the merge commit passes, and a
    commit from the merged side branch is refused, though it is an ancestor."""
    made = make_remote(bench)
    if which in ("merged_head", "merged_first"):     # the control: it IS an ancestor of the branch
        assert git(bench.remote, "merge-base", "--is-ancestor", made[which], made["tip"]) == ""
        assert made[which] not in git(bench.remote, "rev-list", "--first-parent", BRANCH).split()
    commit = {"short": made["tip"][:12], "upper": made["tip"].upper(), "branch": BRANCH,
              "unknown": "1" * 40}.get(which) or made[which]
    plan = push_rules(commit if len(commit) == 40 else made["tip"])
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", commit], plan)
    if says is None:
        assert done.returncode == 0 and bench.did("docker", "push")
        return
    assert done.returncode == 1 and says in done.stderr and "Nothing was pushed." in done.stderr
    assert not bench.did("docker") and not bench.did("aws", "ecr")
    assert bench.left_behind() == []


def test_the_chain_is_the_branchs_as_fetched_not_as_this_machine_last_knew_it(bench):
    """The branch on GitHub is one commit behind what was asked for (the merge has not
    landed there). The clone decides: the commit is refused, and the one the branch
    is at is built."""
    made = make_remote(bench)
    git(bench.remote, "update-ref", f"refs/heads/{BRANCH}", made["old"])   # the branch, one behind
    git(bench.remote, "tag", "reachable", made["tip"])
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", made["tip"]],
                     push_rules(made["tip"]))
    assert done.returncode == 1 and NOT_IN_THE_CLONE in done.stderr
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
    assert bench.left_behind() == []


def test_an_export_that_holds_a_file_the_commit_does_not_name_is_never_built(bench):
    """The other direction: every file of the commit is there and right, and there is one
    more. `tar` is wrapped for this test only, to leave an extra file behind."""
    c = make_remote(bench)["tip"]
    real_tar = shutil.which("tar")
    wrapper = bench.bin / "tar"
    wrapper.write_text(f"#!/bin/bash\n{real_tar} \"$@\" || exit 1\n"
                       "for last; do :; done\necho extra > \"$last/src/not_in_the_commit.py\"\n")
    wrapper.chmod(0o755)
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c))
    assert done.returncode == 1, done.stdout
    assert f"the export holds {len(IMAGE_FILES) + 1} files and the commit names {len(IMAGE_FILES)}" \
        in done.stderr
    assert not bench.did("docker") and bench.left_behind() == []
    wrapper.unlink()
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c))
    assert done.returncode == 0                                        # the partner


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
    assert bench.left_behind() == []


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
    assert bench.left_behind() == []


@pytest.mark.parametrize("env, change, argv_account, says", [
    ({}, {"sts_account": {"out": "999999999999\n"}}, ACCOUNT, "another account"),
    ({}, {"sts_account": {"status": 255, "out": ""}}, ACCOUNT, "identity could not be read"),
    ({}, {"sts_account": {"out": ""}}, ACCOUNT, "identity could not be read"),
    ({"AWS_REGION": "us-east-1"}, {}, ACCOUNT, "set for region us-east-1"),
    ({"AWS_DEFAULT_REGION": "eu-west-1"}, {}, ACCOUNT, "set for region eu-west-1"),
    ({"AWS_ENDPOINT_URL": "http://127.0.0.1:9"}, {}, ACCOUNT, "AWS_ENDPOINT_URL... is set"),
    ({"AWS_ENDPOINT_URL_STS": "http://127.0.0.1:9"}, {}, ACCOUNT, "AWS_ENDPOINT_URL... is set"),
    ({"AWS_ENDPOINT_URL_ECR": "http://127.0.0.1:9"}, {}, ACCOUNT, "AWS_ENDPOINT_URL... is set"),
    ({"AWS_ENDPOINT_URL_CLOUDFORMATION": "x"}, {}, ACCOUNT, "AWS_ENDPOINT_URL... is set"),
    ({"AWS_ENDPOINT_URL_S3": ""}, {}, ACCOUNT, "AWS_ENDPOINT_URL... is set"),
    ({}, {}, "12345", "12-digit AWS account"),
    ({}, {}, "", "12-digit AWS account")])
def test_the_identity_and_region_check_comes_before_everything(bench, env, change, argv_account, says):
    c = make_remote(bench)["tip"]
    done = bench.run("push-image.sh", ["--account", argv_account, "--commit", c],
                     push_rules(c, **change), env=env)
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("git") and not bench.did("docker") and not bench.did("aws", "ecr")
    if any(name.startswith("AWS_ENDPOINT_URL") for name in env):
        assert not bench.did("aws")                 # refused before STS is asked anything
    done = bench.run("push-image.sh", ["--account", ACCOUNT, "--commit", c], push_rules(c),
                     env={"AWS_REGION": REGION, "AWS_ENDPOINT": "not-a-name-the-guard-refuses"})
    assert done.returncode == 0                                        # the partner


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
    assert not bench.did("docker", "push") and bench.left_behind() == []


# ============================================================== the deploy ==

def cfn(*args: str) -> list:
    return ["cloudformation", "--region", REGION, *args]


GUARD_RULES = [r for r in push_rules("0" * 40) if r["name"] in ("sts_account", "sts_arn")]
NO_STACK = ("\nAn error occurred (ValidationError) when calling the DescribeStacks operation: "
            "Stack with id discharge-eval does not exist\n")
SET = ["--stack-name", "discharge-eval", "--change-set-name", ANY]
STATE_QUERY = "Parameters[?ParameterKey=='ScheduleState'].ParameterValue | [0]"
TARGET_QUERY = "Parameters[?ParameterKey=='Target'].ParameterValue | [0]"
DIGEST_QUERY = "Parameters[?ParameterKey=='ImageDigest'].ParameterValue | [0]"
DETAIL_QUERY = ("length(Changes[?ResourceChange.ResourceType=='AWS::Scheduler::Schedule']"
                ".ResourceChange.Details[] | [?Target.Name=='State'])")


def live_state(name: str) -> list:
    return ["scheduler", "get-schedule", "--region", REGION, "--group-name", "discharge-eval",
            "--name", name, "--query", "State", "--output", "text"]


def stack(query: str) -> list:
    return cfn("describe-stacks", "--stack-name", "discharge-eval", "--query", query, "--output", "text")


def deploy_rules(exists: bool, state: str = "DISABLED", target: str = "v1", digest: str = IMAGE,
                 /, **change) -> list:
    """What a deploy asks AWS. `state`, `target` and `digest` are the stack's own
    parameters as they stand; the live schedules are in `state` unless changed."""
    rules = GUARD_RULES + [
        rule("exists", "aws", stack("Stacks[0].StackStatus"),
             "UPDATE_COMPLETE" if exists else "", 0 if exists else 254,
             err="" if exists else NO_STACK),
        rule("state_now", "aws", stack("Stacks[0]." + STATE_QUERY), state),
        rule("target_now", "aws", stack("Stacks[0]." + TARGET_QUERY), target),
        rule("digest_now", "aws", stack("Stacks[0]." + DIGEST_QUERY), digest),
        rule("run_state", "aws", live_state("discharge-eval-ws7-run"), state),
        rule("check_state", "aws", live_state("discharge-eval-ws7-check"), state),
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
        rule("set_target", "aws", cfn("describe-change-set", *SET, "--query", TARGET_QUERY, "--output",
                                      "text"), target),
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
FIRST = ["--account", ACCOUNT, "--target", "v1"]           # the first deploy
LATER = ["--account", ACCOUNT]                              # every later one
YES = "discharge-eval\n"


def test_the_first_deploy_creates_the_stack_with_no_image_and_the_schedules_disabled(bench):
    done = bench.run("deploy.sh", FIRST, deploy_rules(False), stdin=YES, env=EMAIL)
    assert done.returncode == 0, done.stderr
    assert parameters(bench) == {"Target": "ParameterValue=v1", "ImageDigest": "ParameterValue=",
                                 "ScheduleState": "ParameterValue=DISABLED",
                                 "AlertEmail": "ParameterValue=invented@example.invalid"}
    made = bench.did("aws", *cfn("create-change-set"))[0]["args"]
    assert made[made.index("--change-set-type") + 1] == "CREATE"
    assert made[made.index("--template-body") + 1] == \
        f"file://{bench.repo}/infra/eval/../eval-template.yaml"
    assert "--template-url" not in made and "--capabilities" in made
    assert bench.names() == ["sts_account", "sts_arn", "exists", "create_set", "wait_set", "table",
                             "set_state", "set_details", "set_target", "execute", "wait_create",
                             "protect", "outputs"]
    assert "invented@example.invalid" not in done.stdout + done.stderr


@pytest.mark.parametrize("state", ["DISABLED", "ENABLED"])
@pytest.mark.parametrize("target", ["v1", "pipeline"])
def test_an_update_hands_the_schedules_state_and_the_target_back_unchanged(bench, state, target):
    """Otherwise every image release after slice 6 would switch both schedules off, and
    every one after W11 would switch the pipeline back to v1 (ruling H4, 26 Nov 2026)."""
    done = bench.run("deploy.sh", LATER + ["--image-digest", IMAGE],
                     deploy_rules(True, state, target), stdin=YES)
    assert done.returncode == 0, done.stderr
    assert parameters(bench) == {"Target": "UsePreviousValue=true",
                                 "ScheduleState": "UsePreviousValue=true",
                                 "ImageDigest": f"ParameterValue={IMAGE}",
                                 "AlertEmail": "UsePreviousValue=true"}
    assert bench.names() == ["sts_account", "sts_arn", "exists", "state_now", "target_now",
                             "digest_now", "run_state", "check_state", "image", "create_set",
                             "wait_set", "table", "set_state", "set_details", "set_target", "execute",
                             "wait_update", "protect", "outputs"]
    assert f"deploy: the target stays {target}\n" in done.stdout
    assert f"deploy: this change set's Target is {target}\n" in done.stdout
    # (The prompt has no line end, so the last line starts with it.)
    assert done.stdout.endswith(f"deploy: done. The target is {target}. The schedules are {state}.\n")


def test_deploy_never_names_enabled_and_names_a_target_only_when_told_to():
    text = (SCRIPTS / "deploy.sh").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "ParameterKey=ScheduleState,ParameterValue=DISABLED" in code
    assert "ParameterKey=ScheduleState,UsePreviousValue=true" in code
    assert code.count("ParameterKey=ScheduleState") == 2 and "ParameterValue=ENABLED" not in code
    assert sorted(re.findall(r"ParameterKey=Target,(\S+?)\"", code)) == \
        ["ParameterValue=$NEW_TARGET", "ParameterValue=$TARGET", "UsePreviousValue=true"]


def test_only_change_target_changes_the_target_and_the_script_says_what_changes(bench):
    """W11. The one way the target changes after the first deploy, and it is said before
    the confirmation. Naming the target the stack already has changes nothing."""
    done = bench.run("deploy.sh", LATER + ["--change-target", "pipeline"],
                     deploy_rules(True, "ENABLED", "v1", set_target={"out": "pipeline\n"}), stdin=YES)
    assert done.returncode == 0, done.stderr
    assert parameters(bench)["Target"] == "ParameterValue=pipeline"
    assert parameters(bench)["ScheduleState"] == "UsePreviousValue=true"
    assert "deploy: THE TARGET CHANGES from v1 to pipeline" in done.stdout
    assert done.stdout.index("THE TARGET CHANGES") < done.stdout.index("Type the stack name")
    assert done.stdout.endswith("deploy: done. The target is pipeline. The schedules are ENABLED.\n")

    done = bench.run("deploy.sh", LATER + ["--change-target", "pipeline"],
                     deploy_rules(True, "ENABLED", "pipeline"), stdin=YES)
    assert done.returncode == 0 and parameters(bench)["Target"] == "UsePreviousValue=true"
    assert "deploy: the target stays pipeline\n" in done.stdout


@pytest.mark.parametrize("argv, exists, target, change, says", [
    # --target is the first deploy's. On a stack that exists it is refused, whatever it names.
    (LATER + ["--target", "v1"], True, "pipeline", {}, "leave --target out"),
    (LATER + ["--target", "pipeline"], True, "pipeline", {}, "leave --target out"),
    (LATER + ["--target", "v1", "--image-digest", IMAGE], True, "pipeline", {}, "leave --target out"),
    # --change-target is a later deploy's.
    (LATER + ["--change-target", "pipeline"], False, "v1", {}, "takes --target, not --change-target"),
    (LATER, False, "v1", {}, "the first deploy needs --target"),
    (LATER + ["--target", "v1", "--change-target", "pipeline"], True, "v1", {}, "not both"),
    (LATER + ["--change-target", "main"], True, "v1", {}, "--change-target must be v1 or pipeline"),
    (LATER + ["--target", "main"], False, "v1", {}, "--target must be v1 or pipeline"),
    (LATER, True, "v1", {"target_now": {"out": "None\n"}}, "neither v1 nor pipeline"),
    (LATER, True, "v1", {"target_now": {"status": 255, "out": ""}}, "Target could not be read")])
def test_the_target_is_never_changed_by_an_argument_that_does_not_say_so(bench, argv, exists, target,
                                                                        change, says):
    done = bench.run("deploy.sh", argv, deploy_rules(exists, "ENABLED", target, **change), stdin=YES,
                     env=EMAIL)
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("aws", *cfn("create-change-set"))


@pytest.mark.parametrize("target, change", [
    ("pipeline", {"set_target": {"out": "v1\n"}}),             # the change set would move it
    ("v1", {"set_target": {"out": "pipeline\n"}}),
    ("pipeline", {"set_target": {"status": 255, "out": ""}}),
    ("pipeline", {"set_target": {"out": ""}})])
def test_a_change_set_whose_target_is_not_the_one_meant_is_never_executed(bench, target, change):
    done = bench.run("deploy.sh", LATER, deploy_rules(True, "ENABLED", target, **change), stdin=YES)
    assert done.returncode == 1, done.stdout
    assert "would set Target to" in done.stderr or "Target could not be read" in done.stderr
    assert not bench.did("aws", *cfn("execute-change-set")) and bench.did("aws", *cfn("delete-change-set"))


@pytest.mark.parametrize("state, change, says", [
    ("DISABLED", {"run_state": {"out": "ENABLED\n"}}, "discharge-eval-ws7-run is ENABLED"),
    ("DISABLED", {"check_state": {"out": "ENABLED\n"}}, "discharge-eval-ws7-check is ENABLED"),
    ("ENABLED", {"run_state": {"out": "DISABLED\n"}}, "discharge-eval-ws7-run is DISABLED"),
    ("ENABLED", {"check_state": {"out": "DISABLED\n"}}, "discharge-eval-ws7-check is DISABLED"),
    ("ENABLED", {"run_state": {"status": 255, "out": ""}}, "discharge-eval-ws7-run could not be read"),
    ("ENABLED", {"check_state": {"out": ""}}, "discharge-eval-ws7-check could not be read"),
    ("ENABLED", {"digest_now": {"status": 255, "out": ""}}, "ImageDigest could not be read"),
    ("ENABLED", {"digest_now": {"out": "latest\n"}}, "ImageDigest could not be read")])
def test_deploy_stops_while_a_live_schedule_is_not_in_the_state_the_stack_says(bench, state, change,
                                                                               says):
    """Ruling H3 (26 Nov 2026). The class: success reported while a live schedule's state
    differs from the stack's. CloudFormation cannot see a schedule changed outside the
    stack, so the script reads both live states itself, before any change set."""
    done = bench.run("deploy.sh", LATER + ["--image-digest", IMAGE],
                     deploy_rules(True, state, **change), stdin=YES)
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("aws", *cfn("create-change-set"))
    if "is ENABLED" in says or "is DISABLED" in says:
        assert "changed outside the stack" in done.stderr and "schedules.sh" in done.stderr


@pytest.mark.parametrize("digest", ["", "None"])
def test_a_stack_with_no_image_has_no_schedules_to_read(bench, digest):
    done = bench.run("deploy.sh", LATER + ["--image-digest", IMAGE],
                     deploy_rules(True, "DISABLED", "v1", digest), stdin=YES)
    assert done.returncode == 0, done.stderr
    assert "run_state" not in bench.names() and "check_state" not in bench.names()


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
    done = bench.run("deploy.sh", LATER if exists else FIRST, deploy_rules(exists, state, **change),
                     stdin=YES, env=EMAIL)
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("aws", *cfn("execute-change-set"))
    assert bench.did("aws", *cfn("delete-change-set"))                 # and it is not left behind


@pytest.mark.parametrize("typed", ["", "yes\n", "discharge-eval \n", "DISCHARGE-EVAL\n", "discharge-audit\n"])
def test_a_change_set_is_executed_only_when_the_stack_name_is_typed(bench, typed):
    done = bench.run("deploy.sh", LATER, deploy_rules(True), stdin=typed)
    assert done.returncode == 1 and "not confirmed" in done.stderr
    assert not bench.did("aws", *cfn("execute-change-set")) and bench.did("aws", *cfn("delete-change-set"))


@pytest.mark.parametrize("argv, env, change, says", [
    (LATER + ["--image-digest", "latest"], EMAIL, {}, "--image-digest must be sha256:"),
    (LATER + ["--image-digest", "sha256:abc"], EMAIL, {}, "--image-digest must be sha256:"),
    (LATER + ["--schedule-state", "ENABLED"], EMAIL, {}, "usage: deploy.sh"),
    (FIRST, {}, {"exists": {"status": 254, "err": NO_STACK, "out": ""}}, "set WS7_ALERT_EMAIL"),
    (LATER, EMAIL, {"exists": {"status": 254, "err": "AccessDenied\n", "out": ""}},
     "the stack's state could not be read"),
    (LATER, EMAIL, {"state_now": {"out": "None\n"}}, "neither DISABLED nor ENABLED"),
    (LATER, EMAIL, {"state_now": {"status": 255, "out": ""}}, "ScheduleState could not be read"),
    (LATER + ["--image-digest", IMAGE], EMAIL, {"image": {"status": 254, "out": ""}},
     "not an image in the eval repository"),
    (LATER + ["--image-digest", IMAGE], EMAIL, {"image": {"out": OTHER_IMAGE + "\n"}},
     "not an image in the eval repository")])
def test_deploy_stops_before_any_change_set_when_it_is_given_or_finds_something_wrong(
        bench, argv, env, change, says):
    done = bench.run("deploy.sh", argv, deploy_rules(True, **change), stdin=YES, env=env)
    assert done.returncode == 1 and says in done.stderr, done.stderr
    assert not bench.did("aws", *cfn("create-change-set"))


def test_a_template_over_the_inline_limit_stops_the_deploy_and_no_bucket_is_chosen(bench):
    template = bench.repo / "infra" / "eval-template.yaml"
    body = template.read_bytes()
    template.write_bytes(body + b"#" * (51_200 - len(body)))            # exactly at the limit
    done = bench.run("deploy.sh", LATER, deploy_rules(True), stdin=YES)
    assert done.returncode == 0, done.stderr
    template.write_bytes(body + b"#" * (51_201 - len(body)))            # one byte over
    done = bench.run("deploy.sh", LATER, deploy_rules(True), stdin=YES)
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
    """`now` is the stack's ScheduleState; `after` is what both live schedules say."""
    state = live_state
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
    assert done.returncode == 1 and "a live schedule is not DISABLED" in done.stderr   # believed only if seen


@pytest.mark.parametrize("argv, now", [(["--account", ACCOUNT, "status"], "DISABLED"),
                                       (ON, "ENABLED"), (OFF, "DISABLED")])
def test_status_and_a_switch_to_the_state_it_is_in_change_nothing(bench, argv, now):
    done = bench.run("schedules.sh", argv, schedule_rules(now, now))
    assert done.returncode == 0, done.stderr
    assert bench.names() == ["sts_account", "sts_arn", "state_now", "digest", "run_state", "check_state"]
    assert f"schedule discharge-eval-ws7-run is {now}" in done.stdout


LIVE = {"status": ["--account", ACCOUNT, "status"], "on": ON, "off": OFF}


@pytest.mark.parametrize("action, now, change, first, then", [
    # The stack says DISABLED and a live schedule is ENABLED: the dangerous way round.
    ("status", "DISABLED", {"run_state": {"out": "ENABLED\n"}}, "on", "off"),
    ("status", "DISABLED", {"check_state": {"out": "ENABLED\n"}}, "on", "off"),
    ("off", "DISABLED", {"run_state": {"out": "ENABLED\n"}}, "on", "off"),
    ("off", "DISABLED", {"check_state": {"out": "ENABLED\n"}}, "on", "off"),
    # The stack says ENABLED and a live schedule is DISABLED: a run that will not start.
    ("status", "ENABLED", {"run_state": {"out": "DISABLED\n"}}, "off", "on"),
    ("status", "ENABLED", {"check_state": {"out": "DISABLED\n"}}, "off", "on"),
    ("on", "ENABLED", {"run_state": {"out": "DISABLED\n"}}, "off", "on"),
    ("on", "ENABLED", {"check_state": {"out": "DISABLED\n"}}, "off", "on")])
def test_no_success_is_reported_while_a_live_schedule_differs_from_the_stacks_state(
        bench, action, now, change, first, then):
    """Ruling H3 (26 Nov 2026). The class: exit 0 while a live schedule's state differs
    from the stack's parameter. `status`, and a switch to the state the stack already
    says, both read the two live schedules; either one out of step is a STOP that
    gives the two hand commands that put both right through the stack. The partner is
    the same call with both live schedules in step."""
    done = bench.run("schedules.sh", LIVE[action], schedule_rules(now, now, **change))
    assert done.returncode == 1, done.stdout
    assert f"STOP: a live schedule is not {now}" in done.stderr
    assert "changed outside the stack" in done.stderr
    hand = [line.strip() for line in done.stderr.splitlines() if line.startswith("  infra/eval/")]
    assert hand == [f"infra/eval/schedules.sh --account {ACCOUNT} {first}     then",
                    f"infra/eval/schedules.sh --account {ACCOUNT} {then}"]
    assert not bench.did("aws", *cfn("create-change-set"))             # it reports; it changes nothing
    done = bench.run("schedules.sh", LIVE[action], schedule_rules(now, now))
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("change", [{"run_state": {"status": 255, "out": ""}},
                                    {"check_state": {"out": ""}}])
def test_a_live_schedule_that_cannot_be_read_is_not_a_success(bench, change):
    done = bench.run("schedules.sh", LIVE["status"], schedule_rules("DISABLED", "DISABLED", **change))
    assert done.returncode == 1 and "could not be read" in done.stderr


def test_only_schedules_sh_can_set_the_schedules_state():
    setters = [s.name for s in sorted(SCRIPTS.glob("*.sh"))
               if "ParameterKey=ScheduleState,ParameterValue=$WANT" in s.read_text()]
    assert setters == ["schedules.sh"]
    for script in SCRIPTS.glob("*.sh"):
        text = script.read_text()
        assert "update-schedule" not in text and "create-schedule" not in text, script.name
        assert "disable-alarm-actions" not in text and "set-alarm-state" not in text, script.name


# ========================================================= every guarded call ==

@pytest.mark.parametrize("script", ["push-image.sh", "deploy.sh", "schedules.sh"])
def test_every_aws_call_is_made_with_configured_endpoints_ignored(bench, script):
    """Ruling H2 (26 Nov 2026). The class: any configured endpoint. A variable is refused
    by the guard (the identity test above). An `endpoint_url` in a profile cannot be
    seen from a variable, so the guard tells the AWS CLI to ignore every configured
    endpoint BEFORE its first call, and that stays set for every call after it: here,
    for a whole run of each script, with the caller's own setting saying the opposite."""
    contrary = {"AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "false"}
    if script == "push-image.sh":
        c = make_remote(bench)["tip"]
        done = bench.run(script, ["--account", ACCOUNT, "--commit", c], push_rules(c), env=contrary)
    elif script == "deploy.sh":
        done = bench.run(script, LATER + ["--image-digest", IMAGE], deploy_rules(True), stdin=YES,
                         env=contrary)
    else:
        done = bench.run(script, ON, schedule_rules("DISABLED", "ENABLED"), stdin="ENABLE\n", env=contrary)
    assert done.returncode == 0, done.stderr
    calls = bench.did("aws")
    assert len(calls) >= 4 and calls[0]["args"][:2] == ["sts", "get-caller-identity"]
    assert [c["ignores_endpoints"] for c in calls] == ["true"] * len(calls)


# ================================================================ the runbook ==

RUNBOOK = ROOT / "docs" / "WS7-RUNBOOK.md"


def zsh_blocks(text: str) -> list:
    return re.findall(r"^ *```zsh\n(.*?)^ *```", text, re.S | re.M)


def bare_before_a_colon(block: str) -> list:
    """Every `$NAME` written without braces and followed by a colon. In zsh that colon
    starts a modifier: `$ACCOUNT:role` is `$ACCOUNT` with `:r` applied, then `ole`."""
    return re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*:", block)


def test_no_zsh_block_in_the_runbook_writes_a_bare_name_before_a_colon():
    """Ruling R2 (26 Nov 2026). The class: any `$NAME` followed by `:` in a zsh block
    (`:r`, `:t`, `:p`, `:h`, `:e` and the rest are all modifiers). The runbook writes
    `${NAME}` throughout, so the next block that is added cannot fall in."""
    assert bare_before_a_colon("arn:aws:iam::$ACCOUNT:role/x $B:t ${ACCOUNT}:role $1:x") == \
        ["$ACCOUNT:", "$B:"]                                            # the control
    blocks = zsh_blocks(RUNBOOK.read_text(encoding="utf-8"))
    assert len(blocks) >= 15
    for block in blocks:
        assert bare_before_a_colon(block) == [], block[:200]
        # Throughout: no bare name at all, so the rule does not depend on what follows it.
        assert re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*", block) == [], block[:200]


def test_what_zsh_does_with_a_bare_name_before_a_colon():
    """The control for the rule above, in zsh itself, where zsh is installed."""
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh is not installed here; the runbook's blocks are for the author's Mac")
    done = subprocess.run([zsh, "-f", "-c", 'A=000000000000; print -r -- "x:$A:role" "x:${A}:role"'],
                          capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert done.stdout.split() == ["x:000000000000ole", "x:000000000000:role"]

