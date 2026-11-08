import json
from pathlib import Path

import pytest

from osp.launcher import (
    CONTAINER_STATE_DIR,
    FILE_MAP_ENV,
    IMAGE,
    STATE_VOLUME,
    LauncherError,
    container_command,
    find_engine,
    in_container,
)


def test_in_container_reads_env():
    assert in_container({"OSP_IN_CONTAINER": "1"}) is True
    assert in_container({}) is False


def test_find_engine_prefers_docker_then_podman():
    assert find_engine(lambda n: "/x/docker" if n == "docker" else None) == "docker"
    assert find_engine(lambda n: "/x/podman" if n == "podman" else None) == "podman"
    with pytest.raises(LauncherError, match="docker or podman"):
        find_engine(lambda n: None)


def test_container_command_mounts_repo_and_state(tmp_path):
    cmd = container_command("docker", tmp_path, ["build", "profiles/x.yaml"], [], [], tty=False)
    assert cmd[:4] == ["docker", "run", "--rm", "--privileged"]
    assert cmd[cmd.index(f"{tmp_path}:/work") - 1] == "-v"
    assert f"{STATE_VOLUME}:{CONTAINER_STATE_DIR}" in cmd
    assert "-it" not in cmd
    assert cmd[cmd.index(IMAGE):] == [IMAGE, "python3", "-m", "osp", "build", "profiles/x.yaml"]


def test_container_command_tty():
    assert "-it" in container_command("podman", Path("/r"), ["test", "a.iso"], [], [], tty=True)


def test_container_command_forwards_env_by_name_only(tmp_path):
    cmd = container_command("docker", tmp_path, ["build", "p.yaml"], ["OSP_ADMIN_SSH_KEY"], [], tty=False)
    i = cmd.index("OSP_ADMIN_SSH_KEY")
    assert cmd[i - 1] == "-e"


def test_container_command_mounts_secret_files_read_only(tmp_path):
    secret = tmp_path / "key.pub"
    secret.write_text("ssh-ed25519 AAAA")
    cmd = container_command("docker", tmp_path, ["build", "p.yaml"], [], [str(secret)], tty=False)
    assert f"{secret.resolve()}:/run/osp-secrets/0:ro" in cmd
    env_arg = next(a for a in cmd if a.startswith(f"{FILE_MAP_ENV}="))
    assert json.loads(env_arg.split("=", 1)[1]) == {str(secret): "/run/osp-secrets/0"}
