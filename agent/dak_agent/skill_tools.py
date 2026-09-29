"""Client-side skill tools (list_skills / enable_skill) and skill tool loaders.

`make_skill_tools(agent)` returns the two FunctionTools the agent always
exposes; the heavy lifting lives in module-level functions so it can be
unit-tested without a full agent.
"""
import importlib.util
import logging
import os
import sys
from typing import Iterable, List, Optional, Tuple

from google.adk.tools import FunctionTool
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
import httpx

from . import call_config
from .mcp_headers import session_key_header

logger = logging.getLogger(__name__)

# Wallet tools auto-enabled alongside paid-service skills when AP2 is active,
# so the LLM can always check its balance and pay.
WALLET_TOOL_NAMES = ["check_solana_balance", "get_solana_address", "send_sol_payment"]

# Full wallet toolset exposed at the root agent when AP2 is enabled.
ALL_WALLET_TOOL_NAMES = WALLET_TOOL_NAMES + ["verify_sol_payment"]

_SOLANA_WALLET_TOOLS_FILE = os.path.join(
    os.path.dirname(__file__), "..", "skills", "solana_wallet", "tools.py"
)

# Session-state keys (ADK persists `callback_context.state`/`tool_context.state`
# per session, even across process restarts when a database SessionService is
# used — see enforcer.py's PLAN_KEY for the established pattern). AdaptiveAgent
# is a single instance shared by every session, so the enabled skills and the
# mode-switch outcome must live here rather than as mutable attributes on the
# agent itself. `AdaptiveAgent._resolve_session_instruction`/
# `_resolve_session_tools` read these to rebuild the session's effective
# instruction/tools on demand (see `_apply_session_config`).
STATE_ACTIVE_SKILLS = "dak_active_skills"          # List[str]
STATE_MODE_INSTRUCTION = "dak_mode_instruction"    # Optional[str]
STATE_MODE_TOOL_NAMES = "dak_mode_tool_names"      # Optional[List[str]]


def invalidate_canonical_tools_cache(context) -> None:
    """Clear google-adk v2's per-invocation tool cache so a tool/instruction
    rebuild (mode switch, enable_skill, or the turn-start restore) is visible
    for the rest of this invocation. No-op on v1 or when unavailable."""
    try:
        context._invocation_context.canonical_tools_cache = None
    except Exception:
        pass


def _import_module_from_path(module_name: str, file_path: str):
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_local_tools_from_skill(
    skill_name: str,
    skill_dir: str,
    required_tools: Iterable[str],
    current_tool_names: Iterable[str],
) -> Tuple[List[FunctionTool], List[str]]:
    """Load a skill's tools from its local tools.py.

    Returns (loaded FunctionTools, tool names that must fall back to MCP).
    """
    current = set(current_tool_names)
    missing = [name for name in required_tools if name not in current]

    tools_file = os.path.join(skill_dir, "tools.py")
    if not os.path.exists(tools_file):
        return [], missing

    try:
        module = _import_module_from_path(f"skills.{skill_name}.tools", tools_file)
    except Exception as e:
        logger.error(f"Failed to load local tools for {skill_name}: {e}")
        return [], missing

    local_tools: List[FunctionTool] = []
    mcp_fallback: List[str] = []
    for tool_name in missing:
        func = getattr(module, tool_name, None)
        if callable(func):
            # require_confirmation=False allows autonomous execution and the AP2 flow
            local_tools.append(FunctionTool(func, require_confirmation=False))
            logger.info(f"Loaded local tool '{tool_name}' from {skill_name}")
        else:
            if func is not None:
                logger.warning(f"'{tool_name}' in {skill_name} is not callable.")
            mcp_fallback.append(tool_name)
    return local_tools, mcp_fallback


