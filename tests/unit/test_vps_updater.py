import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "vps_updater", Path(__file__).resolve().parents[2] / "tools/update-vps.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
OLD = "a" * 40
NEW = "b" * 40


def test_child_commands_cannot_consume_remaining_installer_input():
    path = Path(__file__).resolve().parents[2] / "tools/update-vps.py"
    program = f"""import importlib.util,sys
spec=importlib.util.spec_from_file_location("u", {str(path)!r})
u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)
assert u.run(sys.executable,"-c","import sys; print(sys.stdin.read())") == ""
assert sys.stdin.read() == "INSTALL_TIMER_NEXT\\n"
"""
    subprocess.run(
        [sys.executable, "-c", program],
        input="INSTALL_TIMER_NEXT\n",
        text=True,
        capture_output=True,
        check=True,
    )


def test_digest_is_bound_to_this_pull_not_an_older_tag():
    repository = "ghcr.io/egoryich/lab-manager-api"
    old_digest, new_digest = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    known = [f"{repository}@{old_digest}", f"{repository}@{new_digest}"]
    assert (
        module.pulled_digest(repository, f"Digest: {new_digest}\nStatus: done", known) == known[1]
    )
    with pytest.raises(RuntimeError, match="verify the digest"):
        module.pulled_digest(repository, f"Digest: {new_digest}", known[:1])


def ci_run(**changes):
    return {
        "id": 10,
        "head_sha": NEW,
        "head_branch": "dev-vps",
        "event": "push",
        "path": ".github/workflows/check.yml",
        "repository": {"full_name": module.REPOSITORY},
        "run_number": 1,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        **changes,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"conclusion": "failure"},
        {"status": "in_progress"},
        {"head_branch": "main"},
        {"event": "pull_request"},
        {"head_sha": OLD},
        {"path": "another.yml"},
        {"repository": {"full_name": "someone/else"}},
    ],
)
def test_only_exact_successful_push_is_eligible(change):
    assert module.verified_run({"workflow_runs": [ci_run(**change)]}, NEW) is None


def test_new_failed_rerun_cannot_fall_back_to_old_success():
    assert module.verified_run({"workflow_runs": [ci_run()]}, NEW)["id"] == 10
    payload = {"workflow_runs": [ci_run(), ci_run(run_attempt=2, conclusion="failure")]}
    assert module.verified_run(payload, NEW) is None


@pytest.fixture
def updater(tmp_path, monkeypatch):
    update = module.Updater(tmp_path)
    update.compose_file.parent.mkdir(parents=True)
    update.compose_file.write_text("services: {}\n")
    update.env.write_text(
        "# keep\nLAB_API_IMAGE=api:old\nLAB_WEB_IMAGE=web:old\n"
        "POSTGRES_PASSWORD=unchanged\nLAB_PUBLIC_ORIGIN=https://lab.example.org\n"
    )
    monkeypatch.setattr(
        update,
        "running_image",
        lambda service: "sha256:" + ("a" if service == "worker" else service[0]) * 64,
    )
    monkeypatch.setattr(update, "ready", lambda: None)
    monkeypatch.setattr(update, "schema", lambda: "0002_catalog")
    state = {
        "active_sha": OLD,
        "failed_sha": None,
        "compose_hash": module.content_hash(update.compose_file),
        "env_hash": module.content_hash(update.env),
        "schema_revision": "0002_catalog",
        "images": {s: update.running_image(s) for s in ("api", "web", "worker")},
    }
    update.save_state(state)
    return update, state


def test_success_preserves_secrets_and_commits_state(updater, monkeypatch):
    update, state = updater
    calls = []
    monkeypatch.setattr(update, "compose", lambda env, *args: calls.append((env, args)))
    update.apply(NEW, state, update.env.read_text(), "api@digest", "web@digest")
    assert module.fields(update.env.read_text())["POSTGRES_PASSWORD"] == "unchanged"
    assert update.env.read_text().startswith("# keep\n")
    assert json.loads(update.state_file.read_text())["active_sha"] == NEW
    assert not update.pending.exists()
    assert calls[0][1][-3:] == ("api", "web", "worker")
    assert "--no-deps" in calls[0][1] and "--pull" in calls[0][1]


