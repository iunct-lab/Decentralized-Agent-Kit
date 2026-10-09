"""Root agent assembly: feature flags + tool wiring for `adk web`.

`adk web` prefers the module-level `app` (an ADK App carrying the context
harness: compaction + plugins) and falls back to `root_agent`.
"""
import logging
import os

logger = logging.getLogger(__name__)

from .patches import apply_patches, setup_telemetry

apply_patches()
setup_telemetry()

from google.adk.apps import App
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import FunctionTool
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from pydantic import ConfigDict

from .a2a_peer_manager import get_a2a_sub_agents
from .adaptive_agent import AdaptiveAgent
from .builtin_tools import make_builtin_tools
from .config import get_litellm_model_name, load_agent_config, resolve_model_name
from .enforcer import ENFORCER_INSTRUCTION, enforcer_validator
from .explorer import DELEGATION_INSTRUCTION, make_explorer_tool
from .harness import (
    ContextHarnessPlugin,
    HarnessSettings,
    harness_enabled,
    make_compaction_config,
    make_get_context_remaining_tool,
    make_new_context_tool,
    make_read_tool_output_tool,
)
from .mcp_headers import session_key_header
from .permission import DEFAULT_RULES, PermissionPlugin, load_rules
from .skill_tools import ALL_WALLET_TOOL_NAMES, load_solana_wallet_tools


class PatchedMcpToolset(McpToolset):
    model_config = ConfigDict(arbitrary_types_allowed=True)


# --- MCP server (default) ---
# No require_confirmation here: allow / ask / deny is decided for every tool
# call by PermissionPlugin below.
mcp_url = os.getenv("MCP_SERVER_URL", "http://mcp-server:8000/mcp")
mcp_toolset = PatchedMcpToolset(
    connection_params=StreamableHTTPConnectionParams(url=mcp_url),
    header_provider=session_key_header,
)

# --- Feature flags ---
enforcer_mode = os.getenv("ENABLE_ENFORCER_MODE", "false").lower() == "true"
ap2_enabled = os.getenv("ENABLE_AP2_PROTOCOL", "false").lower() == "true"

# --- Tools ---
root_agent_tools = [mcp_toolset] + make_builtin_tools(enforcer_mode)

if ap2_enabled:
    wallet_tools = load_solana_wallet_tools(tool_names=ALL_WALLET_TOOL_NAMES)
    root_agent_tools.extend(wallet_tools)
    logger.info(f"Added {len(wallet_tools)} Solana wallet tools to root agent")

# --- Instruction & callbacks ---
if enforcer_mode:
    instruction = ENFORCER_INSTRUCTION
    after_model_callback = enforcer_validator
else:
    instruction = os.getenv(
        "AGENT_INSTRUCTION",
        "You are a helpful assistant powered by the Decentralized Agent Kit.",
    )
    after_model_callback = None
instruction = f"{instruction}\n\n{DELEGATION_INSTRUCTION}"

# --- Model ---
model_name = resolve_model_name()
formatted_model_name = get_litellm_model_name(model_name)
model = LiteLlm(model=formatted_model_name)

# --- Read-only investigation sub-agent (its tool output stays out of this session) ---
root_agent_tools.append(make_explorer_tool(mcp_url))

# --- Context harness (compaction + tool-output budget + request guard) ---
use_harness = harness_enabled()
harness_settings = HarnessSettings.from_env(formatted_model_name)
if use_harness:
    root_agent_tools.append(make_read_tool_output_tool(harness_settings.tool_output_chars))
    root_agent_tools.append(make_get_context_remaining_tool(harness_settings, formatted_model_name))
    root_agent_tools.append(make_new_context_tool())
    logger.info(
        "Context harness: window=%d tokens, compaction at %d, tool output <= %d chars",
        harness_settings.context_window,
        harness_settings.compaction_token_threshold,
        harness_settings.tool_output_chars,
    )

# --- A2A sub-agents (Consumer mode) ---
a2a_sub_agents = get_a2a_sub_agents()
if a2a_sub_agents:
    logger.info(f"Loaded {len(a2a_sub_agents)} A2A peer agent(s)")

# --- Skill directories (colon-separated) ---
skills_dirs_env = os.getenv("AGENT_SKILLS_DIRS")
skills_dirs = None
if skills_dirs_env:
    skills_dirs = [d.strip() for d in skills_dirs_env.split(":") if d.strip()]
    logger.info(f"Configured skill directories: {skills_dirs}")

root_agent = AdaptiveAgent(
    model=model,
    name="dak_agent",
    instruction=instruction,
    tools=root_agent_tools,
    sub_agents=a2a_sub_agents if a2a_sub_agents else None,
    after_model_callback=after_model_callback,
    mcp_url=mcp_url,
    skills_dirs=skills_dirs,
)

# --- Tool permissions (always on, harness or not) ---
permission_plugin = PermissionPlugin(DEFAULT_RULES + load_rules(load_agent_config().permission_rules), mcp_url)

app = App(
    name="dak_agent",
    root_agent=root_agent,
    plugins=[permission_plugin] + (
        [ContextHarnessPlugin(harness_settings, formatted_model_name, peers_available=bool(a2a_sub_agents))]
        if use_harness else []),
    events_compaction_config=make_compaction_config(harness_settings, llm=model) if use_harness else None,
)