def _no_redirect_http_client(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    """Like the MCP SDK's default client (30 s, 300 s to read a stream), but
    refusing HTTP redirects."""
    return httpx.AsyncClient(headers=headers, auth=auth, follow_redirects=False,
                             timeout=timeout or httpx.Timeout(30.0, read=300.0))


def make_mcp_toolset(
    url: str,
    conn_type: str = "http",
    tool_filter: Optional[List[str]] = None,
    follow_redirects: bool = True,
) -> McpToolset:
    """Create an McpToolset for the given server URL and connection type.
    `follow_redirects=False` for servers the caller chose: an allowed endpoint
    must not be able to send the agent on to another (internal) address."""
    extra = {} if follow_redirects else {"httpx_client_factory": _no_redirect_http_client}
    if conn_type == "sse":
        from google.adk.tools.mcp_tool import SseConnectionParams
        conn_params = SseConnectionParams(url=url, **extra)
    else:
        conn_params = StreamableHTTPConnectionParams(url=url, **extra)
    return McpToolset(
        connection_params=conn_params,
        tool_filter=tool_filter,
        require_confirmation=False,
        header_provider=session_key_header,
    )


def load_solana_wallet_tools(
    existing_tool_names: Iterable[str] = (),
    tool_names: Iterable[str] = WALLET_TOOL_NAMES,
) -> List[FunctionTool]:
    """Load the Solana wallet FunctionTools (for AP2), skipping already-present ones."""
    if not os.path.exists(_SOLANA_WALLET_TOOLS_FILE):
        logger.warning(f"Solana wallet tools not found at {_SOLANA_WALLET_TOOLS_FILE}")
        return []

    try:
        module = _import_module_from_path("skills.solana_wallet.tools", _SOLANA_WALLET_TOOLS_FILE)
    except Exception as e:
        logger.warning(f"Could not load Solana wallet tools: {e}")
        return []

    existing = set(existing_tool_names)
    tools = []
    for name in tool_names:
        if name in existing:
            continue
        func = getattr(module, name, None)
        if callable(func):
            tools.append(FunctionTool(func, require_confirmation=False))
            logger.info(f"Auto-added Solana wallet tool '{name}' for AP2 support")
    return tools


def make_skill_tools(agent) -> List[FunctionTool]:
    """Create the list_skills / enable_skill tools bound to an AdaptiveAgent.

    Thin closures over `agent`: ADK introspects the function signature to build
    the tool schema, so we cannot use bound methods with a `self` parameter.
    """

    async def list_skills() -> str:
        """
        List all available Agent Skills and Remote Tools.
        Returns a list of skills and tools with their names and descriptions.
        """
        await agent.ensure_remote_tools_loaded()

        output = []

        # 1. Local (curated) skills
        if agent.skill_registry:
            skills = agent.skill_registry.list_skills()
            if skills:
                output.append("## Curated Skills (Recommended)")
                output.extend([f"- {s['name']}: {s['description']}" for s in skills])

        # 2. Remote tools (zero-config)
        if agent.available_remote_tools:
            output.append("\n## Individual Remote Tools")
            output.extend([f"- {name}: {desc}" for name, desc in agent.available_remote_tools.items()])

        if not output:
            return "No skills or tools available."

        return "\n".join(output)

    async def enable_skill(skill_name: str, tool_context=None) -> str:
        """
        Enable a specific skill OR an individual remote tool.
        This loads the instructions and makes the tools available.
        """
        if tool_context is not None and call_config.resolve_dak_settings(tool_context).get(
                call_config.STATE_CALL_TOOLS) is not None:
            return ("Error: this call's tools are fixed by the caller (dak:tools); "
                    "enable_skill cannot add tools here.")

        await agent.ensure_remote_tools_loaded()

        if tool_context is None:
            # ADK always injects tool_context for a FunctionTool whose function
            # declares this parameter; without it there is no session to record
            # the change against, so failing loudly beats silently no-op'ing.
            return "Error: enable_skill requires ADK's tool_context to record the change in session state."

        skill = agent.skill_registry.get_skill(skill_name) if agent.skill_registry else None
        active_skills = list(tool_context.state.get(STATE_ACTIVE_SKILLS, []))

        if skill:
            if skill_name in active_skills:
                return f"Skill '{skill_name}' is already active."
            if not agent.skill_registry.find_skill_dir(skill_name):
                logger.warning(f"Skill directory for {skill_name} not found in any configured paths.")
                return f"Error: Skill directory for {skill_name} not found."
        elif skill_name in agent.available_remote_tools:
            if skill_name in active_skills:
                return f"Tool '{skill_name}' is already active."
        else:
            return f"Error: Skill or Tool '{skill_name}' not found."

        active_skills.append(skill_name)
        tool_context.state[STATE_ACTIVE_SKILLS] = active_skills

        # Recompute this session's effective instruction/tools from state (now
        # including `skill_name`) and apply them to the live per-invocation
        # agent. The root/closure `agent` above is never mutated, so this
        # session's skill never leaks into another session's copy.
        agent._apply_session_config(tool_context)

        return f"'{skill_name}' enabled."

    return [
        FunctionTool(list_skills, require_confirmation=False),
        FunctionTool(enable_skill, require_confirmation=False),
    ]
