import json
from pathlib import Path

import pytest

from helpers import profile_data
from osp.launcher import FILE_MAP_ENV
from osp.profile import Profile
from osp.secrets import (
    SecretError,
    collect_refs,
    env_names,
    file_map_from_env,
    file_paths,
    resolve,
    secret_strings,
)


def make(users):
    return Profile(profile_data(users=users), Path("x"))


def test_collect_refs_unique_in_order():
    p = make([
        {"name": "a", "ssh_keys_from": "env:K", "password_hash_from": "file:/s/pw"},
        {"name": "b", "ssh_keys_from": "env:K"},
    ])
    assert collect_refs(p) == ["env:K", "file:/s/pw"]
    assert env_names(collect_refs(p)) == ["K"]
    assert file_paths(collect_refs(p)) == ["/s/pw"]


def test_resolve_env_and_file(tmp_path):
    f = tmp_path / "pw"
    f.write_text("$6$hash\n")
    out = resolve(["env:K", f"file:{f}"], {"K": "ssh-ed25519 AAAA"})
    assert out == {"env:K": "ssh-ed25519 AAAA", f"file:{f}": "$6$hash"}


def test_resolve_uses_file_map(tmp_path):
    real = tmp_path / "mounted"
    real.write_text("v")
    assert resolve(["file:~/orig"], {}, {"~/orig": str(real)}) == {"file:~/orig": "v"}


def test_resolve_reports_all_missing():
    with pytest.raises(SecretError) as ei:
        resolve(["env:A", "env:B", "file:/nope/x"], {"A": ""})
    msg = str(ei.value)
    assert "A" in msg and "B" in msg and "/nope/x" in msg


def test_file_map_from_env():
    assert file_map_from_env({FILE_MAP_ENV: json.dumps({"a": "/run/osp-secrets/0"})}) == {"a": "/run/osp-secrets/0"}
    assert file_map_from_env({}) == {}


def test_secret_strings_splits_lines():
    assert secret_strings({"r": "key1\n\nkey2\n"}) == ["key1", "key2"]
