"""The image's two files, `Dockerfile` and `.dockerignore`, checked on the checkout
(ADR-011 D11 and "What has to change in implementation pull requests"; WS7 slice 2
part 6; docs/WS7-ENTRYPOINT.md, "Part 6: the image").

THIS FILE CHECKS THE CHECKOUT, NOT THE IMAGE. The Dockerfile is not copied into the
image, so these tests cannot pass there, and NOTHING HERE SKIPS: a skip-if-absent
would pass silently in CI if a path were wrong. The file's name is outside
`tests/test_ws7_*.py` on purpose, so the image's default command never collects it.

No docker, no AWS, no network: two text files are read. What the built image really
holds is shown only by building it (the checks in the document above).

Each test states the whole class it closes, not one example of it:

- the commit: the image carries no commit but the one the build was given;
- its place: a new commit rebuilds no RUN or COPY layer;
- the ignore file: neither protected folder can reach the build context;
- the copies: nothing reaches the image but what a COPY names;
- the command: the default command is pytest on exactly the WS7 test files on disk.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
IGNORE = ROOT / ".dockerignore"

COMMIT = "WS7_GIT_COMMIT"
WS7_TESTS = "test_ws7_*.py"

# Every instruction the Dockerfile may use. Anything else (ADD, ENTRYPOINT, ONBUILD,
# VOLUME, a second FROM ...) fails a test here, so no rule below can be stepped round
# with an instruction this file does not read.
ALLOWED = ("FROM", "ENV", "WORKDIR", "COPY", "RUN", "ARG", "USER", "CMD")

# The COPYs, exactly: (flags, sources, destination). `--chmod` is the read-only mode.
COPIES = [
    ((), ("requirements-eval.txt",), "."),
    (("--chmod=u=rwX,go=rX",), ("src/",), "src/"),
    (("--chmod=u=rwX,go=rX",), ("prompts/",), "prompts/"),
    (("--chmod=u=rwX,go=rX",), ("evals/",), "evals/"),
    (("--chmod=u=rwX,go=rX",), ("tests/",), "tests/"),
]

PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def instructions(text: str) -> list[tuple[str, str, int]]:
    """The Dockerfile as [(INSTRUCTION, the rest, line number)]. Comment lines and blank
    lines are dropped, inside a continued instruction too, as the builder drops them;
    lines ending in a backslash are joined."""
    out, buf, start = [], None, 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if buf is None:
            buf, start = "", n
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        name, _, rest = (buf + line).partition(" ")
        out.append((name.upper(), " ".join(rest.split()), start))
        buf = None
    assert buf is None, f"the instruction at line {start} ends in a continuation"
    return out


def dockerfile() -> list[tuple[str, str, int]]:
    return instructions(DOCKERFILE.read_text(encoding="utf-8"))


def ignore_patterns() -> list[str]:
    """The patterns of `.dockerignore`: each line trimmed, comments and blanks dropped."""
    lines = (raw.strip() for raw in IGNORE.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def at(found: list[tuple[str, str, int]]) -> str:
    return ", ".join(f"line {n}: {name} {rest}" for name, rest, n in found) or "none"


# ----------------------------------------------------------------- the reader ==

def test_the_reader_joins_continued_lines_and_drops_comments():
    """The control for every test below: what `instructions` makes of a small text."""
    text = "# syntax=x\nFROM a\n\nenv A=1 \\\n    # a comment\n    B=2\nCMD [\"x\", \\\n     \"y\"]\n"
    assert instructions(text) == [("FROM", "a", 2), ("ENV", "A=1 B=2", 4), ("CMD", '["x", "y"]', 7)]


def test_the_dockerfile_holds_nothing_the_reader_cannot_read():
    """A heredoc or another escape character would be read wrongly, so neither is
    allowed; and the file is one stage, so "after" below means after in the image."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "<<" not in text
    assert not re.search(r"(?im)^\s*#\s*escape\s*=", text)
    names = [name for name, _, _ in dockerfile()]
    assert names[0] == "FROM" and names.count("FROM") == 1


# ----------------------------------------------------------------- the commit ==

