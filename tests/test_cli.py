import types
from pathlib import Path

import pytest

from helpers import profile_data, write_yaml
from osp import cli
from osp.launcher import IMAGE
from osp.run import CommandError


@pytest.fixture
def repo(tmp_path, monkeypatch):
    write_yaml(tmp_path / "profiles" / "p.yaml",
               profile_data(name="p", users=[{"name": "a", "ssh_keys_from": "env:OSP_K"}]))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OSP_IN_CONTAINER", raising=False)
    monkeypatch.setenv("OSP_K", "ssh-ed25519 AAAA")
    return tmp_path


@pytest.fixture
def captured_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "find_engine", lambda: "docker")
    monkeypatch.setattr(cli, "launch", lambda cmd: calls.append(cmd) or 0)
    return calls


def test_parser_build_defaults():
    a = cli.build_parser().parse_args(["build", "p.yaml"])
    assert (a.from_stage, a.no_test, a.keep_failed, a.timeout) == (None, False, True, 300)


def test_profile_show_prints_merged(repo, capsys):
    assert cli.main(["profile", "show", "profiles/p.yaml"]) == 0
    out = capsys.readouterr().out
    assert "name: p" in out and "env:OSP_K" in out and "AAAA" not in out


def test_profile_validate_ok_and_missing_secret(repo, capsys, monkeypatch):
    assert cli.main(["profile", "validate", "profiles/p.yaml"]) == 0
    assert "ok: p" in capsys.readouterr().out
    monkeypatch.delenv("OSP_K")
    assert cli.main(["profile", "validate", "profiles/p.yaml"]) == 2
    assert "OSP_K" in capsys.readouterr().err


def test_invalid_profile_exit_2(repo, capsys):
    write_yaml(repo / "profiles" / "bad.yaml", profile_data(bogus=1))
    assert cli.main(["profile", "validate", "profiles/bad.yaml"]) == 2
    assert "bogus" in capsys.readouterr().err


def test_requires_repo_root(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["profile", "show", "x.yaml"]) == 2
    assert "repository root" in capsys.readouterr().err


def test_build_on_host_launches_container(repo, captured_launch):
    assert cli.main(["build", str(repo / "profiles" / "p.yaml"), "--no-test"]) == 0
    cmd = captured_launch[0]
    assert cmd[cmd.index(IMAGE):] == [IMAGE, "python3", "-m", "osp", "build", "profiles/p.yaml",
                                      "--no-test", "--timeout", "300"]
    assert cmd[cmd.index("OSP_K") - 1] == "-e"
    assert "ssh-ed25519 AAAA" not in " ".join(cmd)


def test_build_on_host_fails_fast_on_missing_secret(repo, captured_launch, monkeypatch):
    monkeypatch.delenv("OSP_K")
    assert cli.main(["build", "profiles/p.yaml"]) == 2
    assert captured_launch == []


def test_test_on_host_forwards_flags(repo, captured_launch):
    (repo / "output").mkdir()
    assert cli.main(["test", "output/x.iso", "--uefi", "--install"]) == 0
    cmd = captured_launch[0]
    assert cmd[cmd.index(IMAGE) + 4:] == ["test", "output/x.iso", "--timeout", "300", "--install", "--uefi"]


@pytest.fixture
def in_container(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("OSP_IN_CONTAINER", "1")
    monkeypatch.setenv("OSP_STATE_DIR", str(tmp_path / "state"))
    calls = []

    def bootstrap(profile, rootfs, runner):
        calls.append("bootstrap")
        rootfs.mkdir(parents=True)

    def fake_customize(profile, secrets, src, rootfs, repo_root, runner):
        calls.append("customize")
        assert secrets == {"env:OSP_K": "ssh-ed25519 AAAA"}
        rootfs.mkdir(parents=True)

    def fake_assemble(profile, rootfs, out_dir, stem, build_hash, runner):
        calls.append("assemble")
        for suffix in (".iso", ".iso.sha256", ".build.json"):
            (out_dir / f"{stem}{suffix}").write_text(suffix)
        return out_dir / f"{stem}.iso"

    backend = types.SimpleNamespace(bootstrap_key=lambda p: "bk", bootstrap=bootstrap)
    monkeypatch.setattr(cli, "get_backend", lambda family: backend)
    monkeypatch.setattr(cli, "customize", fake_customize)
    monkeypatch.setattr(cli, "assemble", fake_assemble)
    return calls


def test_build_in_container_runs_stages_publishes_and_caches(repo, in_container, tmp_path):
    assert cli.main(["build", "profiles/p.yaml", "--no-test"]) == 0
    assert in_container == ["bootstrap", "customize", "assemble"]
    isos = list((repo / "output").glob("p-bookworm-amd64-*.iso"))
    assert len(isos) == 1
    assert isos[0].with_name(isos[0].stem + ".build.json").exists()
    assert (tmp_path / "state" / "cache" / "bootstrap" / "bk" / "stage.json").exists()
    assert cli.main(["build", "profiles/p.yaml", "--no-test"]) == 0
    assert in_container == ["bootstrap", "customize", "assemble"]  # everything cached
    assert cli.main(["build", "profiles/p.yaml", "--no-test", "--from", "assemble"]) == 0
    assert in_container[-1] == "assemble" and len(in_container) == 4


def test_new_day_reruns_only_assemble(repo, in_container, monkeypatch):
    import datetime

    assert cli.main(["build", "profiles/p.yaml", "--no-test"]) == 0
    assert in_container == ["bootstrap", "customize", "assemble"]

    class Tomorrow:
        @staticmethod
        def today():
            return datetime.date.today() + datetime.timedelta(days=1)

    monkeypatch.setattr(cli, "date", Tomorrow)
    assert cli.main(["build", "profiles/p.yaml", "--no-test"]) == 0
    assert in_container == ["bootstrap", "customize", "assemble", "assemble"]


def test_from_rejects_test_stage():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["build", "p.yaml", "--from", "test"])


def test_main_interrupt_exit_130(repo, monkeypatch, capsys):
    def boom(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_profile_show", boom)
    assert cli.main(["profile", "show", "profiles/p.yaml"]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_main_oserror_exit_1(repo, monkeypatch, capsys):
    def boom(args):
        raise PermissionError("denied")

    monkeypatch.setattr(cli, "cmd_profile_show", boom)
    assert cli.main(["profile", "show", "profiles/p.yaml"]) == 1
    assert "denied" in capsys.readouterr().err


def test_stage_failure_prints_log_tail(repo, in_container, monkeypatch, capsys, tmp_path):
    log = tmp_path / "c.log"
    log.write_text("".join(f"line {i}\n" for i in range(50)))

    def failing(*a, **k):
        raise CommandError(["chroot", "x"], 100, log)

    monkeypatch.setattr(cli, "customize", failing)
    assert cli.main(["build", "profiles/p.yaml", "--no-test"]) == 1
    err = capsys.readouterr().err
    assert "stage 'customize' failed" in err and "last 40 lines" in err
    assert "line 49" in err and "line 9\n" not in err


def test_clean_in_container(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("OSP_IN_CONTAINER", "1")
    state = tmp_path / "state"
    monkeypatch.setenv("OSP_STATE_DIR", str(state))
    for d in ("cache/x", "work/y", "test/z"):
        (state / d).mkdir(parents=True)
    assert cli.main(["clean", "--work"]) == 0
    assert (state / "cache").exists() and not (state / "work").exists() and not (state / "test").exists()
    assert cli.main(["clean"]) == 0
    assert not (state / "cache").exists()
