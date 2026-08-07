"""ENV-006 — the unit suite runs with no Databricks workspace.

The requirement is one sentence: `pytest tests/unit` passes with no configured workspace. The
tempting implementation is a marker convention plus a promise. That fails in the way conventions
always fail — the day someone adds a unit test that quietly picks up `~/.databrickscfg`, it passes
on every laptop in the team and fails only in CI, where the reflex fix is `continue-on-error`.

So this proves it instead of asserting it: the whole unit suite is re-run in a subprocess whose
environment has every `DATABRICKS_*` variable removed and whose `HOME` points at an empty
directory, and the assertion is that it still exits zero.

**Cost.** This roughly doubles unit-suite wall clock (~22s becomes ~45s). That is paid knowingly.
The alternative — checking a representative subset — proves the subset, and the failure mode
ENV-006 guards against is precisely a test nobody thought to include in the subset.

**Why not rely on CI alone.** The CI unit job has no `DATABRICKS_*` secrets in scope, which is the
structural enforcement and the stronger one. But CI tells you on push; this tells you on save, and
a gate that fires ten minutes later is a gate people learn to route around.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).relative_to(REPO_ROOT)

# Set in the child so the child's copy of this module skips instead of forking again. `--ignore`
# below already prevents it; this is the belt to that pair of braces, because an accidental
# recursive pytest is a very slow way to discover a mistake.
CHILD_SENTINEL = "RETAIL_LAKEHOUSE_OFFLINE_CHILD"

# Timeout is generous relative to the ~22s the suite takes, because a hang here means something
# is blocking on a network call — which is the failure being tested for, and it must surface as a
# failure rather than as a CI job that runs until the runner's own limit.
SUBPROCESS_TIMEOUT_S = 900


def offline_environment(home: Path) -> dict[str, str]:
    """A copy of the current environment with every route to a workspace removed.

    Four routes exist and all four are closed:

    * `DATABRICKS_*` variables — host, token, client id/secret, config profile, config file path.
    * `~/.databrickscfg` — closed by pointing `HOME` at an empty directory.
    * `SPARK_REMOTE` / `DATABRICKS_SERVERLESS_COMPUTE_ID` — Spark Connect back doors that would let
      a "unit" test acquire a remote session without ever touching the SDK.
    * The bundle CLI's own auth resolution, which reads the same two sources.

    `PATH`, `JAVA_HOME` and the interpreter are preserved on purpose: this test is about
    credentials, not about breaking the toolchain, and a suite that fails because `java` vanished
    would be a false positive that teaches people to ignore it.
    """
    child = {k: v for k, v in os.environ.items() if not k.startswith("DATABRICKS_")}
    child.pop("SPARK_REMOTE", None)
    child.pop("SPARK_CONNECT_MODE_ENABLED", None)
    child["HOME"] = str(home)
    child["USERPROFILE"] = str(home)
    child[CHILD_SENTINEL] = "1"
    return child


@pytest.fixture
def empty_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    assert not (home / ".databrickscfg").exists()
    return home


@pytest.mark.skipif(
    os.environ.get(CHILD_SENTINEL) == "1",
    reason="running inside the offline subprocess; forking again would recurse",
)
def test_unit_suite_needs_no_workspace(empty_home: Path) -> None:
    """ENV-006. The entire unit suite, minus this file, with no route to a workspace."""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit",
            "-m",
            "unit",
            f"--ignore={SELF}",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env=offline_environment(empty_home),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        check=False,
    )

    assert result.returncode == 0, (
        "The unit suite does not pass without a workspace, so it is not a unit suite — it is an "
        "integration suite that happens to work on machines with credentials.\n\n"
        f"exit code: {result.returncode}\n\n"
        f"--- stdout ---\n{result.stdout[-4000:]}\n"
        f"--- stderr ---\n{result.stderr[-2000:]}"
    )

    # A suite that collected nothing also exits zero. That is the shape of a green build that
    # proves nothing, and it is worth one extra assertion to rule out.
    assert " passed" in result.stdout, (
        f"No tests reported as passed — the child run collected nothing:\n{result.stdout[-2000:]}"
    )


@pytest.mark.skipif(
    os.environ.get(CHILD_SENTINEL) == "1",
    reason="running inside the offline subprocess",
)
def test_the_offline_environment_really_has_no_credentials(empty_home: Path) -> None:
    """Non-vacuity guard for the test above.

    If `offline_environment` failed to close every route, the suite would pass in the child for the
    uninteresting reason that it still had credentials, and ENV-006 would be green while untested.
    So: construct a `WorkspaceClient` in that exact environment and require it to fail.

    This test is the reason the one above can be believed.
    """
    probe = (
        "from databricks.sdk import WorkspaceClient\n"
        "try:\n"
        "    WorkspaceClient().current_user.me()\n"
        "except Exception as exc:\n"
        "    print('NO-AUTH:', type(exc).__name__)\n"
        "else:\n"
        "    print('AUTHENTICATED')\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=offline_environment(empty_home),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert "AUTHENTICATED" not in result.stdout, (
        "The 'offline' environment can still authenticate, so ENV-006's proof is circular. "
        f"stdout: {result.stdout!r}"
    )
    assert "NO-AUTH:" in result.stdout, (
        "The probe neither authenticated nor failed to authenticate, so it measured nothing.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr[-1000:]!r}"
    )


def test_no_unit_test_imports_a_workspace_client() -> None:
    """A cheap static companion that fails in milliseconds rather than in forty-five seconds.

    Not a substitute for the subprocess run — an indirect import through a `src` module would slip
    past this — but it turns the most common version of the mistake into an instant, obvious
    failure with the file name in it.
    """
    offenders = [
        path.relative_to(REPO_ROOT)
        for path in sorted((REPO_ROOT / "tests" / "unit").rglob("*.py"))
        if path != Path(__file__) and "WorkspaceClient" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"Unit tests referencing WorkspaceClient: {offenders}. A test that needs a workspace "
        "belongs in tests/integration, where its cost is visible."
    )