def test_the_commit_argument_has_no_default():
    """THE CLASS: the image carries no commit but the one the build was given. So the
    argument is declared once, bare; the variable is set once, from the argument and
    nothing else (no `:-default`, no literal); and no other instruction names it.
    Built without the argument the variable is empty, and `identity()` refuses an
    empty commit (tests/test_ws7_manifest.py)."""
    named = [i for i in dockerfile() if COMMIT in i[1]]
    args = [i for i in named if i[0] == "ARG"]
    envs = [i for i in named if i[0] == "ENV"]
    assert [rest for _, rest, _ in args] == [COMMIT], at(args)
    assert [rest for _, rest, _ in envs] == [f"{COMMIT}=${{{COMMIT}}}"], at(envs)
    assert len(named) == 2, at(named)


def test_the_commit_comes_after_the_last_run_and_copy():
    """THE CLASS: a new commit rebuilds no RUN or COPY layer. A changed argument is a
    cache miss for every RUN after it, and a changed ENV for every layer after it. So
    both come after the last RUN and the last COPY; and the ENV comes after the ARG,
    or it would be set from nothing."""
    found = dockerfile()
    order = [name if name != "ENV" or COMMIT in rest else "ENV-other" for name, rest, _ in found]
    assert order.count("ARG") == 1 and order.count("ENV") == 1, order
    last_layer = max(i for i, name in enumerate(order) if name in ("RUN", "COPY"))
    assert last_layer < order.index("ARG") < order.index("ENV"), order


# ------------------------------------------------------------ the ignore file ==

def test_both_protected_folders_are_ignored_and_nothing_brings_them_back():
    """THE CLASS: neither `evals/runs` nor `evals/calibration` can reach the build
    context. Both are listed. Only a `!` line can bring an ignored path back, and
    telling which globs could match under a folder is not worth the risk, so the file
    has NO `!` line at all. And there is no `Dockerfile.dockerignore`: when one
    exists the builder uses it and does not read `.dockerignore`."""
    patterns = ignore_patterns()
    for folder in ("evals/runs", "evals/calibration"):
        assert folder in patterns, f"{folder} is not in .dockerignore"
    assert [p for p in patterns if p.startswith("!")] == []
    assert not (ROOT / "Dockerfile.dockerignore").exists()


# ----------------------------------------------------------------- the copies ==

def test_only_the_named_files_and_folders_are_copied():
    """THE CLASS: nothing reaches the image but what a COPY names. So there is no
    `COPY .`, because every COPY is one of the five below, exactly; no ADD or any other
    instruction this file does not read; and no RUN that mounts the build context."""
    found = dockerfile()
    assert [i for i in found if i[0] not in ALLOWED] == []
    copies = []
    for _, rest, _ in (i for i in found if i[0] == "COPY"):
        words = rest.split()
        flags = tuple(w for w in words if w.startswith("--"))
        plain = [w for w in words if not w.startswith("--")]
        copies.append((flags, tuple(plain[:-1]), plain[-1]))
    assert copies == COPIES
    mounts = [i for i in found if i[0] == "RUN" and "--mount" in i[1]]
    assert mounts == [], at(mounts)


# ---------------------------------------------------------------- the command ==

def test_the_default_command_is_the_ws7_test_files_on_disk_both_ways():
    """THE CLASS: the default command is pytest on exactly the WS7 test files. It is
    the last instruction and the only CMD, in the JSON form (no shell), and its file
    list equals `tests/test_ws7_*.py` on disk, with nothing missing either way: a new
    WS7 test file that the image would not run fails here, and so does a name in the
    command with no file behind it, or `tests/` whole."""
    found = dockerfile()
    assert [name for name, _, _ in found].count("CMD") == 1 and found[-1][0] == "CMD"
    command = json.loads(found[-1][1])
    assert command[:len(PYTEST)] == PYTEST
    listed = command[len(PYTEST):]
    on_disk = sorted(f"tests/{p.name}" for p in (ROOT / "tests").glob(WS7_TESTS))
    assert on_disk, "no WS7 test file was found: the control"
    assert len(listed) == len(set(listed)), "a file is listed twice"
    assert sorted(set(on_disk) - set(listed)) == [], "on disk, not in the command"
    assert sorted(set(listed) - set(on_disk)) == [], "in the command, not on disk"


def test_this_file_is_outside_the_ws7_glob():
    """So the image's command, which is that glob written out, never collects it."""
    assert not fnmatch.fnmatch(Path(__file__).name, WS7_TESTS)
    assert fnmatch.fnmatch("test_ws7_entry.py", WS7_TESTS)          # the control
