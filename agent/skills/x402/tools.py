"""
Tools for the x402 skill: fetch external HTTP resources and report x402 payment requirements.
A 402 answer becomes an Observation; nothing is paid or signed here (System ENABLES, Agent DECIDES).
"""
import dataclasses

import httpx
from google.adk.tools.tool_context import ToolContext

from dak_agent.payments.x402 import format_x402_observation, parse_payment_required

# Session-state key: {url: [X402Requirement as dict, ...]} read from the last 402 of each URL
STATE_X402_REQUIREMENTS = "x402_requirements"


def fetch_url(url: str, tool_context: ToolContext) -> str:
    """
    Fetch an external HTTP resource with a single GET. Redirects are not followed.

    If the resource answers HTTP 402 (x402), nothing is paid or signed: the payment
    requirement (amount, asset, network, payTo, expires_in) is returned instead.

    Args:
        url: The URL to fetch.

    Returns:
        "HTTP <status>" and the body, or the payment requirement for a 402 answer.
    """
    # Redirects are refused: the requirement shown must belong to the URL that would be paid
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        response = client.get(url)
    if response.status_code != 402:
        return f"HTTP {response.status_code}\n{response.text}"

    try:
        reqs = parse_payment_required(response)
    except ValueError as e:
        return f"HTTP 402 from {url}, but its x402 payment requirement could not be read: {e}"
    # Build a new dict and assign it so that ADK records the state change
    stored = dict(tool_context.state.get(STATE_X402_REQUIREMENTS) or {})
    stored[url] = [dataclasses.asdict(r) for r in reqs]
    tool_context.state[STATE_X402_REQUIREMENTS] = stored
    return format_x402_observation(url, reqs)["error"]


X402_TOOLS = [fetch_url]