def test_worker_image_drift_blocks_apply_before_pending(updater, monkeypatch):
    update, state = updater
    monkeypatch.setattr(update, "running_image", lambda service: service)
    with pytest.raises(RuntimeError, match="API and worker images differ"):
        update.apply(NEW, state, update.env.read_text(), "api@digest", "web@digest")
    assert not update.pending.exists()


def test_legacy_state_requires_explicit_worker_rollout(updater):
    update, state = updater
    del state["images"]["worker"]
    update.save_state(state)
    with pytest.raises(RuntimeError, match="Worker rollout"):
        update.tick()


@pytest.mark.parametrize("failure", ["compose", "ready"])
def test_failed_update_restores_exact_previous_images(updater, monkeypatch, failure):
    update, state = updater
    calls = []

    def compose(env, *args):
        calls.append(env)
        if env == update.candidate and failure == "compose":
            raise RuntimeError("failed start")

    readiness_calls = []

    def ready():
        readiness_calls.append(True)
        if len(readiness_calls) == 1 and failure == "ready":
            raise RuntimeError("failed health")

    monkeypatch.setattr(update, "compose", compose)
    monkeypatch.setattr(update, "ready", ready)
    with pytest.raises(RuntimeError, match="previous release restored"):
        update.apply(NEW, state, update.env.read_text(), "new-api", "new-web")
    restored = json.loads(update.state_file.read_text())
    assert restored["active_sha"] == OLD and restored["failed_sha"] == NEW
    assert module.fields(update.env.read_text())["LAB_API_IMAGE"] == update.running_image("api")
    assert calls == [update.candidate, update.previous]
    assert not update.pending.exists()


def test_interrupted_update_is_recovered_on_next_instance(updater, monkeypatch):
    update, state = updater
    monkeypatch.setattr(update, "compose", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        update.apply(NEW, state, update.env.read_text(), "new-api", "new-web")
    assert update.pending.exists()
    resumed = module.Updater(update.root)
    calls = []
    monkeypatch.setattr(resumed, "compose", lambda *args: calls.append(args))
    monkeypatch.setattr(resumed, "ready", lambda: None)
    monkeypatch.setattr(resumed, "schema", lambda: "0002_catalog")
    resumed.tick()
    assert len(calls) == 1 and not resumed.pending.exists()
    assert json.loads(resumed.state_file.read_text())["failed_sha"] == NEW


def test_schema_drift_prevents_automatic_rollback(updater, monkeypatch):
    update, state = updater
    update.previous.write_text(update.env.read_text())
    update.pending.write_text(json.dumps({"previous_state": state, "target_sha": NEW}))
    monkeypatch.setattr(update, "schema", lambda: "unexpected_revision")
    monkeypatch.setattr(update, "compose", lambda *args: pytest.fail("Must not touch containers"))
    with pytest.raises(RuntimeError, match="rollback refused"):
        update.recover()
    assert update.pending.exists()


def test_external_env_edit_blocks_before_network_or_container_changes(updater, monkeypatch):
    update, _ = updater
    update.env.write_text(update.env.read_text() + "CHANGED=true\n")
    monkeypatch.setattr(update, "git", lambda *args: pytest.fail("Must stop before git fetch"))
    with pytest.raises(RuntimeError, match="env changed"):
        update.tick()


def test_compose_change_requires_manual_release(updater, monkeypatch):
    update, _ = updater

    def git(*args):
        if args[:2] == ("remote", "get-url"):
            return f"https://github.com/{module.REPOSITORY}.git"
        if args == ("rev-parse", "FETCH_HEAD"):
            return NEW
        if args[0] == "rev-parse":
            return "different" if args[1] == f"{NEW}:infra/vps/compose.yml" else "same"
        return ""

    monkeypatch.setattr(update, "git", git)
    monkeypatch.setattr(
        module,
        "github_json",
        lambda path: (
            {"workflow_runs": [ci_run()]}
            if "workflows" in path
            else {
                "jobs": [
                    {"name": name, "status": "completed", "conclusion": "success"}
                    for name in ("identity-groups", "vps-images")
                ]
            }
        ),
    )
    monkeypatch.setattr(update, "apply", lambda *args: pytest.fail("Must not deploy"))
    with pytest.raises(RuntimeError, match="manual deployment required"):
        update.tick()


def test_migration_failure_restores_database_and_old_images(updater, monkeypatch):
    update, state = updater
    calls = []
    revision = "0002_catalog"

    def compose(env, *args):
        nonlocal revision
        calls.append((env, args))
        if args[:1] == ("run",):
            revision = "0005_sizing"
            raise RuntimeError("migration failed after changing schema")

    def transfer(args, *, source=None, destination=None, cwd=None):
        nonlocal revision
        if destination:
            destination.write_bytes(b"valid archive")
        elif "--list" not in args:
            revision = "0002_catalog"

    monkeypatch.setattr(update, "compose", compose)
    monkeypatch.setattr(update, "schema", lambda: revision)
    monkeypatch.setattr(
        update,
        "ready",
        lambda **kwargs: (
            (_ for _ in ()).throw(RuntimeError("candidate unhealthy")) if kwargs else None
        ),
    )
    monkeypatch.setattr(module, "transfer", transfer)
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: type("D", (), {"free": 2**32})())
    with pytest.raises(RuntimeError, match="previous release restored"):
        update.apply_migration(
            NEW, state, update.env.read_text(), "new-api", "new-web", "0005_sizing"
        )
    assert not update.pending.exists() and not update.backup.exists()
    assert json.loads(update.state_file.read_text())["failed_sha"] == NEW
    assert module.fields(update.env.read_text())["LAB_API_IMAGE"] == update.running_image("api")
    assert any(
        args[:3] == ("exec", "-T", "postgres")
        and "DROP SCHEMA public CASCADE; CREATE SCHEMA public" in args
        for _, args in calls
    )
    assert any(args[:1] == ("up",) and env == update.previous for env, args in calls)


