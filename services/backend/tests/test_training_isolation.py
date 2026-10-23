"""A09/A33/A50: fitting is one bounded child, never the calling process."""

import asyncio
import builtins
import contextlib
import hashlib
import os
import runpy
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from app.db import get, put, update
from helpers import async_sql, principal


def training_rows():
    at = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        {
            "lead_id": str(i),
            "endpoint_id": f"e{i}",
            "at": (at + timedelta(days=i)).isoformat(),
            "outcome_available_at": (at + timedelta(days=i, minutes=1)).isoformat(),
            "human": int(i % 3 != 0),
            "features": {"local_hour": 9 + i % 8},
        }
        for i in range(120)
    ]


def source(service, p, **fields):
    """An immutable dataset and the job that names it; the job carries no rows."""
    from app.optimization.workflows import dataset_checksum

    value = {"rows": training_rows(), "goal": "meeting_held", **fields}
    checksum = dataset_checksum(value)
    with service.db.transaction() as s:
        dataset = put(
            s,
            "learning_datasets",
            p["tenant_id"],
            p["seller_id"],
            {**value, "checksum": checksum, "source": "immutable_operational_feature_snapshots"},
        )
        return {
            "goal": "meeting_held",
            "source_dataset_id": dataset.id,
            "input_fingerprint": checksum,
            **{k: v for k, v in fields.items() if k in {"baseline", "window_days"}},
        }


def test_operational_training_admission_keeps_unclassified_outcomes_unknown(workspace, app, monkeypatch):
    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(
        service,
        "save_operational_dataset",
        lambda p: {
            "id": "unclassified-dataset",
            "rows": [{"human": None}] * 120,
        },
    )
    result = service.train_operational(p)
    assert result["status"] == "insufficient_data" and result["rows"] == 120
    assert result["classified_rows"] == 0


@pytest.mark.asyncio
async def test_training_freezes_and_uses_the_actual_configured_baseline(workspace, app):
    from app.scheduling.models import EstimatePolicy

    service, p = app.state.service, principal(workspace)
    baseline = {
        "estimate_policy": EstimatePolicy(human_prior=(8, 2)).model_dump(mode="json"),
        "handling_seconds": 300,
    }
    with service.db.transaction() as s:
        campaign = service.required(s, "campaigns", workspace["campaign_id"], p)
        update(campaign, optimization={**service.optimization_config(s, p), **baseline})
    snapshot = service.save_operational_dataset(p)
    assert snapshot.get("baseline") == baseline
    payload = source(service, p, baseline=baseline, window_days=14)
    await service.train_model_job(p, payload)
    with service.db.transaction(False) as s:
        model = service._by_key(s, p, "model_versions", "training:" + payload["source_dataset_id"])
        assert model.data.get("baseline") == baseline
        assert model.data.get("window_days") == 14


def test_training_source_rejects_changed_window_before_publication(workspace, app):
    from fastapi import HTTPException

    service, p = app.state.service, principal(workspace)
    payload = source(service, p, window_days=7)
    with service.db.transaction(False) as s, pytest.raises(HTTPException) as failure:
        service._training_source(s, p, payload)
    assert failure.value.status_code == 409


@pytest.mark.asyncio
async def test_training_cannot_substitute_an_easier_baseline(workspace, app, monkeypatch):
    from fastapi import HTTPException

    service, p = app.state.service, principal(workspace)
    payload = source(service, p)
    payload["baseline"] = {"estimate_policy": {"human_prior": [0.01, 100]}, "handling_seconds": 120}

    async def forbidden(*args, **kwargs):
        raise AssertionError("Changed baseline reached fitting")

    monkeypatch.setattr("app.ml.worker.train_isolated", forbidden)
    with pytest.raises(HTTPException) as failure:
        await service.train_model_job(p, payload)
    assert failure.value.status_code == 409


