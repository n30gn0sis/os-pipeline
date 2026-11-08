from pathlib import Path

import pytest

from helpers import profile_data, write_yaml
from osp.profile import Profile, ProfileError, load_merged, merge


def test_merge_lists_concatenate_and_dedupe():
    assert merge({"a": [1, 2]}, {"a": [2, 3]}) == {"a": [1, 2, 3]}


def test_merge_dedupes_dict_items():
    u = {"name": "x"}
    assert merge({"users": [u]}, {"users": [dict(u)]}) == {"users": [u]}


def test_merge_scalars_override_and_dicts_recurse():
    assert merge({"s": 1, "d": {"x": 1, "y": 2}}, {"s": 2, "d": {"y": 3}}) == {"s": 2, "d": {"x": 1, "y": 3}}


def test_merge_does_not_mutate_inputs():
    a, b = {"l": [1], "d": {"x": 1}}, {"l": [2], "d": {"y": 2}}
    merge(a, b)
    assert a == {"l": [1], "d": {"x": 1}} and b == {"l": [2], "d": {"y": 2}}


def test_load_merged_resolves_extends_left_to_right(tmp_path):
    pdir = tmp_path / "profiles"
    write_yaml(pdir / "a.yaml", {"name": "a", "packages": ["a"], "live": {"autologin": False}})
    write_yaml(pdir / "b.yaml", {"name": "b", "packages": ["b"], "live": {"autologin": True}})
    child = write_yaml(pdir / "child.yaml", {"name": "child", "extends": ["a", "b"], "packages": ["c"]})
    data = load_merged(child, pdir)
    assert data["packages"] == ["a", "b", "c"]
    assert data["live"]["autologin"] is True
    assert data["name"] == "child"
    assert "extends" not in data


def test_load_merged_is_transitive_and_accepts_string_extends(tmp_path):
    pdir = tmp_path / "profiles"
    write_yaml(pdir / "a.yaml", {"packages": ["a"]})
    write_yaml(pdir / "b.yaml", {"extends": "a", "packages": ["b"]})
    child = write_yaml(pdir / "c.yaml", {"extends": ["b"], "packages": ["c"]})
    assert load_merged(child, pdir)["packages"] == ["a", "b", "c"]


def test_extends_cycle_raises(tmp_path):
    pdir = tmp_path / "profiles"
    write_yaml(pdir / "a.yaml", {"extends": ["b"]})
    write_yaml(pdir / "b.yaml", {"extends": ["a"]})
    with pytest.raises(ProfileError, match="cycle"):
        load_merged(pdir / "a.yaml", pdir)


def test_missing_parent_raises(tmp_path):
    pdir = tmp_path / "profiles"
    child = write_yaml(pdir / "c.yaml", {"extends": ["nope"]})
    with pytest.raises(ProfileError, match="not found"):
        load_merged(child, pdir)


def test_non_mapping_profile_raises(tmp_path):
    p = tmp_path / "profiles" / "x.yaml"
    p.parent.mkdir()
    p.write_text("- just\n- a list\n")
    with pytest.raises(ProfileError, match="mapping"):
        load_merged(p, p.parent)


def test_profile_accessors_and_defaults():
    p = Profile(profile_data(), Path("x.yaml"))
    assert (p.name, p.family, p.flavor, p.release, p.arch) == ("t", "debian", "debian", "bookworm", "amd64")
    assert p.packages == [] and p.files == [] and p.users == [] and p.hooks == [] and p.checks == []
    assert p.services_enable == [] and p.services_disable == []
    assert p.installer_mode == "none" and p.installer_config is None
    assert p.persistence is False and p.autologin is False and p.autologin_user is None
    assert p.desktop is None


def test_profile_autologin_user_is_first_user():
    p = Profile(profile_data(users=[{"name": "a"}, {"name": "b"}], live={"autologin": True}), Path("x"))
    assert p.autologin_user == "a"


def test_malformed_yaml_raises_profile_error(tmp_path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "x.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="x.yaml"):
        load_merged(pdir / "x.yaml", pdir)
