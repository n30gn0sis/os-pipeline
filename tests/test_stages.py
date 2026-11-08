import json

import pytest

import osp.stages as stages
from osp.mounts import safe_rmtree
from osp.run import CommandError
from osp.stages import StageError, run_stage, stages_to_force


def quiet(_msg):
    pass


def test_stages_to_force():
    assert stages_to_force(None) == set()
    assert stages_to_force("customize") == {"customize", "assemble", "test"}
    with pytest.raises(ValueError):
        stages_to_force("nope")


def test_run_stage_runs_and_writes_manifest(tmp_path):
    out = tmp_path / "s"
    calls = []
    run_stage("bootstrap", "k1", out, lambda d: calls.append(d), echo=quiet)
    assert calls == [out]
    m = json.loads((out / "stage.json").read_text())
    assert m["stage"] == "bootstrap" and m["key"] == "k1" and "completed" in m and "tool_versions" in m


def test_run_stage_uses_cache_for_same_key(tmp_path):
    out = tmp_path / "s"
    run_stage("bootstrap", "k1", out, lambda d: None, echo=quiet)
    calls = []
    run_stage("bootstrap", "k1", out, lambda d: calls.append(d), echo=quiet)
    assert calls == []


def test_run_stage_reruns_on_new_key_or_force_and_clears_dir(tmp_path):
    out = tmp_path / "s"
    run_stage("bootstrap", "k1", out, lambda d: (d / "old").write_text("x"), echo=quiet)
    run_stage("bootstrap", "k2", out, lambda d: None, echo=quiet)
    assert not (out / "old").exists()
    calls = []
    run_stage("bootstrap", "k2", out, lambda d: calls.append(d), force=True, echo=quiet)
    assert calls == [out]


def test_failed_stage_wraps_error_and_keeps_dir(tmp_path):
    out = tmp_path / "s"
    log = tmp_path / "b.log"

    def boom(d):
        (d / "partial").write_text("x")
        raise CommandError(["mmdebstrap"], 1, log)

    with pytest.raises(StageError) as ei:
        run_stage("bootstrap", "k", out, boom, echo=quiet)
    assert "stage 'bootstrap' failed" in str(ei.value)
    assert ei.value.log_path == log
    assert (out / "partial").exists() and not (out / "stage.json").exists()


def test_failed_stage_removed_without_keep_failed(tmp_path):
    out = tmp_path / "s"

    def boom(d):
        raise RuntimeError("x")

    with pytest.raises(StageError) as ei:
        run_stage("customize", "k", out, boom, keep_failed=False, log_path=tmp_path / "c.log", echo=quiet)
    assert not out.exists()
    assert ei.value.log_path == tmp_path / "c.log"


def test_failed_stage_keeps_dir_when_cleanup_refused_by_mount(tmp_path, monkeypatch):
    out = tmp_path / "s"
    log = tmp_path / "c.log"
    mounts = tmp_path / "mounts"
    mounts.write_text(f"x {out.resolve()}/proc ext4 rw 0 0\n")
    monkeypatch.setattr(stages, "safe_rmtree", lambda p: safe_rmtree(p, mounts))

    def boom(d):
        raise RuntimeError("x")

    with pytest.raises(StageError) as ei:
        run_stage("customize", "k", out, boom, keep_failed=False, log_path=log, echo=quiet)
    msg = str(ei.value)
    assert "stage 'customize' failed: x" in msg and "work dir kept" in msg and "still mounted" in msg
    assert ei.value.log_path == log
    assert out.exists()


@pytest.mark.parametrize("garbage", ["{not json", "[1, 2]", ""])
def test_corrupt_manifest_is_treated_as_not_cached(tmp_path, garbage):
    out = tmp_path / "s"
    out.mkdir()
    (out / "stage.json").write_text(garbage)
    calls = []
    run_stage("bootstrap", "k1", out, lambda d: calls.append(d), echo=quiet)
    assert calls == [out]
    m = json.loads((out / "stage.json").read_text())
    assert m["stage"] == "bootstrap" and m["key"] == "k1"
    assert not (out / "stage.json.tmp").exists()
