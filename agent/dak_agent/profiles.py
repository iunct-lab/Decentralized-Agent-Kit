"""Harness profiles: per-model overrides of HarnessSettings.

A profile is a YAML file (``agent/profiles/<name>.yaml``) with a ``pattern``
(a glob on the LiteLLM model name) and HarnessSettings field values. The
profile named by ``DAK_HARNESS_PROFILE`` wins; otherwise the first profile
whose pattern matches the model. ``default`` (pattern ``*``) is tried last.
"""
import fnmatch
import glob
import os
from typing import List, Optional

import yaml

DEFAULT_PROFILE = "default"


def load_profile_files(dirs: List[str]) -> List[dict]:
    """Every ``*.yaml`` in ``dirs`` (missing directories are skipped), each
    with ``name`` set to its file stem. ``default`` comes last so a catch-all
    pattern does not hide the specific ones."""
    found = []
    for directory in dirs:
        for path in sorted(glob.glob(os.path.join(directory, "*.yaml"))):
            with open(path, encoding="utf-8") as f:
                entry = yaml.safe_load(f) or {}
            if not isinstance(entry, dict) or "pattern" not in entry:
                raise ValueError(f"Harness profile {path} has no 'pattern'")
            entry["name"] = os.path.splitext(os.path.basename(path))[0]
            found.append(entry)
    return sorted(found, key=lambda e: e["name"] == DEFAULT_PROFILE)


def resolve_profile(model_name: str, explicit: Optional[str], profiles: List[dict]) -> dict:
    """The profile named ``explicit`` (ValueError if there is none), else the
    first whose pattern matches ``model_name``, else ``{}``."""
    if explicit:
        for entry in profiles:
            if entry["name"] == explicit:
                return entry
        raise ValueError(f"Unknown harness profile {explicit!r} (DAK_HARNESS_PROFILE)")
    for entry in profiles:
        if fnmatch.fnmatch(model_name, entry["pattern"]):
            return entry
    return {}
