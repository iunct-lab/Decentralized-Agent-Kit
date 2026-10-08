"""Shared fixtures for the Docker-based integration tests.

Requires the stack started with:
    docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build --wait
"""
import os
import time
import uuid

import httpx
import pytest

AGENT_URL = os.getenv("DAK_AGENT_URL", "http://localhost:8000")
AGENT_ENFORCER_URL = os.getenv("DAK_AGENT_ENFORCER_URL", "http://localhost:8010")
AGENT_AP2_URL = os.getenv("DAK_AGENT_AP2_URL", "http://localhost:8011")
AGENT_HARNESS_EVAL_URL = os.getenv("DAK_AGENT_HARNESS_EVAL_URL", "http://localhost:8012")
AGENT_CONSUMER_URL = os.getenv("DAK_AGENT_CONSUMER_URL", "http://localhost:8013")
AGENT_AP2_INTENT_URL = os.getenv("DAK_AGENT_AP2_INTENT_URL", "http://localhost:8014")
MCP_URL = os.getenv("DAK_MCP_URL", "http://localhost:8001/mcp")
BFF_URL = os.getenv("DAK_BFF_URL", "http://localhost:8002")
FAKE_LLM_URL = os.getenv("DAK_FAKE_LLM_URL", "http://localhost:8089")
INSPECT_SERVER_URL = os.getenv("DAK_INSPECT_SERVER_URL", "http://localhost:8090")
# Real-LLM CI runs (nightly-eval, CPU-only Ollama) can take much longer per
# turn than the fake-LLM suite; overridable so those runs can raise it.
AGENT_RUN_TIMEOUT = float(os.getenv("DAK_AGENT_RUN_TIMEOUT", "120.0"))

APP_NAME = "dak_agent"


def wait_for(url: str, timeout: float = 60.0):
    """Poll a URL until it answers (any HTTP status) or the timeout expires."""
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=5.0)
            return
        except Exception as e:
            last_error = e
            time.sleep(1.0)
    raise RuntimeError(f"Service at {url} not reachable: {last_error}")


@pytest.fixture(scope="session", autouse=True)
def stack_ready():
    wait_for(f"{FAKE_LLM_URL}/health")
    wait_for(f"{INSPECT_SERVER_URL}/health")
    wait_for(f"{AGENT_URL}/list-apps")


class FakeLlm:
    """Control client for the fake LLM service."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def script(self, model: str, responses: list):
        resp = httpx.post(f"{self.base_url}/script/{model}", json={"responses": responses}, timeout=10.0)
        resp.raise_for_status()

    def clear(self, model: str):
        httpx.delete(f"{self.base_url}/script/{model}", timeout=10.0)

    def requests(self, model: str) -> list:
        """What the model received, oldest first: [{"messages": [...], "tools": [...]}]."""
        return httpx.get(f"{self.base_url}/requests/{model}", timeout=10.0).json()

    @staticmethod
    def text(content: str) -> dict:
        return {"text": content}

    @staticmethod
    def tool_call(name: str, **args) -> dict:
        return {"tool_call": {"name": name, "args": args}}

    @staticmethod
    def tool_calls(*calls: dict) -> dict:
        """Parallel calls in one response: `tool_calls(tool_call(...), tool_call(...))`."""
        return {"tool_calls": [c["tool_call"] for c in calls]}


@pytest.fixture
def fake_llm():
    return FakeLlm(FAKE_LLM_URL)


class AgentClient:
    """Minimal client for the ADK REST API."""

    def __init__(self, base_url: str, user_id: str = None):
        self.base_url = base_url
        self.user_id = user_id or f"it_user_{uuid.uuid4().hex[:8]}"

    def create_session(self, state: dict | None = None) -> str:
        resp = httpx.post(
            f"{self.base_url}/apps/{APP_NAME}/users/{self.user_id}/sessions",
            json={"state": state} if state else {},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def run(self, session_id: str, prompt: str) -> list:
        """Send a user message and return the list of ADK events."""
        payload = {
            "app_name": APP_NAME,
            "user_id": self.user_id,
            "session_id": session_id,
            "new_message": {"parts": [{"text": prompt}]},
        }
        resp = httpx.post(f"{self.base_url}/run", json=payload, timeout=AGENT_RUN_TIMEOUT)
        resp.raise_for_status()
        return resp.json()


@pytest.fixture
def agent():
    return AgentClient(AGENT_URL)


@pytest.fixture
def agent_enforcer():
    return AgentClient(AGENT_ENFORCER_URL)


@pytest.fixture
def agent_ap2():
    return AgentClient(AGENT_AP2_URL)


@pytest.fixture
def agent_ap2_intent():
    return AgentClient(AGENT_AP2_INTENT_URL)


@pytest.fixture
def agent_harness_eval():
    return AgentClient(AGENT_HARNESS_EVAL_URL)


@pytest.fixture
def agent_consumer():
    return AgentClient(AGENT_CONSUMER_URL)


# --- Event helpers ---

def event_texts(events: list) -> list:
    texts = []
    for event in events:
        for part in (event.get("content") or {}).get("parts") or []:
            if part.get("text"):
                texts.append(part["text"])
    return texts


def function_calls(events: list) -> list:
    calls = []
    for event in events:
        for part in (event.get("content") or {}).get("parts") or []:
            if part.get("functionCall"):
                calls.append(part["functionCall"])
    return calls


def function_responses(events: list) -> list:
    responses = []
    for event in events:
        for part in (event.get("content") or {}).get("parts") or []:
            if part.get("functionResponse"):
                responses.append(part["functionResponse"])
    return responses


# --- Fake-LLM request helpers (an entry of FakeLlm.requests) ---

def system_instruction(entry: dict) -> str:
    for message in entry["messages"]:
        if message.get("role") == "system":
            return message.get("content") or ""
    return ""


def tool_names(entry: dict) -> list:
    return [t["function"]["name"] for t in entry.get("tools", [])]
