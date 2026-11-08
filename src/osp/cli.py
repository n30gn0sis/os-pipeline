"""osp command line. build/test/clean run inside the builder container; profile commands run anywhere."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date
from pathlib import Path

import yaml

from osp.assemble import assemble
from osp.backends import get_backend
from osp.buildid import artifact_stem, compute_build_hash
from osp.customize import customize
from osp.launcher import LauncherError, container_command, find_engine, in_container, launch
from osp.mounts import safe_rmtree
from osp.profile import ProfileError
from osp.qemu import boot_test, install_test, kvm_available
from osp.run import CommandError, Runner
from osp.secrets import (SecretError, collect_refs, env_names, file_map_from_env, file_paths, resolve,
                         secret_strings)
from osp.stages import STAGES, StageError, run_stage, stages_to_force
from osp.validate import load_profile

FIRMWARES = ("bios", "uefi")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="osp", description="Build purpose-built OS images from profiles.")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build an image from a profile")
    b.add_argument("profile")
    b.add_argument("--from", dest="from_stage", choices=STAGES, help="rerun from this stage, ignoring its cache")
    b.add_argument("--no-test", action="store_true", help="skip the QEMU boot tests")
    b.add_argument("--keep-failed", action=argparse.BooleanOptionalAction, default=True,
                   help="keep a failed stage's work directory for inspection (default: on)")
    b.add_argument("--timeout", type=int, default=300, help="seconds per boot test")
    b.set_defaults(func=cmd_build)

    t = sub.add_parser("test", help="boot-test an ISO in QEMU")
    t.add_argument("iso")
    t.add_argument("--install", action="store_true", help="run the unattended install test")
    fw = t.add_mutually_exclusive_group()
    fw.add_argument("--bios", action="store_true")
    fw.add_argument("--uefi", action="store_true")
    t.add_argument("--timeout", type=int, default=300, help="seconds per boot")
    t.set_defaults(func=cmd_test)

    pr = sub.add_parser("profile", help="inspect profiles")
    psub = pr.add_subparsers(dest="profile_command", required=True)
    s = psub.add_parser("show", help="print the fully merged profile")
    s.add_argument("profile")
    s.set_defaults(func=cmd_profile_show)
    v = psub.add_parser("validate", help="validate a profile and its secrets")
    v.add_argument("profile")
    v.set_defaults(func=cmd_profile_validate)

    c = sub.add_parser("clean", help="remove cached and work state")
    c.add_argument("--cache", action="store_true")
    c.add_argument("--work", action="store_true")
    c.set_defaults(func=cmd_clean)
    return p


def _repo_root() -> Path:
    cwd = Path.cwd()
    if not (cwd / "profiles").is_dir():
        raise LauncherError("run osp from the repository root (no profiles/ directory here)")
    return cwd


def _state_dir(repo: Path) -> Path:
    return Path(os.environ.get("OSP_STATE_DIR") or repo / "output" / "state")


def _repo_relative(path: str, repo: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(repo.resolve()).as_posix()
    except ValueError:
        raise LauncherError(f"{path} must be inside the repository ({repo})") from None


def _launch(repo: Path, osp_args: list[str], envs: list[str], files: list[str]) -> int:
    return launch(container_command(find_engine(), repo, osp_args, envs, files, tty=sys.stdin.isatty()))


def publish(assemble_dir: Path, out_dir: Path, stem: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    for suffix in (".iso", ".iso.sha256", ".build.json"):
        shutil.copy2(assemble_dir / f"{stem}{suffix}", out_dir / f"{stem}{suffix}")
    return out_dir / f"{stem}.iso"


def _run_tests(iso: Path, install: bool, firmwares: list[str], workdir: Path, log_dir: Path,
               runner: Runner, timeout: int) -> int:
    if not kvm_available():
        print("warning: /dev/kvm is not available; QEMU falls back to software emulation "
              "and boot tests will be slow", file=sys.stderr)
    failed = False
    for fw in firmwares:
        if install:
            res = install_test(iso, fw, workdir / fw, log_dir, runner, timeout=timeout)
        else:
            res = boot_test(iso, fw, workdir / fw, log_dir / f"boot-{fw}.log", timeout=timeout)
        label = f"{fw} install" if install else fw
        print(f"[test {label}] {'PASS' if res.ok else 'FAIL'}: {res.reason} "
              f"(passed {len(res.passed)}, failed {res.failed}) log: {res.log_path}")
        failed = failed or not res.ok
    return 1 if failed else 0


def cmd_build(args) -> int:
    repo = _repo_root()
    if not in_container():
        profile = load_profile(args.profile, repo)
        refs = collect_refs(profile)
        resolve(refs, os.environ)  # fail fast on the host, before starting the container
        fwd = ["build", _repo_relative(args.profile, repo)]
        if args.from_stage:
            fwd += ["--from", args.from_stage]
        if args.no_test:
            fwd.append("--no-test")
        if not args.keep_failed:
            fwd.append("--no-keep-failed")
        fwd += ["--timeout", str(args.timeout)]
        return _launch(repo, fwd, env_names(refs), file_paths(refs))
    return _build_in_container(args, repo)


def _build_in_container(args, repo: Path) -> int:
    out, state = repo / "output", _state_dir(repo)
    profile = load_profile(args.profile, repo)
    secrets = resolve(collect_refs(profile), os.environ, file_map_from_env(os.environ))
    build_hash = compute_build_hash(profile, repo, secrets)
    stem = artifact_stem(profile, build_hash, date.today())
    logs = out / "logs" / stem
    base = Runner(secrets=secret_strings(secrets))
    force = stages_to_force(args.from_stage)
    backend = get_backend(profile.family)
    bkey = backend.bootstrap_key(profile)
    work = state / "work" / stem

    def stage(name, key, out_dir, fn):
        log = logs / f"{name}.log"
        return run_stage(name, key, out_dir, lambda d: fn(d, base.with_log(log)),
                         force=name in force, keep_failed=args.keep_failed, log_path=log)

    boot_dir = stage("bootstrap", bkey, state / "cache" / "bootstrap" / bkey,
                     lambda d, r: backend.bootstrap(profile, d / "rootfs", r))
    cust_dir = stage("customize", f"{build_hash}:{bkey}", work / "customize",
                     lambda d, r: customize(profile, secrets, boot_dir / "rootfs", d / "rootfs", repo, r))
    asm_dir = stage("assemble", build_hash, work / "assemble",
                    lambda d, r: assemble(profile, cust_dir / "rootfs", d, stem, build_hash, r))
    iso = publish(asm_dir, out, stem)
    print(f"built {iso}")
    if args.no_test:
        return 0
    return _run_tests(iso, False, list(FIRMWARES), state / "test" / stem, logs,
                      base.with_log(logs / "test.log"), args.timeout)


def cmd_test(args) -> int:
    repo = _repo_root()
    if not in_container():
        fwd = ["test", _repo_relative(args.iso, repo), "--timeout", str(args.timeout)]
        if args.install:
            fwd.append("--install")
        if args.bios:
            fwd.append("--bios")
        if args.uefi:
            fwd.append("--uefi")
        return _launch(repo, fwd, [], [])
    iso = Path(args.iso)
    firmwares = ["bios"] if args.bios else ["uefi"] if args.uefi else list(FIRMWARES)
    logs = repo / "output" / "logs" / iso.stem
    return _run_tests(iso, args.install, firmwares, _state_dir(repo) / "test" / iso.stem, logs,
                      Runner(log_path=logs / "test.log"), args.timeout)


def cmd_profile_show(args) -> int:
    profile = load_profile(args.profile, _repo_root())
    print(yaml.safe_dump(profile.data, sort_keys=False), end="")
    return 0


def cmd_profile_validate(args) -> int:
    profile = load_profile(args.profile, _repo_root())
    resolve(collect_refs(profile), os.environ, file_map_from_env(os.environ))
    print(f"ok: {profile.name}")
    return 0


def cmd_clean(args) -> int:
    repo = _repo_root()
    if not in_container():
        fwd = ["clean"] + (["--cache"] if args.cache else []) + (["--work"] if args.work else [])
        return _launch(repo, fwd, [], [])
    state = _state_dir(repo)
    both = not (args.cache or args.work)
    targets = []
    if args.cache or both:
        targets.append(state / "cache")
    if args.work or both:
        targets += [state / "work", state / "test"]
    for target in targets:
        if target.exists():
            safe_rmtree(target)
            print(f"removed {target}")
    return 0


def _print_log_tail(log_path) -> None:
    if log_path and Path(log_path).is_file():
        tail = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        print(f"--- last {len(tail)} lines of {log_path} ---", file=sys.stderr)
        print("\n".join(tail), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ProfileError, SecretError, LauncherError) as e:
        print(f"osp: {e}", file=sys.stderr)
        return 2
    except (StageError, CommandError) as e:
        print(f"osp: {e}", file=sys.stderr)
        _print_log_tail(e.log_path)
        return 1
    except RuntimeError as e:  # e.g. safe_rmtree refusing to delete a mounted tree
        print(f"osp: {e}", file=sys.stderr)
        return 1
