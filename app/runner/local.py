"""Runs tests directly on this machine, in a fresh folder and virtualenv for every run.

That keeps runs from seeing each other's files and Python packages, but it doesn't protect
the machine: the commands run with the worker's own permissions, so only register
repositories you trust. Running each job in a throwaway container is the planned fix.
"""

import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

from app.runner.base import RunResult, RunSpec
from app.runner.junit import ReportError, parse_junit

logger = logging.getLogger(__name__)

# The platform's own steps get fixed limits. The project's timeout covers only its setup
# and test commands, so a slow download never eats into the time its tests get.
CHECKOUT_TIMEOUT_SECONDS = 300
VENV_TIMEOUT_SECONDS = 120


class _Stop(Exception):
    """Ends a run early. The message says why, e.g. "the setup command exited with code 1"."""


class LocalRunner:
    def __init__(self, workspace_root: Path | None = None, max_log_bytes: int = 200_000) -> None:
        self.workspace_root = workspace_root
        self.max_log_bytes = max_log_bytes

    def run(self, spec: RunSpec) -> RunResult:
        # resolve(): Windows can hand out the temp folder under a short name (JUSTIN~1),
        # which venv warns about. The full name keeps every path unambiguous.
        workdir = Path(tempfile.mkdtemp(prefix="run-", dir=self.workspace_root)).resolve()
        log_path = workdir / "run.log"
        try:
            # Unbuffered, so lines written here and the commands' own output stay in order.
            with log_path.open("ab", buffering=0) as log:
                result = _Job(spec, workdir, log).execute()
            return replace(result, log=_read_tail(log_path, self.max_log_bytes))
        finally:
            _remove_tree(workdir)


class _Job:
    """One run's steps, all writing to the same log."""

    def __init__(self, spec: RunSpec, workdir: Path, log: BinaryIO) -> None:
        self.spec = spec
        self.src = workdir / "src"
        self.venv = workdir / "venv"
        self.log = log

    def execute(self) -> RunResult:
        spec = self.spec
        commit_sha = None
        try:
            commit_sha = self.checkout()
            self.create_venv()
            deadline = time.monotonic() + spec.timeout_seconds
            if spec.setup_command:
                code = self.command(spec.setup_command, deadline)
                if code != 0:
                    raise _Stop(f"the setup command exited with code {code}")
            report = self.src / spec.report_path
            # A report committed to the repository must never pass for this run's results.
            report.unlink(missing_ok=True)
            exit_code = self.command(spec.test_command, deadline)
            if not report.is_file():
                raise _Stop(
                    f"the test command exited with code {exit_code} "
                    f"and wrote no report at {spec.report_path}"
                )
            try:
                cases = parse_junit(report)
            except ReportError as exc:
                raise _Stop(f"could not read the test report: {exc}") from exc
        except _Stop as stop:
            self.write(f"\nerror: {stop}\n")
            return RunResult(commit_sha=commit_sha, error=str(stop))
        return RunResult(commit_sha=commit_sha, exit_code=exit_code, cases=tuple(cases))

    def checkout(self) -> str:
        """Fetch just the requested commit, not the whole history, and return its SHA."""
        self.src.mkdir()
        deadline = time.monotonic() + CHECKOUT_TIMEOUT_SECONDS
        timed_out = f"checking out the code took over {CHECKOUT_TIMEOUT_SECONDS} s"
        for args in (
            ["git", "init", "--quiet"],
            # "--" ends git's options, so a URL or ref can never be read as one.
            ["git", "fetch", "--quiet", "--depth=1", "--", self.spec.repo_url, self.spec.ref],
            ["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        ):
            if self.step(args, deadline, timed_out) != 0:
                raise _Stop(f"could not check out {self.spec.ref!r} from {self.spec.repo_url}")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.src, capture_output=True, text=True, check=True
        )
        return head.stdout.strip()

    def create_venv(self) -> None:
        deadline = time.monotonic() + VENV_TIMEOUT_SECONDS
        timed_out = f"creating the virtualenv took over {VENV_TIMEOUT_SECONDS} s"
        if self.step([sys.executable, "-m", "venv", str(self.venv)], deadline, timed_out) != 0:
            raise _Stop("could not create the virtualenv")

    def command(self, line: str, deadline: float) -> int:
        """Run one of the project's own commands, inside the run's virtualenv."""
        timed_out = f"the setup and test commands took over {self.spec.timeout_seconds} s"
        return self.step(line, deadline, timed_out, env=_venv_env(self.venv))

    def step(
        self,
        command: str | list[str],
        deadline: float,
        timed_out: str,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run a command in the checkout, appending its output to the log. Returns its exit code."""
        self.write(f"$ {command if isinstance(command, str) else ' '.join(command)}\n")
        process = subprocess.Popen(
            command,
            # The project's commands are shell lines, like the steps in a CI config file.
            shell=isinstance(command, str),
            cwd=self.src,
            env=env or _base_env(),
            stdin=subprocess.DEVNULL,  # a test that waits for input gets end-of-file, not a hang
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # see _kill_tree
        )
        try:
            return process.wait(timeout=max(deadline - time.monotonic(), 0))
        except BaseException as exc:
            # A timeout, or the worker itself being stopped: either way nothing may keep
            # running after this, including anything the command started.
            _kill_tree(process)
            if isinstance(exc, subprocess.TimeoutExpired):
                raise _Stop(timed_out) from None
            raise

    def write(self, text: str) -> None:
        self.log.write(text.encode())


def _base_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        CI="true",  # the convention CI services follow: tools skip prompts and animations
        GIT_TERMINAL_PROMPT="0",  # a private repository fails fast instead of asking for a login
        GCM_INTERACTIVE="never",  # the same for Git Credential Manager's sign-in window
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    return env


def _venv_env(venv: Path) -> dict[str, str]:
    """What activating the virtualenv would do: its python and pip come first on PATH."""
    env = _base_env()
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    env["PATH"] = f"{scripts}{os.pathsep}{env.get('PATH', '')}"
    env["VIRTUAL_ENV"] = str(venv)
    env.pop("PYTHONHOME", None)
    return env


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill a command and every process it started, so nothing outlives the run."""
    if os.name == "nt":
        # /T takes the whole tree of child processes, /F doesn't ask them nicely first.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
    else:
        # start_new_session made the command the leader of a new process group, so one
        # signal reaches it and everything it started, and never the worker itself.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _remove_tree(path: Path) -> None:
    def make_writable_and_retry(
        function: Callable[[str], object], failed_path: str, _exc: BaseException
    ) -> None:
        # Git marks its object files read-only, and Windows won't delete read-only files.
        os.chmod(failed_path, stat.S_IWRITE)
        function(failed_path)

    for attempt in range(5):
        try:
            shutil.rmtree(path, onexc=make_writable_and_retry)
            return
        except OSError:
            # Windows can keep a just-killed process's files locked for a moment.
            time.sleep(0.2 * (attempt + 1))
    logger.warning("could not remove the workspace %s", path)


def _read_tail(path: Path, limit: int) -> str:
    """The end of the log, where failures show up: at most `limit` bytes of it."""
    with path.open("rb") as file:
        size = file.seek(0, os.SEEK_END)
        file.seek(max(size - limit, 0))
        text = file.read().decode("utf-8", errors="replace")
    if size > limit:
        text = f"[{size - limit} bytes cut from the start of the log]\n{text}"
    return text
