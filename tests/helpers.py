import copy
from pathlib import Path

import yaml

BASE = {
    "schema_version": 1,
    "name": "t",
    "distro": {"family": "debian", "flavor": "debian", "release": "bookworm", "arch": "amd64"},
}


def profile_data(**overrides) -> dict:
    data = copy.deepcopy(BASE)
    data.update(copy.deepcopy(overrides))
    return data


def write_yaml(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path
