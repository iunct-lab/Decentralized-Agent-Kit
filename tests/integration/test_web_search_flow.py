"""Web search E2E (PBI #330): with TAVILY_API_KEY the agent enables web_search,
calls tavily_search on (fake) Tavily with the key as a Bearer header, and the
results reach the model. Without the key the skill is not there."""
import httpx

from conftest import FAKE_TAVILY_URL, function_calls, function_responses

MODEL = "fake-websearch"
KEY = "test-dummy-key"  # agent-websearch's TAVILY_API_KEY in docker-compose.test.yml


def _list_skills(client, fake_llm, model):
    fake_llm.clear(model)
    fake_llm.script(model, [fake_llm.tool_call("list_skills"), fake_llm.text("Listed.")])
    events = client.run(client.create_session(), "What skills do you have?")
    return str(next(r for r in function_responses(events) if r.get("name") == "list_skills").get("response", {}))


def test_web_search_skill_listed_with_key(agent_websearch, fake_llm):
    assert "web_search" in _list_skills(agent_websearch, fake_llm, MODEL)


def test_web_search_round_trip(agent_websearch, fake_llm):
    fake_llm.clear(MODEL)
    fake_llm.script(MODEL, [
        fake_llm.tool_call("enable_skill", skill_name="web_search"),
        fake_llm.tool_call("tavily_search", query="DAK"),
        fake_llm.text("Found it."),
    ])

    events = agent_websearch.run(agent_websearch.create_session(), "Search the web for DAK")

    assert [c["name"] for c in function_calls(events)] == ["enable_skill", "tavily_search"]
    search = next(r for r in function_responses(events) if r.get("name") == "tavily_search")
    assert "https://fake-tavily.example/dak" in str(search.get("response", {}))
    # The model's next request carries the search result.
    assert "https://fake-tavily.example/dak" in str(fake_llm.requests(MODEL)[-1]["messages"])
    auth = httpx.get(f"{FAKE_TAVILY_URL}/last-auth", timeout=10.0).json()["authorization"]
    assert auth == f"Bearer {KEY}"


def test_web_search_not_listed_without_key(agent, fake_llm):
    assert "web_search" not in _list_skills(agent, fake_llm, "fake-default")
