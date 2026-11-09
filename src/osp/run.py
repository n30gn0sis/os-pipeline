"""Single choke point for running external commands: logging, redaction, errors."""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import IO, Mapping, Sequence


class CommandError(Exception):
    def __init__(self, cmd: Sequence[str], returncode: int, log_path: Path | None):
        self.cmd = [str(c) for c in cmd]
        self.returncode = returncode
        self.log_path = log_path
        super().__init__(f"command failed (exit {returncode}): {shlex.join(self.cmd)}")


class Runner:
    def __init__(self, log_path: Path | None = None, secrets: Sequence[str] = ()):
        self.log_path = Path(log_path) if log_path else None
        self._secrets = [s for s in secrets if s]

    def with_log(self, log_path: Path) -> "Runner":
        return Runner(log_path, self._secrets)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _open_log(self) -> IO[str] | None:
        if self.log_path is None:
            return None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        return open(self.log_path, "a", encoding="utf-8")

    def run(self, cmd, *, cwd=None, env=None, input: str | None = None, check: bool = True):
        cmd = [str(c) for c in cmd]
        log = self._open_log()
        lines: list[str] = []
        try:
            if log:
                log.write(f"$ {self.redact(shlex.join(cmd))}\n")
            with subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            ) as proc:
                if input is not None:
                    proc.stdin.write(input)
                    proc.stdin.close()
                for line in proc.stdout:
                    lines.append(line)
                    if log:
                        log.write(self.redact(line))
                rc = proc.wait()
        finally:
            if log:
                log.close()
        if check and rc != 0:
            raise CommandError([self.redact(c) for c in cmd], rc, self.log_path)
        return subprocess.CompletedProcess(cmd, rc, "".join(lines), "")


class RecordingRunner(Runner):
    """Test double: records commands instead of executing them."""

    def __init__(self, results: Mapping[str, tuple[int, str]] | None = None):
        super().__init__(None)
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self._results = dict(results or {})

    def with_log(self, log_path: Path) -> "RecordingRunner":
        return self

    def run(self, cmd, *, cwd=None, env=None, input: str | None = None, check: bool = True):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        self.inputs.append(input)
        rc, out = self._results.get(cmd[0], (0, ""))
        if check and rc != 0:
            raise CommandError(cmd, rc, None)
        return subprocess.CompletedProcess(cmd, rc, out, "")
