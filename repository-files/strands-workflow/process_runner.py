"""Run a command in an owned process tree and stop it before returning."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path


class ProcessCleanupError(RuntimeError):
    """Execution artifacts cannot be consumed while processes may still write them."""


class _WindowsJob:
    def __init__(self):
        import win32job

        self.api = win32job
        self.handle = win32job.CreateJobObject(None, "")
        limits = win32job.QueryInformationJobObject(
            self.handle, win32job.JobObjectExtendedLimitInformation
        )
        limits["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(
            self.handle, win32job.JobObjectExtendedLimitInformation, limits
        )

    def assign(self, pid: int) -> None:
        import win32api
        import win32con

        process = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid
        )
        try:
            self.api.AssignProcessToJobObject(self.handle, process)
        finally:
            process.Close()

    def stop(self) -> None:
        self.api.TerminateJobObject(self.handle, 124)
        deadline = time.monotonic() + 10
        while self.api.QueryInformationJobObject(
            self.handle, self.api.JobObjectBasicAccountingInformation
        )["ActiveProcesses"]:
            if time.monotonic() >= deadline:
                raise ProcessCleanupError("Timed out waiting for the Windows process tree to stop")
            time.sleep(0.01)

    def close(self) -> None:
        self.handle.Close()


def _stop_tree(process: subprocess.Popen, job: _WindowsJob | None) -> None:
    try:
        if job is not None:
            job.stop()
            if process.poll() is None:
                process.kill()
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    except ProcessCleanupError:
        raise
    except Exception as error:
        raise ProcessCleanupError(f"Could not stop the command process tree: {error}") from error


def run_process(command: list[str], *, cwd: Path, input=None, timeout=900):
    """Capture output without pipes inherited by children keeping communicate alive."""
    job = _WindowsJob() if os.name == "nt" else None
    process = None
    try:
        with tempfile.TemporaryFile() as output:
            # The bootstrap cannot spawn the command until assignment succeeds and
            # communicate sends its payload. This closes the Job assignment race.
            # A Windows venv python.exe is itself a launcher. Use the real
            # interpreter so it cannot spawn before being assigned to the Job.
            invocation = (
                [sys._base_executable, str(Path(__file__).resolve()), "--child"] if job else command
            )
            process = subprocess.Popen(
                invocation,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                start_new_session=job is None,
            )
            expired = None
            try:
                if job:
                    job.assign(process.pid)
                payload = json.dumps({"command": command, "input": input}) if job else input
                process.communicate(payload, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                expired = error
            finally:
                _stop_tree(process, job)
            output.seek(0)
            captured = output.read().decode("utf-8", errors="replace")
            if expired is not None:
                raise subprocess.TimeoutExpired(command, timeout, output=captured)
            return subprocess.CompletedProcess(command, process.returncode, captured)
    finally:
        try:
            if job is not None:
                job.close()
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        except Exception as error:
            raise ProcessCleanupError(f"Could not finalize process cleanup: {error}") from error


def _child() -> int:
    payload = json.load(sys.stdin)
    try:
        return subprocess.run(
            payload["command"],
            input=payload["input"],
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        ).returncode
    except OSError as error:
        print(str(error), file=sys.stderr)
        return 127


if __name__ == "__main__":
    if sys.argv[1:] != ["--child"]:
        raise SystemExit("This module is launched by run_process")
    raise SystemExit(_child())
