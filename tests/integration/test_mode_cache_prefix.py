"""What a mode switch does to the request prefix, seen in what the model receives
(PBI #105). A prompt cache only reuses the leading part of a request that is
identical to the previous one; a switch regenerates the system instruction and
selects a new tool set, so the prefix changes. This pins the current behaviour
as the material for #92 (whether to keep mode switching); the tests do not make
the prefix stable."""
import json
import os

from conftest import function_calls

MODEL = "fake-default"


def _system_instruction(request: dict) -> str:
    messages = request["messages"]
    assert messages and messages[0]["role"] == "system", messages[:1]
    content = messages[0]["content"]
    return content if isinstance(content, str) else "".join(c.get("text", "") for c in content)


def _tool_names(request: dict) -> list:
    return [tool["function"]["name"] for tool in request.get("tools") or []]


def test_mode_switch_changes_system_instruction_and_tool_set(agent, fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [
        fake_llm.tool_call("switch_mode", reason="focus", new_focus="read files"),
        # mode_manager's meta call reads the same scripted model
        fake_llm.text(json.dumps({"instruction": "Focus on reading files.",
                                  "selected_tools": ["read_file", "switch_mode"], "selected_skills": []})),
        fake_llm.text("ok"),
    ])

    events = agent.run(agent.create_session(), "Look at the files.")

    assert "switch_mode" in [c["name"] for c in function_calls(events)], f"events: {events}"
    # The meta call carries no tools; the agent's requests always do.
    requests = [r for r in fake_llm.requests(MODEL) if r.get("tools")]
    assert len(requests) == 2, [_tool_names(r) for r in requests]
    before, after = requests

    system_before, system_after = _system_instruction(before), _system_instruction(after)
    shared = len(os.path.commonprefix([system_before, system_after]))
    # An observation, not an assertion (#92 reads it): how much of the system
    # instruction a prefix cache could still reuse after the switch.
    print(f"system instruction: before {len(system_before)} chars, after {len(system_after)} chars, "
          f"common prefix {shared} chars")
    assert "Focus on reading files." in system_after

    tools_before, tools_after = _tool_names(before), _tool_names(after)
    print(f"tools before: {sorted(tools_before)}\ntools after: {sorted(tools_after)}")
    # The switch keeps the way back: switch_mode stays callable.
    assert "switch_mode" in tools_after
    # Today a switch changes the tool set, so the cached prefix (system +
    # tool definitions) breaks. If this starts failing, the prefix became
    # stable and #92's material changed.
    assert set(tools_before) != set(tools_after)