@pytest.mark.asyncio
async def test_training_uses_a_real_child_and_publishes_off_loop_without_inheriting_secrets(
    workspace, app, monkeypatch
):
    from app.ml import training

    def forbidden(*args):
        raise AssertionError("Fitting ran inside the live authority process")

    monkeypatch.setattr(training, "train_challenger", forbidden)
    monkeypatch.setenv("OPENDIALER_SECRET_KEY", "must-not-reach-training")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-reach-training")
    spawn, environments = asyncio.create_subprocess_exec, []

    async def inspected(*args, **kwargs):
        environments.append(kwargs["env"])
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", inspected)
    service, p = app.state.service, principal(workspace)
    payload = source(service, p)
    with async_sql(app) as queries:
        await service.train_model_job(p, payload)
    assert not queries, queries
    with service.db.transaction(False) as s:
        models = list(service.scoped(s, "model_versions", p))
        assert len(models) == 1
        model = models[0].data
        assert model["source_dataset_id"] == payload["source_dataset_id"]
        assert model["training_runtime"]["pid"] != os.getpid()
        assert model["training_runtime"]["native_threads"] == 1
        assert model["training_runtime"]["priority"] == "low"
        assert not model["active"]
        assert hashlib.sha256(Path(model["artifact"]).read_bytes()).hexdigest() == model["checksum"]
    assert len(environments) == 1
    assert "OPENDIALER_SECRET_KEY" not in environments[0]
    assert "OPENROUTER_API_KEY" not in environments[0]
    assert environments[0]["OPENBLAS_NUM_THREADS"] == "1"
    await service.train_model_job(p, payload)
    assert len(environments) == 1, "A replayed completed dataset cannot fit another candidate"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "other_seller", "altered_rows"])
async def test_training_requires_the_exact_owned_dataset_before_spawning(workspace, app, monkeypatch, change):
    service, p = app.state.service, principal(workspace)
    payload = source(service, p)
    if change == "missing":
        payload["source_dataset_id"] = "absent"
    elif change == "altered_rows":
        # The named dataset no longer matches the content the job was queued for.
        with service.db.transaction() as s:
            dataset = get(s, "learning_datasets", payload["source_dataset_id"])
            update(dataset, rows=[{**dataset.data["rows"][0], "human": 1}, *dataset.data["rows"][1:]])
    else:
        with service.db.transaction() as s:
            get(s, "learning_datasets", payload["source_dataset_id"]).seller_id = "another-seller"

    def forbidden(*args):
        raise AssertionError("Unowned input reached training")

    from app.ml import training

    monkeypatch.setattr(training, "train_challenger", forbidden)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await service.train_model_job(p, payload)
    assert error.value.status_code in {403, 404, 409}


ENTRIES = Path(__file__).resolve().parents[3] / "scripts"


def test_frozen_trainer_dispatch_does_not_boot_an_authority(monkeypatch):
    entry = ENTRIES / "trainer_entry.py"
    child, seen = ModuleType("app.ml.worker"), []
    child.main = lambda: seen.append(True)
    monkeypatch.setitem(sys.modules, "app.ml.worker", child)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", [str(entry)])
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name not in {"app.cli", "app.main", "uvicorn"}, "Trainer imported the authority"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    runpy.run_path(str(entry), run_name="__main__")
    assert seen == [True]


def test_trainer_self_check_loads_the_fitting_stack_and_rejects_other_arguments():
    import json
    import subprocess

    entry = str(ENTRIES / "trainer_entry.py")
    receipt = subprocess.run(  # noqa: S603 - current interpreter, repository entrypoint
        [sys.executable, entry, "--self-check"], capture_output=True, text=True, timeout=120, check=True
    )
    assert {"numpy", "scipy", "sklearn"} <= set(json.loads(receipt.stdout)["libraries"])
    other = subprocess.run(  # noqa: S603 - current interpreter, repository entrypoint
        [sys.executable, entry, "serve"], capture_output=True, text=True, timeout=120
    )
    assert other.returncode == 2 and "opendialer-trainer" in other.stderr


@pytest.mark.parametrize(
    "authority,trainer",
    [
        ("opendialer-backend", "opendialer-trainer"),
        ("opendialer-backend-aarch64-apple-darwin", "opendialer-trainer-aarch64-apple-darwin"),
        ("opendialer-backend.exe", "opendialer-trainer.exe"),
        ("opendialer-backend-x86_64-pc-windows-msvc.exe", "opendialer-trainer-x86_64-pc-windows-msvc.exe"),
    ],
)
def test_frozen_training_launches_the_trainer_installed_beside_the_authority(
    tmp_path, monkeypatch, authority, trainer
):
    from app.ml import worker

    for name in (authority, trainer):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / authority))
    assert worker.child_command() == [str(tmp_path / trainer)]
    assert worker.trainer_unavailable() is None


