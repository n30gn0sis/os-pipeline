import pytest

from osp.mounts import chroot_mounts, mounts_under, safe_rmtree
from osp.run import RecordingRunner


def fake_mounts(tmp_path, *mountpoints):
    f = tmp_path / "mounts"
    f.write_text("".join(f"x {m} ext4 rw 0 0\n" for m in mountpoints))
    return f


def test_mounts_under_matches_path_and_children_only(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    r = str(root.resolve())
    mf = fake_mounts(tmp_path, f"{r}/proc", f"{r}2/x", "/other")
    assert mounts_under(root, mf) == [f"{r}/proc"]


def test_mounts_under_without_mounts_file(tmp_path):
    assert mounts_under(tmp_path, tmp_path / "missing") == []


def test_safe_rmtree_refuses_when_mounted(tmp_path):
    root = tmp_path / "r"
    (root / "proc").mkdir(parents=True)
    mf = fake_mounts(tmp_path, f"{root.resolve()}/proc")
    with pytest.raises(RuntimeError, match="still mounted"):
        safe_rmtree(root, mf)
    assert root.exists()


def test_safe_rmtree_removes(tmp_path):
    root = tmp_path / "r"
    (root / "a").mkdir(parents=True)
    safe_rmtree(root, tmp_path / "none")
    assert not root.exists()


def test_chroot_mounts_order_and_cleanup_on_error(tmp_path):
    runner = RecordingRunner()
    with pytest.raises(ValueError):
        with chroot_mounts(tmp_path, runner):
            raise ValueError("boom")
    mounts = [c for c in runner.calls if c[0] == "mount"]
    umounts = [c for c in runner.calls if c[0] == "umount"]
    assert [c[-1] for c in mounts] == [str(tmp_path / p) for p in ("proc", "sys", "dev", "dev/pts")]
    assert [c[-1] for c in umounts] == [str(tmp_path / p) for p in ("dev/pts", "dev", "sys", "proc")]


def test_chroot_mounts_lazy_fallback(tmp_path):
    runner = RecordingRunner(results={"umount": (1, "busy")})
    with chroot_mounts(tmp_path, runner):
        pass
    assert ["umount", "-l", str(tmp_path / "proc")] in runner.calls
