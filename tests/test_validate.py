import pytest
import yaml

from helpers import profile_data, write_yaml
from osp.profile import ProfileError
from osp.validate import load_profile, validate


def errs(data, repo):
    return "\n".join(validate(data, repo))


def test_minimal_profile_is_valid(tmp_path):
    assert validate(profile_data(), tmp_path) == []


def test_schema_error_reports_location(tmp_path):
    assert "packages:" in errs(profile_data(packages="vim"), tmp_path)


def test_unknown_top_level_key_rejected(tmp_path):
    assert "bogus" in errs(profile_data(bogus=1), tmp_path)


def test_secret_fields_require_references(tmp_path):
    out = errs(profile_data(users=[{"name": "a", "ssh_keys_from": "ssh-ed25519 AAAA"}]), tmp_path)
    assert "users/0/ssh_keys_from" in out


def test_unknown_family(tmp_path):
    d = profile_data()
    d["distro"]["family"] = "gentoo"
    assert "unknown backend" in errs(d, tmp_path)


def test_unsupported_release(tmp_path):
    d = profile_data()
    d["distro"]["release"] = "sid"
    assert "unsupported" in errs(d, tmp_path)


def test_calamares_requires_desktop(tmp_path):
    assert "requires a desktop" in errs(profile_data(installer={"mode": "calamares"}), tmp_path)


def test_unattended_requires_config(tmp_path):
    assert "installer/config: required" in errs(profile_data(installer={"mode": "unattended"}), tmp_path)


def test_unattended_config_requires_target_disk(tmp_path):
    write_yaml(tmp_path / "installer" / "i.yaml", {"hostname": "h"})
    out = errs(profile_data(installer={"mode": "unattended", "config": "installer/i.yaml"}), tmp_path)
    assert "target_disk is required" in out


def test_install_config_rejects_keep_test_marker(tmp_path):
    write_yaml(tmp_path / "installer" / "i.yaml", {"target_disk": "/dev/sda", "keep_test_marker": True})
    out = errs(profile_data(installer={"mode": "unattended", "config": "installer/i.yaml"}), tmp_path)
    assert "keep_test_marker" in out


def test_install_config_missing_file(tmp_path):
    out = errs(profile_data(installer={"mode": "tui", "config": "installer/nope.yaml"}), tmp_path)
    assert "file not found" in out


def test_valid_unattended(tmp_path):
    write_yaml(tmp_path / "installer" / "i.yaml", {"target_disk": "/dev/sda", "hostname": "h", "confirm": True})
    assert validate(profile_data(installer={"mode": "unattended", "config": "installer/i.yaml"}), tmp_path) == []


def test_duplicate_user_names(tmp_path):
    out = errs(profile_data(users=[{"name": "a"}, {"name": "a", "groups": ["sudo"]}]), tmp_path)
    assert "duplicate user" in out


def test_autologin_requires_user(tmp_path):
    assert "autologin" in errs(profile_data(live={"autologin": True}), tmp_path)


def test_missing_file_and_hook_sources(tmp_path):
    out = errs(profile_data(files=[{"src": "files/x", "dest": "/etc/x"}], hooks={"customize": ["hooks/h.sh"]}), tmp_path)
    assert "files/x" in out and "hooks/h.sh" in out


def test_load_profile_raises_with_all_errors(tmp_path):
    p = write_yaml(tmp_path / "profiles" / "bad.yaml", profile_data(live={"autologin": True}, installer={"mode": "calamares"}))
    with pytest.raises(ProfileError) as ei:
        load_profile(p, tmp_path)
    assert "autologin" in str(ei.value) and "desktop" in str(ei.value)


def test_load_profile_returns_profile(tmp_path):
    p = write_yaml(tmp_path / "profiles" / "ok.yaml", profile_data())
    assert load_profile(p, tmp_path).name == "t"


def test_malformed_install_config_is_reported(tmp_path):
    (tmp_path / "installer").mkdir()
    (tmp_path / "installer" / "i.yaml").write_text("a: [1\n", encoding="utf-8")
    out = errs(profile_data(installer={"mode": "unattended", "config": "installer/i.yaml"}), tmp_path)
    assert "unreadable" in out


def test_explicit_null_sections_rejected(tmp_path):
    p = tmp_path / "profiles" / "nulls.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump(profile_data()) + "services:\n", encoding="utf-8")
    with pytest.raises(ProfileError) as ei:
        load_profile(p, tmp_path)
    assert "services" in str(ei.value)
