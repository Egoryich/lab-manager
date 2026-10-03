"""The node updater accepts only a complete, verified forward release."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "node_updater", Path(__file__).resolve().parents[2] / "tools/update-node.py"
)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)
OLD = "a" * 40
NEW = "b" * 40


def test_latest_ci_attempt_must_succeed(monkeypatch):
    def api(path):
        if path.startswith("actions/runs?"):
            return {
                "workflow_runs": [
                    {
                        "id": 1,
                        "head_sha": NEW,
                        "head_branch": updater.BRANCH,
                        "event": "push",
                        "path": ".github/workflows/check.yml",
                        "repository": {"full_name": updater.REPOSITORY},
                        "run_number": 7,
                        "run_attempt": 1,
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "id": 2,
                        "head_sha": NEW,
                        "head_branch": updater.BRANCH,
                        "event": "push",
                        "path": ".github/workflows/check.yml",
                        "repository": {"full_name": updater.REPOSITORY},
                        "run_number": 7,
                        "run_attempt": 2,
                        "status": "completed",
                        "conclusion": "failure",
                    },
                ]
            }
        pytest.fail("Failed CI must not fetch a release")

    monkeypatch.setattr(updater, "api", api)
    assert updater.verified_workflow(NEW) is False


def test_release_requires_exact_assets_and_pinned_digests(monkeypatch):
    wheel = "lab_node_agent-0.7.0-py3-none-any.whl"
    assets = updater.ASSETS | {"SHA256SUMS", wheel}

    def release(extra=False):
        return {
            "tag_name": f"node-agent-{NEW}",
            "target_commitish": NEW,
            "assets": [
                {
                    "name": name,
                    "digest": "sha256:" + "1" * 64,
                    "browser_download_url": (
                        f"https://github.com/{updater.REPOSITORY}/releases/"
                        f"download/node-agent-{NEW}/{name}"
                    ),
                }
                for name in assets | ({"unexpected"} if extra else set())
            ],
        }

    monkeypatch.setattr(updater, "api", lambda path: release())
    found, name = updater.release_assets(NEW)
    assert set(found) == assets and name == wheel
    monkeypatch.setattr(updater, "api", lambda path: release(extra=True))
    with pytest.raises(updater.UpdateError, match="RELEASE_ASSETS_MISMATCH"):
        updater.release_assets(NEW)


def test_relocated_venv_entrypoints_are_reinstalled_from_verified_wheel(tmp_path, monkeypatch):
    calls = []
    broken_once = True

    def run(*args, **kwargs):
        nonlocal broken_once
        calls.append(args)
        if args[0].endswith("lab-node-agent") and broken_once:
            broken_once = False
            raise updater.UpdateError("COMMAND_FAILED:lab-node-agent")
        return ""

    monkeypatch.setattr(updater, "run", run)
    updater.verify_entrypoints(tmp_path, "agent.whl")
    assert any("--force-reinstall" in call for call in calls)
    assert any(call[0].endswith("lab-node-policy") and "--help" in call for call in calls)


def test_valid_entrypoints_need_no_reinstall(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "run", lambda *args, **kwargs: calls.append(args) or "")
    updater.verify_entrypoints(tmp_path, "agent.whl")
    assert len(calls) == 2
    assert all("--force-reinstall" not in call for call in calls)


def test_failed_activation_restores_previous_release_and_blocks_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "BASE", tmp_path)
    monkeypatch.setattr(updater, "STATE_DIR", tmp_path / "update-state")
    updater.STATE_DIR.mkdir()
    monkeypatch.setattr(updater.shutil, "disk_usage", lambda path: SimpleNamespace(free=10**9))
    monkeypatch.setattr(updater, "download_release", lambda *args: None)
    monkeypatch.setattr(updater, "run", lambda *args: None)
    switched = []
    monkeypatch.setattr(updater, "switch", switched.append)
    calls = 0

    def health():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise updater.UpdateError("CANDIDATE_UNHEALTHY")

    monkeypatch.setattr(updater, "health", health)
    with pytest.raises(updater.UpdateError, match="UPDATE_FAILED_ROLLED_BACK"):
        updater.deploy({"active_sha": OLD}, NEW, {}, "wheel.whl")
    assert switched == [NEW, OLD]
    assert not (updater.STATE_DIR / "pending.json").exists()
    assert updater.json.loads((updater.STATE_DIR / "state.json").read_text()) == {
        "active_sha": OLD,
        "blocked_sha": NEW,
    }


def test_interrupted_candidate_rolls_back_when_recovery_health_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "STATE_DIR", tmp_path)
    monkeypatch.setattr(updater.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "fcntl",
        SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None),
    )
    monkeypatch.setattr(updater.sys, "argv", ["update-node.py"])
    monkeypatch.setattr(updater, "run", lambda *args: None)
    installed = [NEW]
    monkeypatch.setattr(updater, "current_sha", lambda: installed[0])
    monkeypatch.setattr(updater, "switch", lambda sha: installed.__setitem__(0, sha))
    (tmp_path / "state.json").write_text(updater.json.dumps({"active_sha": OLD}))
    (tmp_path / "pending.json").write_text(
        updater.json.dumps({"previous_sha": OLD, "candidate_sha": NEW})
    )
    calls = 0

    def health():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise updater.UpdateError("CANDIDATE_UNHEALTHY")

    monkeypatch.setattr(updater, "health", health)
    updater.main()
    assert installed == [OLD]
    assert calls == 2
    assert not (tmp_path / "pending.json").exists()
    assert updater.json.loads((tmp_path / "state.json").read_text()) == {
        "active_sha": OLD,
        "blocked_sha": NEW,
    }
