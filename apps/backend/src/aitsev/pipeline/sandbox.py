"""Running verification commands against a candidate's files.

* ``DockerRunner`` (production): each command runs in a fresh container with the workspace
  mounted, dropped capabilities, resource limits and, for verify commands, no network.
* ``LocalRunner`` (development and tests only): a plain subprocess in a temporary directory.

Setup commands (for example installing dependencies into ``.venv``) run first with network
access. Verify commands (build, lint, tests) run after, without network in Docker.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from aitsev.pipeline.workspace import write_tree


@dataclass(slots=True)
class CommandResult:
    command: str
    phase: str  # setup | verify
    exit_code: int | None
    output: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(slots=True)
class VerificationResult:
    """``passed`` is None when no verify commands were configured."""

    passed: bool | None
    builtin_issues: list[str] = field(default_factory=list)
    commands: list[CommandResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "builtin_issues": list(self.builtin_issues),
            "commands": [asdict(c) for c in self.commands],
        }

    def failure_report(self, limit: int = 6000) -> str:
        parts: list[str] = []
        if self.builtin_issues:
            parts.append("Built-in checks failed:\n" + "\n".join(self.builtin_issues))
        for cmd in self.commands:
            if not cmd.ok:
                status = "timed out" if cmd.timed_out else f"exit code {cmd.exit_code}"
                parts.append(f"$ {cmd.command}  ({cmd.phase}, {status})\n{cmd.output}")
        report = "\n\n".join(parts) or "Verification failed."
        return report if len(report) <= limit else "...(truncated)...\n" + report[-limit:]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 4
    return text[:head] + "\n...(output truncated)...\n" + text[-(limit - head) :]


class Runner(ABC):
    def __init__(self, output_limit: int = 20_000, work_root: str | None = None) -> None:
        self.output_limit = output_limit
        self.work_root = work_root

    def _make_workdir(self) -> Path:
        if self.work_root:
            Path(self.work_root).mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="aitsev-", dir=self.work_root))

    @abstractmethod
    async def run(
        self, workdir: Path, command: str, *, timeout: int, network: bool, image: str | None
    ) -> CommandResult: ...

    async def verify(
        self,
        files: Mapping[str, str],
        *,
        setup_commands: Sequence[str],
        verify_commands: Sequence[str],
        timeout: int,
        image: str | None = None,
    ) -> VerificationResult:
        """Write ``files`` to a fresh directory and run setup then verify commands."""
        if not verify_commands:
            return VerificationResult(passed=None)
        workdir = await asyncio.to_thread(self._make_workdir)
        try:
            await asyncio.to_thread(write_tree, workdir, files)
            results: list[CommandResult] = []
            deadline = time.monotonic() + timeout
            phases = [("setup", c, True) for c in setup_commands]
            phases += [("verify", c, False) for c in verify_commands]
            for phase, command, network in phases:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    results.append(
                        CommandResult(command, phase, None, "Time budget exhausted", 0, True)
                    )
                    return VerificationResult(passed=False, commands=results)
                result = await self.run(
                    workdir,
                    command,
                    timeout=max(1, math.ceil(remaining)),
                    network=network,
                    image=image,
                )
                result.phase = phase
                result.output = _truncate(result.output, self.output_limit)
                results.append(result)
                if not result.ok:
                    return VerificationResult(passed=False, commands=results)
            return VerificationResult(passed=True, commands=results)
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)


class LocalRunner(Runner):
    """Runs commands as a local subprocess. Not isolated: use only for development and tests."""

    async def run(
        self, workdir: Path, command: str, *, timeout: int, network: bool, image: str | None
    ) -> CommandResult:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(workdir),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        started = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workdir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_session_kwargs(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            _kill_tree(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            return CommandResult(
                command, "verify", None, f"Timed out after {timeout}s", _ms(started), True
            )
        return CommandResult(
            command,
            "verify",
            proc.returncode,
            out.decode("utf-8", errors="replace"),
            _ms(started),
        )


class DockerRunner(Runner):
    """Runs each command in a fresh, locked-down container."""

    def __init__(
        self,
        image: str,
        *,
        memory: str = "2g",
        cpus: str = "2",
        output_limit: int = 20_000,
        work_root: str | None = None,
        docker_bin: str = "docker",
    ) -> None:
        super().__init__(output_limit=output_limit, work_root=work_root)
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.docker_bin = docker_bin

    def build_command(
        self, workdir: Path, command: str, *, name: str, network: bool, image: str | None
    ) -> list[str]:
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        args = [
            self.docker_bin, "run", "--rm",
            "--name", name,
            "--user", f"{uid}:{gid}",
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", "512",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-e", "HOME=/workspace/.home",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{workdir}:/workspace",
            "-w", "/workspace",
        ]  # fmt: skip
        if not network:
            args += ["--network", "none"]
        args += [image or self.image, "sh", "-c", command]
        return args

    async def run(
        self, workdir: Path, command: str, *, timeout: int, network: bool, image: str | None
    ) -> CommandResult:
        name = f"aitsev-{uuid.uuid4().hex[:12]}"
        args = self.build_command(workdir, command, name=name, network=network, image=image)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **_session_kwargs(),
            )
        except FileNotFoundError:
            return CommandResult(
                command, "verify", None, "Docker is not installed or not on PATH", _ms(started)
            )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            await self._kill(name)
            _kill_tree(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            return CommandResult(
                command, "verify", None, f"Timed out after {timeout}s", _ms(started), True
            )
        return CommandResult(
            command, "verify", proc.returncode, out.decode("utf-8", errors="replace"), _ms(started)
        )

    async def _kill(self, name: str) -> None:
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                self.docker_bin, "kill", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            await asyncio.wait_for(killer.wait(), timeout=30)


def _session_kwargs() -> dict[str, Any]:
    """Start children in their own process group on POSIX so a timeout kills the whole tree."""
    return {"start_new_session": True} if sys.platform != "win32" else {}


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # Windows: no process groups here; kill the direct child
            proc.kill()


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
