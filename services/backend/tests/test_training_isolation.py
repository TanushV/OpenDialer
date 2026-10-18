"""A09/A33/A50: fitting is one bounded child, never the calling process."""

import asyncio
import builtins
import hashlib
import os
import runpy
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from app.db import get, put, update
from test_background_event_loop import async_sql
from test_control_completion import principal


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


def source(service, p):
    data = training_rows()
    with service.db.transaction() as s:
        dataset = put(
            s,
            "learning_datasets",
            p["tenant_id"],
            p["seller_id"],
            {
                "rows": data,
                "goal": "meeting_held",
                "source": "immutable_operational_feature_snapshots",
            },
        )
        return {"rows": data, "goal": "meeting_held", "source_dataset_id": dataset.id}


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
        payload["rows"][0]["human"] = 1
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


def test_frozen_child_dispatch_does_not_boot_an_authority(monkeypatch):
    entry = Path(__file__).resolve().parents[3] / "scripts/backend_entry.py"
    child, seen = ModuleType("app.ml.worker"), []
    child.main = lambda: seen.append(True)
    monkeypatch.setitem(sys.modules, "app.ml.worker", child)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", [str(entry), "--train-child"])
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name not in {"app.cli", "app.main", "uvicorn"}, "Child imported the authority"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    runpy.run_path(str(entry), run_name="__main__")
    assert seen == [True]


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
    task = asyncio.create_task(worker.train_isolated(training_rows(), output, timeout_seconds=0.3))
    if failure == "cancel":
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError) as error:
            await asyncio.wait_for(task, 5)
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
@pytest.mark.parametrize("changed", ["role", "goal", "source"])
async def test_changed_authority_or_dataset_cannot_publish_trained_candidate(
    workspace, app, monkeypatch, changed
):
    from app.ml import worker

    service, p = app.state.service, principal(workspace)
    payload = source(service, p)

    async def fitted(rows, output_dir, **kwargs):
        output_dir.mkdir(parents=True)
        artifact = output_dir / "challenger.joblib"
        artifact.write_bytes(b"fixture internal artifact")
        with service.db.transaction() as s:
            if changed == "role":
                update(get(s, "users", p["user_id"]), disabled=True)
            elif changed == "source":
                update(get(s, "learning_datasets", payload["source_dataset_id"]), rows=[])
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
    assert not list((service.settings.data_dir / "models").glob("*/challenger.joblib"))