def test_interrupted_migration_finishes_healthy_candidate(updater, monkeypatch):
    update, state = updater
    update.previous.write_text(update.env.read_text())
    update.candidate.write_text(module.image_env(update.env.read_text(), "new-api", "new-web"))
    update.backup.write_bytes(b"archive")
    update.pending.write_text(
        json.dumps(
            {
                "previous_state": state,
                "target_sha": NEW,
                "target_schema": "0005_sizing",
                "migration": True,
            }
        )
    )
    monkeypatch.setattr(update, "schema", lambda: "0005_sizing")
    monkeypatch.setattr(update, "ready", lambda **kwargs: None)
    monkeypatch.setattr(
        update, "compose", lambda *args: pytest.fail("Must not roll back healthy candidate")
    )
    update.recover()
    assert not update.pending.exists() and not update.backup.exists()
    assert json.loads(update.state_file.read_text())["schema_revision"] == "0005_sizing"


def test_unknown_migration_revision_retains_archive(updater, monkeypatch):
    update, state = updater
    update.pending.write_text(
        json.dumps(
            {
                "previous_state": state,
                "target_sha": NEW,
                "target_schema": "0005_sizing",
                "migration": True,
            }
        )
    )
    update.backup.write_bytes(b"archive")
    monkeypatch.setattr(update, "schema", lambda: "unexpected")
    with pytest.raises(RuntimeError, match="archive retained"):
        update.recover()
    assert update.backup.exists() and update.pending.exists()


def test_started_candidate_cannot_automatically_restore_possible_writes(updater, monkeypatch):
    update, state = updater
    update.previous.write_text(update.env.read_text())
    update.candidate.write_text(module.image_env(update.env.read_text(), "new-api", "new-web"))
    update.backup.write_bytes(b"archive")
    update.pending.write_text(
        json.dumps(
            {
                "previous_state": state,
                "target_sha": NEW,
                "target_schema": "0005_sizing",
                "migration": True,
                "candidate_started": True,
            }
        )
    )
    monkeypatch.setattr(update, "schema", lambda: "0005_sizing")
    monkeypatch.setattr(
        update,
        "ready",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("candidate unhealthy")),
    )
    monkeypatch.setattr(update, "compose", lambda *args: pytest.fail("Must not restore DB"))
    with pytest.raises(RuntimeError, match="may have accepted writes"):
        update.recover()
    assert update.pending.exists() and update.backup.exists()
