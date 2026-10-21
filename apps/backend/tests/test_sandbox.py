from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from aitsev.pipeline.sandbox import CommandResult, DockerRunner, LocalRunner, VerificationResult

PY = sys.executable


def _leftover_workdirs(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if p.name.startswith("aitsev-")]


async def test_no_verify_commands_means_unverified(tmp_path):
    runner = LocalRunner(work_root=str(tmp_path))
    result = await runner.verify(
        {"a.py": "x=1\n"}, setup_commands=["false"], verify_commands=[], timeout=30
    )
    assert result.passed is None
    assert result.commands == []


async def test_setup_then_verify_pass_and_cleanup(tmp_path):
    runner = LocalRunner(work_root=str(tmp_path))
    result = await runner.verify(
        {"a.py": "print('hello from a')\n"},
        setup_commands=[f"{PY} -c \"open('setup.txt','w').write('ok')\""],
        verify_commands=[f"{PY} a.py", "test -f setup.txt"],
        timeout=60,
    )
    assert result.passed is True
    assert [c.phase for c in result.commands] == ["setup", "verify", "verify"]
    assert "hello from a" in result.commands[1].output
    assert _leftover_workdirs(tmp_path) == []


async def test_first_failure_stops_the_run(tmp_path):
    runner = LocalRunner(work_root=str(tmp_path))
    result = await runner.verify(
        {"a.py": "raise SystemExit(3)\n"},
        setup_commands=[],
        verify_commands=[f"{PY} a.py", "echo never-runs"],
        timeout=60,
    )
    assert result.passed is False
    assert len(result.commands) == 1
    assert result.commands[0].exit_code == 3
    report = result.failure_report()
    assert "exit code 3" in report and "a.py" in report


async def test_timeout_kills_the_process_group(tmp_path):
    runner = LocalRunner(work_root=str(tmp_path))
    result = await runner.verify(
        {"slow.py": "import time\ntime.sleep(30)\n"},
        setup_commands=[],
        verify_commands=[f"{PY} slow.py"],
        timeout=1,
    )
    assert result.passed is False
    assert result.commands[0].timed_out
    assert "Timed out after 1s" in result.commands[0].output
    assert "timed out" in result.failure_report()


async def test_time_budget_is_shared_across_commands(tmp_path):
    runner = LocalRunner(work_root=str(tmp_path))
    result = await runner.verify(
        {"slow.py": "import time\ntime.sleep(1.5)\n"},
        setup_commands=[f"{PY} slow.py"],
        verify_commands=[f"{PY} slow.py"],
        timeout=2,
    )
    assert result.passed is False
    assert result.commands[0].ok
    assert result.commands[1].timed_out


async def test_output_is_truncated(tmp_path):
    runner = LocalRunner(output_limit=200, work_root=str(tmp_path))
    result = await runner.verify(
        {"big.py": "print('x' * 5000)\nprint('THE-END')\n"},
        setup_commands=[],
        verify_commands=[f"{PY} big.py"],
        timeout=60,
    )
    out = result.commands[0].output
    assert len(out) < 400 and "truncated" in out and "THE-END" in out


async def test_local_runner_does_not_leak_parent_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "super-secret")
    runner = LocalRunner(work_root=str(tmp_path))
    result = await runner.verify(
        {"env.py": "import os\nprint(os.environ.get('OLLAMA_API_KEY', 'absent'))\n"},
        setup_commands=[],
        verify_commands=[f"{PY} env.py"],
        timeout=60,
    )
    assert "absent" in result.commands[0].output
    assert "super-secret" not in result.commands[0].output


def test_failure_report_is_capped():
    result = VerificationResult(
        passed=False,
        builtin_issues=["x.py: bad"],
        commands=[CommandResult("pytest", "verify", 1, "E" * 20_000, 5)],
    )
    report = result.failure_report(limit=1000)
    assert len(report) <= 1100 and report.startswith("...(truncated)")


def test_docker_command_is_locked_down(tmp_path):
    runner = DockerRunner("python:3.12-slim", memory="1g", cpus="1")
    verify = runner.build_command(tmp_path, "pytest -q", name="n1", network=False, image=None)
    setup = runner.build_command(
        tmp_path, "pip install x", name="n2", network=True, image="node:22-slim"
    )
    for args in (verify, setup):
        joined = " ".join(args)
        assert args[:3] == ["docker", "run", "--rm"]
        assert "--cap-drop ALL" in joined
        assert "--security-opt no-new-privileges" in joined
        assert "--pids-limit 512" in joined
        assert f"-v {tmp_path}:/workspace" in joined
        assert "--privileged" not in joined
    assert "--network none" in " ".join(verify)
    assert "--network" not in setup
    assert verify[-4:] == ["python:3.12-slim", "sh", "-c", "pytest -q"]
    assert setup[-4] == "node:22-slim"
    assert "--memory 1g" in " ".join(verify) and "--cpus 1" in " ".join(verify)


def _fake_docker(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-docker"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


async def test_docker_runner_runs_through_the_docker_binary(tmp_path):
    log = tmp_path / "calls.log"
    fake = _fake_docker(
        tmp_path,
        f'echo "$@" >> {log}\ncase "$*" in *fail-me*) echo boom; exit 7;; esac\necho ok',
    )
    runner = DockerRunner("img", docker_bin=fake, work_root=str(tmp_path / "work"))
    result = await runner.verify(
        {"a.py": "x=1\n"}, setup_commands=["setup-cmd"], verify_commands=["fail-me"], timeout=30
    )
    assert result.passed is False
    assert result.commands[0].ok and result.commands[1].exit_code == 7
    calls = log.read_text().splitlines()
    assert len(calls) == 2 and "setup-cmd" in calls[0] and "--network none" in calls[1]


async def test_docker_runner_timeout_kills_container(tmp_path):
    log = tmp_path / "calls.log"
    fake = _fake_docker(tmp_path, f'echo "$@" >> {log}\ncase "$1" in kill) exit 0;; esac\nsleep 30')
    runner = DockerRunner("img", docker_bin=fake, work_root=str(tmp_path / "work"))
    result = await runner.verify({}, setup_commands=[], verify_commands=["x"], timeout=1)
    assert result.commands[0].timed_out
    assert any(line.startswith("kill aitsev-") for line in log.read_text().splitlines())


async def test_docker_missing_binary_is_a_failed_command(tmp_path):
    runner = DockerRunner("img", docker_bin=str(tmp_path / "no-docker"))
    result = await runner.verify({}, setup_commands=[], verify_commands=["x"], timeout=10)
    assert result.passed is False
    assert "not installed" in result.commands[0].output


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


@pytest.mark.docker
@pytest.mark.skipif(not _docker_available(), reason="Docker daemon not available")
async def test_real_docker_sandbox_blocks_network():
    runner = DockerRunner("python:3.12-slim", work_root=tempfile.gettempdir())
    ok = await runner.verify(
        {"t.py": "assert 1 + 1 == 2\nprint('in-container')\n"},
        setup_commands=[],
        verify_commands=["python t.py"],
        timeout=300,
    )
    assert ok.passed is True and "in-container" in ok.commands[0].output
    blocked = await runner.verify(
        {"n.py": "import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=3)\n"},
        setup_commands=[],
        verify_commands=["python n.py"],
        timeout=300,
    )
    assert blocked.passed is False
    assert os.path.isdir(tempfile.gettempdir())
