import os
from pathlib import Path
from types import SimpleNamespace
import json
import time

from scripts import hermes_worker_scope as wrapper

CURRENT_UID = int(getattr(os, "getuid", lambda: 996)())
CURRENT_GID = int(getattr(os, "getgid", lambda: 996)())


def test_wrapper_builds_fixed_bounded_worker_slice(tmp_path, monkeypatch):
    env_root = tmp_path / "input"
    env_root.mkdir(mode=0o700)
    payload = env_root / "payload.json"
    payload.write_text('{"DISPLAY": ":99"}', encoding="utf-8")
    payload.chmod(0o600)
    monkeypatch.setattr(wrapper, "ENV_ROOT", env_root)
    monkeypatch.setattr(wrapper, "WORKER_UID", CURRENT_UID)
    monkeypatch.setattr(wrapper, "WORKER_GID", CURRENT_GID)
    calls = []

    def run(argv, **_kwargs):
        if argv[0] == "/usr/bin/systemctl":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "LoadState=loaded\n"
                    f"MemoryMax={10 * 1024 * 1024 * 1024}\n"
                    f"MemoryHigh={8 * 1024 * 1024 * 1024}\n"
                    "MemorySwapMax=0\n"
                ),
            )
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(wrapper.subprocess, "run", run)

    assert wrapper._run(
        ["run", "safe-unit", str(64 * 1024 * 1024), str(payload), "--", "/bin/true"]
    ) == 0

    argv = calls[0]
    assert argv[argv.index("--uid") + 1] == str(CURRENT_UID)
    assert argv[argv.index("--gid") + 1] == str(CURRENT_GID)
    assert argv[argv.index("--slice") + 1] == "hermes-workers.slice"
    assert "MemoryMax=67108864" in argv
    assert argv[-4:] == ["exec", str(payload), "--", "/bin/true"]
    assert ":99" not in " ".join(argv)
    assert not payload.exists()


def test_wrapper_rejects_option_injection_and_multiple_stop_units(monkeypatch):
    monkeypatch.setattr(wrapper, "_validate_caller", lambda: None)
    calls = []
    monkeypatch.setattr(
        wrapper.subprocess,
        "run",
        lambda argv, check=False: calls.append(argv) or SimpleNamespace(returncode=0),
    )

    assert wrapper.main(["stop", "hermes-worker-system-one.scope", "ssh.service"]) == 2
    assert wrapper.main(["stop", "ssh.service"]) == 2
    assert calls == []


def test_wrapper_emits_only_unit_bound_recent_oom_attestation(monkeypatch, capsys):
    monkeypatch.setattr(wrapper, "_validate_caller", lambda: None)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=marker)

    monkeypatch.setattr(wrapper.subprocess, "run", run)
    since = int(time.time())
    unit = "hermes-worker-system-proof.scope"
    marker = f"{unit}: A process of this unit has been killed by the OOM killer.\n"

    assert wrapper.main(["evidence", unit, str(since)]) == 0
    assert json.loads(capsys.readouterr().out) == {"oom_kill": True, "unit": unit}
    assert calls[0][0][0:3] == ["/usr/bin/journalctl", "--unit", unit]


def test_wrapper_rejects_stale_or_non_worker_evidence_queries(monkeypatch):
    monkeypatch.setattr(wrapper, "_validate_caller", lambda: None)
    called = []
    monkeypatch.setattr(
        wrapper.subprocess,
        "run",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    assert wrapper.main(["evidence", "ssh.service", str(int(time.time()))]) == 2
    assert wrapper.main([
        "evidence",
        "hermes-worker-system-proof.scope",
        str(int(time.time()) - 301),
    ]) == 2
    assert called == []


def test_wrapper_rejects_missing_aggregate_slice(tmp_path, monkeypatch):
    env_root = tmp_path / "input"
    env_root.mkdir(mode=0o700)
    payload = env_root / "payload.json"
    payload.write_text("{}", encoding="utf-8")
    payload.chmod(0o600)
    monkeypatch.setattr(wrapper, "ENV_ROOT", env_root)
    monkeypatch.setattr(wrapper, "WORKER_UID", CURRENT_UID)
    monkeypatch.setattr(
        wrapper.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "LoadState=not-found\nMemoryMax=infinity\n"
                "MemoryHigh=infinity\nMemorySwapMax=infinity\n"
            ),
        ),
    )

    try:
        wrapper._run(
            ["run", "safe-unit", str(64 * 1024 * 1024), str(payload), "--", "/bin/true"]
        )
    except ValueError as exc:
        assert "absent or unbounded" in str(exc)
    else:
        raise AssertionError("unbounded aggregate slice was accepted")


def test_worker_exec_consumes_private_environment(tmp_path, monkeypatch):
    env_root = tmp_path / "input"
    env_root.mkdir(mode=0o700)
    payload = env_root / "payload.json"
    payload.write_text('{"HOME": "/safe", "PATH": "/usr/bin"}', encoding="utf-8")
    payload.chmod(0o600)
    monkeypatch.setattr(wrapper, "ENV_ROOT", env_root)
    monkeypatch.setattr(wrapper, "WORKER_UID", CURRENT_UID)
    calls = []
    monkeypatch.setattr(
        wrapper.os,
        "execvpe",
        lambda executable, argv, env: calls.append((executable, argv, env)),
    )

    assert wrapper._exec_worker(
        ["exec", str(payload), "--", "/bin/true", "--literal"]
    ) == 127
    assert calls == [
        (
            "/bin/true",
            ["/bin/true", "--literal"],
            {"HOME": "/safe", "PATH": "/usr/bin"},
        )
    ]
    assert not payload.exists()
