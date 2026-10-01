"""Plan mode: the agent investigates and writes a plan, but changes nothing else.

Entered with `planner(enter_plan_mode=True)`. While the session-state flag is
set, the permission plugin tightens its decision with `active_rules`: writes
outside `plans/*.md` and every `run_command` are denied. The rules only ever
tighten (they are combined with the configured rules by the strictest result),
so Plan mode never allows a call the configured rules would ask about.
"""
from typing import Any, List, Mapping, MutableMapping

from .permission import Action, Rule, evaluate

PLAN_MODE_KEY = "dak_plan_mode_active"
PLAN_FILE_GLOB = "plans/*.md"

PLAN_MODE_RULES: List[Rule] = [
    Rule("*", "*", "*", "allow"),  # anything not named below: no objection from Plan mode
    *(r for tool in ("write_file", "edit_file") for r in (
        Rule("*", tool, "*", "deny"),
        Rule("*", tool, PLAN_FILE_GLOB, "allow"),
        # fnmatch's `*` also matches `/` and `..`: `plans/../agent/x.md` is not a plan file.
        *(Rule("*", tool, p, "deny") for p in ("../*", "*/../*")),
    )),
    Rule("*", "run_command", "*", "deny"),
]


def is_active(state: MutableMapping) -> bool:
    return bool(state.get(PLAN_MODE_KEY, False))


def enter(state: MutableMapping) -> None:
    state[PLAN_MODE_KEY] = True


def exit_(state: MutableMapping) -> None:
    state[PLAN_MODE_KEY] = False


def active_rules(state: MutableMapping) -> List[Rule]:
    return PLAN_MODE_RULES if is_active(state) else []


def check(state: MutableMapping, source: str, tool_name: str, args: Mapping[str, Any]) -> Action:
    """Plan mode's objection to a call: "deny", or "allow" (none, also when Plan mode is off)."""
    rules = active_rules(state)
    return evaluate(rules, source, tool_name, args) if rules else "allow"
