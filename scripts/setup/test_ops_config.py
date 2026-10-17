"""The root ops.config.json is in the shared kit's shape (run: `uv run --no-project --with pytest pytest scripts -q`)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _config():
    return json.loads((ROOT / "ops.config.json").read_text(encoding="utf-8"))


def test_ops_config_has_the_kit_shape():
    config = _config()

    assert set(config) == {"projectUrl", "envPrefix", "fields", "milestone", "sections", "storyPoints"}
    assert set(config["fields"]) == {"area", "kind", "priority", "phase"}
    assert config["envPrefix"] == "DAK"
    assert re.fullmatch(r"https://github\.com/(orgs|users)/[^/]+/projects/\d+", config["projectUrl"])


def test_area_labels_are_options_of_the_area_field():
    labels = re.findall(r'name: "area:([^"]+)"', (ROOT / ".github/labels.yml").read_text(encoding="utf-8"))

    assert labels
    assert set(labels) <= set(_config()["fields"]["area"])


def test_phase_options_are_the_milestone_phases():
    phases = _config()["fields"]["phase"]

    assert phases == [f"Phase {n}" for n in range(5)]  # Milestones are "Phase 1 — …" … "Phase 4 — …"; Phase 0 is the groundwork
