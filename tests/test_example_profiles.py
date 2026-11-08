from pathlib import Path

import pytest

from osp.validate import load_profile

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = {
    "rescue": ("debian", "bookworm", "tui"),
    "homelab-node": ("debian", "bookworm", "unattended"),
    "desktop-xfce": ("ubuntu", "noble", "calamares"),
    "kiosk": ("debian", "bookworm", "none"),
}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_example_profile_is_valid(name):
    p = load_profile(REPO / "profiles" / f"{name}.yaml", REPO)
    assert (p.flavor, p.release, p.installer_mode) == EXAMPLES[name]
    assert p.checks, "every example profile declares boot-test checks"