def test_missing_trainer_is_a_clear_error_that_never_queues_training(workspace, app, tmp_path, monkeypatch):
    from app.ml import training, worker

    (tmp_path / "opendialer-backend").write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "opendialer-backend"))
    with pytest.raises(RuntimeError, match="opendialer-trainer"):
        worker.child_command()
    service, p = app.state.service, principal(workspace)
    monkeypatch.setattr(training, "training_support", lambda rows: None)
    monkeypatch.setattr(
        service,
        "save_operational_dataset",
        lambda p: {"id": "dataset", "rows": [{"human": i % 2} for i in range(200)]},
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Training was queued without a trainer")

    monkeypatch.setattr(service.jobs, "enqueue", forbidden)
    result = service.train_operational(p)
    assert result["status"] == "trainer_unavailable"
    assert "opendialer-trainer" in result["reason"]


def test_frozen_trainer_is_an_independent_runtime_not_the_authoritys(monkeypatch):
    from app.ml import worker

    for name in ("_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL"):
        monkeypatch.setenv(name, "/authority/extraction")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/authority/extraction")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/authority/extraction")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    environment = worker.child_environment()
    assert not [k for k in environment if k.startswith("_PYI_") or k.endswith("LIBRARY_PATH")]
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "PYTHONPATH" not in environment


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "cancel", "overflow", "malformed", "exit"])
async def test_training_child_failures_are_bounded_reaped_and_sanitized(tmp_path, monkeypatch, failure):
    from app.ml import worker

    commands = {
        "timeout": "import time; time.sleep(30)",
        "cancel": "import time; time.sleep(30)",
        "overflow": "import sys; sys.stdout.write('x' * 3000000); sys.stdout.flush()",
        "malformed": "print('raw synthetic content must not escape')",
        "exit": "import sys; sys.stderr.write('private synthetic error'); sys.exit(1)",
    }
    monkeypatch.setattr(worker, "child_command", lambda: [sys.executable, "-c", commands[failure]])
    spawn, processes, started = asyncio.create_subprocess_exec, [], asyncio.Event()

    async def observed(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        processes.append(child)
        started.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed)
    output = tmp_path / "models" / "candidate"
    # Only the timeout case uses a short budget: its child sleeps for 30 s, so the timeout is
    # the sole possible outcome. Every other child gets a budget it cannot plausibly hit, so a
    # slow interpreter start under load cannot turn its failure into a timeout (or a cancel
    # into a timeout), and the message below identifies which mechanism actually fired.
    budget = 0.3 if failure == "timeout" else 120
    task = asyncio.create_task(worker.train_isolated(training_rows(), output, timeout_seconds=budget))
    if failure == "cancel":
        await asyncio.wait_for(started.wait(), 60)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError) as error:
            await asyncio.wait_for(task, 150)
        expected = "Training timed out" if failure == "timeout" else "Training failed validation"
        assert str(error.value).startswith(expected), (failure, str(error.value))
        assert "synthetic" not in str(error.value) and len(str(error.value)) < 200
    assert processes and all(p.returncode is not None for p in processes)
    assert not output.exists(), "Failed or cancelled candidates leave no partial model directory"


