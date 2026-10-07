"""The root ops.config.json describes DAK's Project: only the values DAK has of its own
(run: `uv run --no-project --with pytest pytest scripts -q`)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _config():
    return json.loads((ROOT / "ops.config.json").read_text(encoding="utf-8"))


def test_ops_config_has_only_dak_values():
    config = _config()

    assert set(config) == {"projectUrl", "fields"}
    assert set(config["fields"]) == {"area", "phase"}
    assert re.fullmatch(r"https://github\.com/(orgs|users)/[^/]+/projects/\d+", config["projectUrl"])


def test_area_labels_are_options_of_the_area_field():
    labels = re.findall(r'name: "area:([^"]+)"', (ROOT / ".github/labels.yml").read_text(encoding="utf-8"))

    assert labels
    assert set(labels) <= set(_config()["fields"]["area"])


def test_phase_options_are_numbered_from_zero():
    phases = _config()["fields"]["phase"]

    assert phases == [f"Phase {n}" for n in range(len(phases))]
