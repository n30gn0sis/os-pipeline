import hashlib
from datetime import date
from pathlib import Path

from helpers import profile_data
from osp.buildid import artifact_stem, compute_build_hash, referenced_paths
from osp.profile import Profile


def make(repo: Path) -> Profile:
    (repo / "files" / "d").mkdir(parents=True)
    (repo / "files" / "d" / "a.conf").write_text("a=1")
    (repo / "hooks").mkdir()
    (repo / "hooks" / "h.sh").write_text("echo hi")
    return Profile(profile_data(
        files=[{"src": "files/d/", "dest": "/etc/d/"}],
        hooks={"customize": ["hooks/h.sh"]},
    ), repo / "profiles" / "t.yaml")


def test_referenced_paths(tmp_path):
    assert referenced_paths(make(tmp_path)) == ["files/d/", "hooks/h.sh"]


def test_hash_is_stable(tmp_path):
    p = make(tmp_path)
    assert compute_build_hash(p, tmp_path, {}) == compute_build_hash(p, tmp_path, {})
    assert len(compute_build_hash(p, tmp_path, {})) == 64


def test_hash_changes_with_referenced_file_content(tmp_path):
    p = make(tmp_path)
    before = compute_build_hash(p, tmp_path, {})
    (tmp_path / "files" / "d" / "a.conf").write_text("a=2")
    assert compute_build_hash(p, tmp_path, {}) != before


def test_hash_changes_with_secret_value_but_does_not_contain_it(tmp_path):
    p = make(tmp_path)
    secret = "hunter2-SECRET"
    h1 = compute_build_hash(p, tmp_path, {"env:K": "one"})
    h2 = compute_build_hash(p, tmp_path, {"env:K": "two"})
    assert h1 != h2
    h3 = compute_build_hash(p, tmp_path, {"env:K": secret})
    assert secret not in h3
    assert hashlib.sha256(secret.encode()).hexdigest() not in h3


def test_hash_changes_with_merged_profile(tmp_path):
    p = make(tmp_path)
    before = compute_build_hash(p, tmp_path, {})
    changed = Profile(profile_data(
        files=[{"src": "files/d/", "dest": "/etc/d/"}],
        hooks={"customize": ["hooks/h.sh"]},
        packages=["vim"],
    ), p.source)
    assert compute_build_hash(changed, tmp_path, {}) != before


def test_installer_config_is_referenced_and_hashed(tmp_path):
    (tmp_path / "installer").mkdir()
    (tmp_path / "installer" / "i.yaml").write_text("hostname: a")
    p = Profile(profile_data(installer={"mode": "tui", "config": "installer/i.yaml"}),
                tmp_path / "profiles" / "t.yaml")
    assert referenced_paths(p) == ["installer/i.yaml"]
    before = compute_build_hash(p, tmp_path, {})
    (tmp_path / "installer" / "i.yaml").write_text("hostname: b")
    assert compute_build_hash(p, tmp_path, {}) != before


def test_payload_tree_is_hashed(tmp_path, monkeypatch):
    import osp.buildid as buildid

    payload_root = tmp_path / "pkg"
    (payload_root / "payload").mkdir(parents=True)
    (payload_root / "payload" / "x").write_text("one")
    monkeypatch.setattr(buildid.resources, "files", lambda _pkg: payload_root)
    p = make(tmp_path)
    before = compute_build_hash(p, tmp_path, {})
    (payload_root / "payload" / "x").write_text("two")
    assert compute_build_hash(p, tmp_path, {}) != before


def test_artifact_stem(tmp_path):
    p = make(tmp_path)
    assert artifact_stem(p, "abcdef0123", date(2026, 11, 8)) == "t-bookworm-amd64-20261108-abcdef"