@pytest.mark.asyncio
async def test_only_one_training_child_per_authority_and_cancellation_releases_it(tmp_path, monkeypatch):
    from app.ml import worker

    monkeypatch.setattr(
        worker, "child_command", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    spawn, started, children = asyncio.create_subprocess_exec, asyncio.Event(), []

    async def observed(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed)
    first = asyncio.create_task(worker.train_isolated(training_rows(), tmp_path / "models" / "first"))
    await asyncio.wait_for(started.wait(), 3)
    try:
        with pytest.raises(RuntimeError, match="already active"):
            await worker.train_isolated(training_rows(), tmp_path / "models" / "second")
        assert len(children) == 1
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    with pytest.raises(RuntimeError, match="timed out"):
        await worker.train_isolated(training_rows(), tmp_path / "models" / "third", timeout_seconds=0.1)
    assert len(children) == 2 and all(p.returncode is not None for p in children)


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[{}] * 20001, [{"features": {"segment": "x" * 17000000}}]])
async def test_training_input_limits_reject_before_spawning(tmp_path, monkeypatch, rows):
    from app.ml import worker

    async def forbidden(*args, **kwargs):
        raise AssertionError("Oversized input started a child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(ValueError, match="bound"):
        await worker.train_isolated(rows, tmp_path / "candidate")


@pytest.mark.asyncio
async def test_cancellation_during_process_creation_still_reaps_owned_child(tmp_path, monkeypatch):
    from app.ml import worker

    monkeypatch.setattr(
        worker, "child_command", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    spawn, started, release, children = asyncio.create_subprocess_exec, asyncio.Event(), asyncio.Event(), []

    async def delayed(*args, **kwargs):
        child = await spawn(*args, **kwargs)
        children.append(child)
        started.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    task = asyncio.create_task(worker.train_isolated(training_rows(), tmp_path / "models" / "candidate"))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert children[0].returncode is not None, "Cancellation during spawn orphaned its CPU child"
    finally:
        if children[0].returncode is None:
            children[0].kill()
            await children[0].wait()


@pytest.mark.asyncio
async def test_training_capacity_lock_is_shared_across_authority_processes(tmp_path):
    from app.ml import worker

    root = tmp_path / "models"
    holder = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; from pathlib import Path; from app.ml.worker import training_slot; "
        "slot=training_slot(Path(sys.argv[1])); print('locked', flush=True); sys.stdin.read()",
        str(root),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env=worker.child_environment(),
    )
    try:
        assert await asyncio.wait_for(holder.stdout.readline(), 3) == b"locked\n"
        with pytest.raises(RuntimeError, match="already active"):
            await worker.train_isolated(training_rows(), root / "second")
        assert not (root / "second").exists()
    finally:
        holder.stdin.close()
        await asyncio.wait_for(holder.wait(), 3)


@pytest.mark.asyncio
async def test_cancellation_during_cleanup_does_not_leak_training_capacity(tmp_path, monkeypatch):
    from app.ml import worker

    monkeypatch.setattr(
        worker, "child_command", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    original, acquired, descriptors = worker.reap, worker.training_slot, []
    entered, release = asyncio.Event(), asyncio.Event()

    def observed(root):
        descriptor = acquired(root)
        descriptors.append(descriptor)
        return descriptor

    async def delayed(child):
        entered.set()
        await release.wait()
        await original(child)

    monkeypatch.setattr(worker, "training_slot", observed)
    monkeypatch.setattr(worker, "reap", delayed)
    task = asyncio.create_task(
        worker.train_isolated(training_rows(), tmp_path / "models" / "first", timeout_seconds=0.1)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeError, match="timed out"):
            await worker.train_isolated(training_rows(), tmp_path / "models" / "second", timeout_seconds=0.1)
        assert not (tmp_path / "models" / "first").exists()
    finally:
        release.set()
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)


@pytest.mark.asyncio
async def test_cancelling_during_publication_does_not_delete_committed_artifact(workspace, app, monkeypatch):
    from app.ml import worker

    service, p = app.state.service, principal(workspace)
    payload = source(service, p)

    def fit_result(output):
        output.mkdir(parents=True)
        path = output / "challenger.json"
        path.write_bytes(b"owned candidate")
        return {"artifact": str(path), "checksum": hashlib.sha256(path.read_bytes()).hexdigest()}

    async def fitted(rows, output_dir, **kwargs):
        return fit_result(output_dir)

    monkeypatch.setattr(worker, "train_isolated", fitted)
    original, entered, release = service._training_publish, threading.Event(), threading.Event()

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(service, "_training_publish", delayed)
    task = asyncio.create_task(service.train_model_job(p, payload))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with service.db.transaction(False) as s:
            models = service.scoped(s, "model_versions", p)
            assert len(models) == 1 and Path(models[0].data["artifact"]).exists()
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["role", "goal", "source", "baseline"])
async def test_changed_authority_or_dataset_cannot_publish_trained_candidate(
    workspace, app, monkeypatch, changed
):
    from app.ml import worker

    service, p = app.state.service, principal(workspace)
    payload = source(service, p)
    if changed == "baseline":
        with service.db.transaction(False) as s:
            config = service.optimization_config(s, p)
        payload = source(service, p, baseline={k: config[k] for k in ("estimate_policy", "handling_seconds")})

    async def fitted(rows, output_dir, **kwargs):
        output_dir.mkdir(parents=True)
        artifact = output_dir / "challenger.json"
        artifact.write_bytes(b"fixture internal artifact")
        with service.db.transaction() as s:
            if changed == "role":
                update(get(s, "users", p["user_id"]), disabled=True)
            elif changed == "source":
                update(get(s, "learning_datasets", payload["source_dataset_id"]), rows=[])
            elif changed == "baseline":
                campaign = service.scoped(s, "campaigns", p)[0]
                update(
                    campaign, optimization={**campaign.data.get("optimization", {}), "handling_seconds": 300}
                )
            else:
                campaign = service.scoped(s, "campaigns", p)[0]
                update(
                    campaign,
                    optimization={**campaign.data.get("optimization", {}), "conversion_goal": "sale_won"},
                )
        return {"artifact": str(artifact), "checksum": hashlib.sha256(artifact.read_bytes()).hexdigest()}

    monkeypatch.setattr(worker, "train_isolated", fitted)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await service.train_model_job(p, payload)
    assert error.value.status_code in {403, 409}
    with service.db.transaction(False) as s:
        assert not service.scoped(s, "model_versions", p)
    assert not list((service.settings.data_dir / "models").glob("*/challenger.json"))
