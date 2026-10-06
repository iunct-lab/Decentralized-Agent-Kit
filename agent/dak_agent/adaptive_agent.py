"""AdaptiveAgent: an LlmAgent with Dynamic Mode Switching and Agent Skills."""
import asyncio
import difflib
import json
import logging
import os
import re
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Tuple

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.utils import instructions_utils
from google.genai import types
from pydantic import ConfigDict, Field, PrivateAttr
import inspect

from . import builtin_tools, call_config, remote_tools, skill_tools
from .config import get_litellm_model_name, load_agent_config
from .errors import PaymentRequiredError
from .harness import HarnessSettings
from .handlers.payment_handler import PaymentHandler
from .mode_manager import ModeManager
from .skill_registry import SkillRegistry

logger = logging.getLogger(__name__)

# What google-adk returns for a tool call the user rejected (function_tool.py / mcp_tool.py).
REJECTED_TOOL_CALL = {"error": "This tool call is rejected."}

# Last-resort bound on listing one caller MCP server's tools. ADK's own
# connection timeout (5 s, retried once) normally ends it first; cancelling
# ADK mid-connection can orphan a session, so this stays well above that.
CALLER_MCP_PROBE_TIMEOUT_S = 30.0
# Per-invocation (ADK drops `temp:` state after the invocation): the tool names
# each reachable caller MCP server listed on this call, {url: [names]}.
STATE_CALLER_MCP_TOOLS = "temp:dak_caller_mcp_tools"
# What DAK asks the model when its reply failed `dak:output_schema` / `dak:inspection`.
INSPECTION_RETRY_PROMPT = ("Your previous response failed validation.\nValidation errors:\n{errors}\n\n"
                           "Produce a corrected response only, in the exact format required.")


class AdaptiveAgent(LlmAgent):
    """
    A wrapper around LlmAgent that implements Dynamic Mode Switching and Agent Skills.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Override tools field to allow toolset instances
    tools: List[Any] = []

    skill_registry: Optional[SkillRegistry] = Field(default=None, exclude=True)
    available_remote_tools: Dict[str, str] = Field(default_factory=dict, exclude=True)

    _mode_manager: ModeManager = PrivateAttr()
    _all_available_tools: List[Any] = PrivateAttr()
    _builtin_tools: List[Any] = PrivateAttr()  # FunctionTools that never get filtered
    _has_default_mcp_toolset: bool = PrivateAttr(default=False)
    _mcp_toolset_cache: Dict[Tuple[str, str, frozenset], Any] = PrivateAttr(default_factory=dict)
    _base_instruction: str = PrivateAttr(default="")
    _original_callback: Optional[Any] = PrivateAttr(default=None)
    _disable_mode_switching: bool = PrivateAttr(default=False)
    _mcp_url: str = PrivateAttr(default="")
    _mcp_servers: Dict[str, Dict] = PrivateAttr(default_factory=dict)
    _active_skills: List[str] = PrivateAttr(default_factory=list)
    _payment_handler: Optional[PaymentHandler] = PrivateAttr(default=None)
    _enable_ap2: bool = PrivateAttr(default=False)
    _base_model_name: str = PrivateAttr(default="")
    _llm_model_cache: Dict[str, Any] = PrivateAttr(default_factory=dict)

    def __init__(
        self,
        model: str,
        name: str,
        instruction: str,
        tools: List[Any],
        sub_agents: Optional[List[Any]] = None,
        after_model_callback: Optional[Any] = None,
        disable_mode_switching: bool = False,
        mcp_url: Optional[str] = None,
        skills_dirs: Optional[List[str]] = None,
    ):
        # Split provided tools into built-in FunctionTools and MCP toolsets.
        # The agent starts with ONLY built-in tools; MCP tools are enabled via
        # skills or mode switching to keep the initial context small.
        all_tools = list(tools) + skill_tools.make_skill_tools(self)

        builtin_tools = []
        for tool in all_tools:
            if "Toolset" not in type(tool).__name__:
                builtin_tools.append(tool)
        logger.info("Initializing with minimal toolset (Client-Side Skills + Built-in)")

        init_kwargs = {
            "model": model,
            "name": name,
            "instruction": instruction,
            "tools": builtin_tools,
            "before_agent_callback": self._restore_session_config,
            "after_model_callback": self._wrapped_callback,
            "on_tool_error_callback": self._on_tool_error,
            "after_tool_callback": self._restore_reject_reason,
        }
        if sub_agents:
            init_kwargs["sub_agents"] = sub_agents
            logger.info(f"Initializing with {len(sub_agents)} A2A sub-agent(s)")

        super().__init__(**init_kwargs)

        # Skill registry. Default skills dir is agent/skills; AGENT_SKILLS_DIRS
        # (resolved by the caller) may add more.
        current_dir = os.path.dirname(__file__)
        if not skills_dirs:
            skills_dirs = [os.path.abspath(os.path.join(current_dir, "..", "skills"))]
        else:
            skills_dirs = [d if os.path.isabs(d) else os.path.abspath(d) for d in skills_dirs]

        self.skill_registry = SkillRegistry(skills_dirs)
        self.skill_registry.load_skills()
        self._active_skills = []

        # Remote tool metadata is lazy-loaded on first use (cannot await here).
        self.available_remote_tools = {}

        model_name_str = model if isinstance(model, str) else getattr(model, "model", str(model))
        self._base_model_name = model_name_str
        self._mode_manager = ModeManager(model_name=model_name_str)
        self._all_available_tools = all_tools
        self._builtin_tools = builtin_tools
        self._has_default_mcp_toolset = any("Toolset" in type(t).__name__ for t in all_tools)
        # `self.instruction`/`self.tools` are never reassigned after this point:
        # google-adk v2 runs each invocation on a shallow copy of this (single,
        # process-wide) agent, so mutating them here would leak one session's
        # enabled skills/mode into every other session's copy. Session-specific
        # config is rebuilt from `callback_context.state` onto the live copy by
        # `_apply_session_config` instead (see `_restore_session_config`,
        # `skill_tools.enable_skill`, `_perform_mode_switch`).
        self._base_instruction = instruction
        self._original_callback = after_model_callback
        self._disable_mode_switching = disable_mode_switching

        self._mcp_url = mcp_url or os.getenv("MCP_SERVER_URL", "http://mcp-server:8000/mcp")
        self._mcp_servers = load_agent_config().mcp_servers

        # AP2 Protocol feature flag (experimental)
        self._enable_ap2 = os.getenv("ENABLE_AP2_PROTOCOL", "false").lower() == "true"
        if self._enable_ap2:
            logger.info("AP2 Protocol ENABLED via ENABLE_AP2_PROTOCOL=true")
            try:
                self._payment_handler = PaymentHandler()
            except Exception as e:
                logger.warning(f"Failed to initialize PaymentHandler: {e}")
                self._payment_handler = None
        else:
            logger.info("AP2 Protocol DISABLED (set ENABLE_AP2_PROTOCOL=true to enable)")

        logger.info(f"AdaptiveAgent initialized with MCP URL: {self._mcp_url}")

    # --- Accessors used by skill_tools closures ---

    @property
    def active_skills(self) -> List[str]:
        return self._active_skills

    @property
    def mcp_servers(self) -> Dict[str, Dict]:
        return self._mcp_servers

    @property
    def mcp_url(self) -> str:
        return self._mcp_url

    @property
    def ap2_enabled(self) -> bool:
        return self._enable_ap2

    async def ensure_remote_tools_loaded(self):
        """Lazy-load remote MCP tool metadata if not already loaded."""
        if self.available_remote_tools:
            return
        self.available_remote_tools = await remote_tools.discover_remote_tools(self._mcp_url)

    # --- Session-scoped config (instruction/tools/active_skills) ---
    #
    # One AdaptiveAgent instance is shared by every session in the process,
    # and google-adk v2 hands each invocation a shallow copy of it. `self`
    # (this root instance) is therefore never mutated past __init__: doing so
    # would leak one session's enabled skills/mode into every other session's
    # copy. Instead, the enabled skills and mode-switch outcome are recorded
    # in `callback_context.state` (ADK persists this per session, across
    # process restarts too, when a database SessionService is used — same
    # pattern as `enforcer.py`'s PLAN_KEY), and `_apply_session_config`
    # recomputes the effective instruction/tools from that state and applies
    # them onto the live per-invocation copy.

    def _resolve_session_instruction(
        self, state: MutableMapping[str, Any], call_settings: Dict[str, Any]
    ) -> str:
        """Rebuild this session's system instruction from its state. A
        per-call `dak:instruction` replaces it entirely."""
        call_instruction = call_settings.get(call_config.STATE_CALL_INSTRUCTION)
        if call_instruction:
            return call_instruction

        mode_instruction = state.get(skill_tools.STATE_MODE_INSTRUCTION)
        instruction = mode_instruction if mode_instruction else self._base_instruction

        for skill_name in state.get(skill_tools.STATE_ACTIVE_SKILLS, []):
            skill = self.skill_registry.get_skill(skill_name) if self.skill_registry else None
            if skill and skill.get("instructions"):
                instruction += f"\n\n# Skill: {skill_name}\n{skill['instructions']}"
            elif skill_name in self.available_remote_tools:
                instruction += (
                    f"\n\n# Tool Enabled: {skill_name}\n"
                    f"You have enabled the raw tool '{skill_name}'. Use it according to its schema."
                )
        return instruction + self._tools_error_section(state) + self._verbatim_sections(state)

    def _verbatim_sections(self, state: MutableMapping[str, Any]) -> str:
        """The instruction's tail built from text the user or the model wrote
        (the plan, the original request, the handoff): sent as is, never
        through ADK's `{var}` session-state injection."""
        return self._plan_section(state) + self._original_request_section(state) + self._handoff_section(state)

    @staticmethod
    def _tools_error_section(state: MutableMapping[str, Any]) -> str:
        """Tell the model which of the caller's tools are unavailable on this
        call, so it can answer or try something else instead of failing."""
        errors = state.get(call_config.STATE_TOOLS_ERROR)
        # DAK writes it, but a caller can also set any `dak:` key: ignore junk.
        if not isinstance(errors, list) or not errors or not all(
                isinstance(e, Mapping) and isinstance(e.get("url"), str) for e in errors):
            return ""
        lines = "\n".join(f"- {e.get('url')} ({e.get('reason')})" for e in errors)
        return f"\n\n# Unavailable tools\nThese tool servers could not be reached on this call:\n{lines}"

    async def _probe_caller_mcp_servers(self, state: MutableMapping[str, Any], servers: List[Dict[str, str]]) -> None:
        """List the tools of each caller MCP server once per call. Uses the
        shared, cached toolset for the server (bounded by the operator's
        allow-list) rather than a new connection per call. An unreachable
        server is recorded in `dak:tools_error`; the turn goes on without it."""
        async def probe(server):
            toolset = self._cached_mcp_toolset(server["url"], server["type"], (), follow_redirects=False)
            try:
                tools = await asyncio.wait_for(toolset.get_tools(), timeout=CALLER_MCP_PROBE_TIMEOUT_S)
            except Exception as e:
                reason = "timed out" if isinstance(e, asyncio.TimeoutError) else (str(e) or type(e).__name__)
                logger.warning(f"Caller MCP server {server['url']} is unreachable: {reason}")
                return server["url"], None, {"url": server["url"], "reason": f"unreachable: {reason}"[:300]}
            return server["url"], sorted({getattr(t, "name", "") for t in tools} - {""}), None

        # Concurrently: several servers that are down must not add up.
        results = await asyncio.gather(*(probe(s) for s in servers))
        listed = {url: names for url, names, _ in results if names is not None}
        errors = [error for _, _, error in results if error]
        state[STATE_CALLER_MCP_TOOLS] = listed
        if errors or state.get(call_config.STATE_TOOLS_ERROR):
            state[call_config.STATE_TOOLS_ERROR] = errors or None

    def _plan_section(self, state: MutableMapping[str, Any]) -> str:
        """The session's plan (`write_todos`), rebuilt from state every turn so
        compaction of the event history never loses it. Sent with every
        request, so capped by the (startup) window; read_plan has it all."""
        todos = state.get(builtin_tools.STATE_TODOS)
        if not isinstance(todos, list) or not todos:
            return ""
        max_chars = HarnessSettings(context_window=self._mode_manager.max_context_tokens).plan_chars
        return f"\n\n# Current Plan\n{builtin_tools.format_todos(todos, max_chars=max_chars)}"

    def _original_request_section(self, state: MutableMapping[str, Any]) -> str:
        """The session's first user message, rebuilt from state every turn so
        compaction of the event history never loses it (the summary's own
        `User request` depends on the model). Capped like the plan."""
        request = state.get(builtin_tools.STATE_ORIGINAL_REQUEST)
        if not isinstance(request, str) or not request:
            return ""
        max_chars = HarnessSettings(context_window=self._mode_manager.max_context_tokens).plan_chars
        if len(request) > max_chars:
            marker = "\n[truncated — call read_original_request for the full text]"
            request = request[: max_chars - len(marker)] + marker
        return f"\n\n# Original Request\n{request}"

    def _handoff_section(self, state: MutableMapping[str, Any]) -> str:
        """The session's handoff (`write_handoff`), rebuilt from state every
        turn, so a session resumed after a reset, over A2A or by another
        process starts from it. Capped like the plan; read_handoff has it all."""
        handoff = state.get(builtin_tools.STATE_HANDOFF)
        if not isinstance(handoff, dict) or not handoff:
            return ""
        text = builtin_tools.format_handoff(handoff)
        max_chars = HarnessSettings(context_window=self._mode_manager.max_context_tokens).plan_chars
        if len(text) > max_chars:
            marker = "\n[truncated — call read_handoff for the full text]"
            text = text[: max_chars - len(marker)] + marker
        return f"\n\n# Handoff\n{text}"

    @staticmethod
    def _capture_original_request(context: CallbackContext) -> None:
        """Keep the text of the session's first user message, once."""
        state = context.state
        if state.get(builtin_tools.STATE_ORIGINAL_REQUEST):
            return
        content = context.user_content
        text = "\n".join(p.text for p in (content.parts or []) if p.text) if content else ""
        if text:
            state[builtin_tools.STATE_ORIGINAL_REQUEST] = text

    def _resolve_session_tools(
        self, state: MutableMapping[str, Any], call_settings: Dict[str, Any]
    ) -> List[Any]:
        """Rebuild this session's tool list from its state. A per-call
        `dak:tools` (list of names) replaces it: only those built-in tools,
        plus those names from the default MCP server."""
        call_tools = call_settings.get(call_config.STATE_CALL_TOOLS)
        if isinstance(call_tools, list):
            return self._select_tools_by_name(call_tools)
        if isinstance(call_tools, Mapping):
            if "mcp_servers" not in call_tools:
                return self._select_tools_by_name(list(call_tools.get("names") or []))
            servers, _ = call_config.resolve_caller_mcp_servers(call_settings)
            names = call_tools.get("names")
            if names is not None and not names:
                return []  # "names": [] means no tools, as the list form does
            listed = state.get(STATE_CALLER_MCP_TOOLS) or {}
            # Only the caller's servers that answered this call's probe; none
            # of ours, and no fallback when they are all down. Names are
            # matched against what the server listed, so arbitrary names never
            # add toolsets (and connections) to the cache. A refused or
            # malformed entry yields no tools (the call is also refused before
            # any model call by `_restore_session_config`).
            tools = []
            for s in servers or []:
                if s["url"] not in listed:
                    continue
                chosen = set(listed[s["url"]]) & set(names) if names else set()
                if names and not chosen:
                    continue
                tools.append(self._cached_mcp_toolset(s["url"], s["type"], chosen, follow_redirects=False))
            return tools

        active_skills = list(state.get(skill_tools.STATE_ACTIVE_SKILLS, []))
        tools = list(self._builtin_tools)
        current_names = {getattr(t, "name", None) for t in tools} - {None}
        mcp_groups: Dict[Tuple[str, str], set] = {}

        def add_mcp_names(names, server_cfg: Optional[Dict] = None):
            missing = [n for n in names if n not in current_names]
            if not missing:
                return
            target_url = server_cfg.get("url") if server_cfg else self._mcp_url
            target_type = server_cfg.get("type", "http") if server_cfg else "http"
            if target_url:
                mcp_groups.setdefault((target_url, target_type), set()).update(missing)

        for skill_name in active_skills:
            skill = self.skill_registry.get_skill(skill_name) if self.skill_registry else None
            if skill:
                skill_dir = self.skill_registry.find_skill_dir(skill_name)
                if not skill_dir:
                    logger.warning(f"Skill directory for {skill_name} not found in any configured paths.")
                    continue
                local_tools, mcp_fallback = skill_tools.load_local_tools_from_skill(
                    skill_name, skill_dir, skill.get("tools", []), current_names
                )
                for tool in local_tools:
                    name = getattr(tool, "name", None)
                    if name and name not in current_names:
                        tools.append(tool)
                        current_names.add(name)
                server_cfg = self.mcp_servers.get(skill["mcp_server"]) if skill.get("mcp_server") else None
                add_mcp_names(mcp_fallback, server_cfg)
            elif skill_name in self.available_remote_tools:
                add_mcp_names([skill_name])

        if skill_tools.STATE_MODE_TOOL_NAMES in state:
            mode_tool_names = state.get(skill_tools.STATE_MODE_TOOL_NAMES) or []
            add_mcp_names(mode_tool_names)
            if not mode_tool_names and not mcp_groups and self._has_default_mcp_toolset:
                # A mode switch selected no tools; fall back to the full,
                # unfiltered default MCP server rather than stranding the agent.
                mcp_groups[(self._mcp_url, "http")] = set()

        for (url, conn_type), names in mcp_groups.items():
            tools.append(self._cached_mcp_toolset(url, conn_type, names))

        if self.ap2_enabled and "solana_wallet" not in active_skills and any(
            s != "solana_wallet" for s in active_skills
        ):
            tools.extend(skill_tools.load_solana_wallet_tools(current_names))

        return tools

    def _select_tools_by_name(self, tool_names: List[Any]) -> List[Any]:
        """`dak:tools` as a list: the named built-in tools, and the remaining
        names filtered from the default MCP server (a name it does not have
        simply matches nothing). An empty list means no tools at all."""
        wanted = {str(n) for n in tool_names}
        tools = [t for t in self._builtin_tools if getattr(t, "name", None) in wanted]
        # Only names the default MCP server really has (loaded by
        # `_restore_session_config`): the toolset cache is keyed by the name
        # set, and each cached toolset keeps a connection, so arbitrary
        # caller-chosen names must not create new entries.
        rest = (wanted - {getattr(t, "name", None) for t in tools}) & set(self.available_remote_tools)
        if rest and self._has_default_mcp_toolset:
            tools.append(self._cached_mcp_toolset(self._mcp_url, "http", rest))
        return tools

    def _cached_mcp_toolset(self, url: str, conn_type: str, names, follow_redirects: bool = True) -> Any:
        """One McpToolset per (server, tool filter), shared by every session.
        Each McpToolset owns an MCP session manager whose connection is only
        released by that same manager, so building a fresh one per turn would
        leak a connection per turn. The filter is never mutated after
        creation, so sharing across sessions is safe. Caller-chosen servers
        get `follow_redirects=False` (a separate cache entry)."""
        key = (url, conn_type, frozenset(names)) + (() if follow_redirects else ("no-redirects",))
        toolset = self._mcp_toolset_cache.get(key)
        if toolset is None:
            if follow_redirects:
                toolset = skill_tools.make_mcp_toolset(url, conn_type, sorted(names) or None)
            else:
                toolset = skill_tools.make_mcp_toolset(url, conn_type, sorted(names) or None, follow_redirects=False)
            self._mcp_toolset_cache[key] = toolset
        return toolset

    def _model_for(self, model_name: str) -> LiteLlm:
        """One LiteLlm per model id, shared by every session (same reason as
        `_cached_mcp_toolset`: do not rebuild it on every turn)."""
        llm = self._llm_model_cache.get(model_name)
        if llm is None:
            llm = LiteLlm(model=get_litellm_model_name(model_name))
            self._llm_model_cache[model_name] = llm
        return llm

    def _live_agent(self, context: CallbackContext) -> "AdaptiveAgent":
        """The per-invocation copy google-adk v2 actually runs. Falls back to
        `self` only when the context carries no invocation agent (unit tests
        calling these methods directly); in production that would write this
        session's config onto the shared root, so it is logged."""
        try:
            live = context._invocation_context.agent
        except Exception:
            live = None
        if live is None:
            logger.warning("No live invocation agent on context; applying session config to the root agent.")
            return self
        return live

    def _apply_session_config(self, context: CallbackContext) -> Optional[Dict[str, Any]]:
        """Recompute this session's instruction/tools/active_skills from
        `context.state` and apply them onto the live per-invocation agent.
        Returns an error dict when the call asked for a model the operator
        does not allow; the caller must then stop before any model call."""
        state = context.state
        live = self._live_agent(context)
        call_settings = call_config.resolve_dak_settings(context)
        model_name, model_error = call_config.resolve_model_selection(call_settings, self._base_model_name)
        tools_error = call_config.validate_call_tools(call_settings) or call_config.validate_call_inspection(call_settings)
        instruction = self._resolve_session_instruction(state, call_settings)
        verbatim = self._verbatim_sections(state)
        if call_settings.get(call_config.STATE_CALL_INSTRUCTION):
            # A provider (callable) makes ADK skip `{var}` session-state
            # injection, so the caller's text reaches the model verbatim
            # (`{date}` in it would otherwise fail the turn with a KeyError).
            live.instruction = lambda _ctx, text=instruction: text
        elif verbatim:
            # The plan and the original request are model- or user-written
            # text: keep them out of `{var}` injection (same KeyError), but
            # still inject the operator's instruction.
            templated = instruction[: -len(verbatim)]

            async def with_verbatim(ctx, templated=templated, verbatim=verbatim):
                return await instructions_utils.inject_session_state(templated, ctx) + verbatim

            live.instruction = with_verbatim
        else:
            live.instruction = instruction
        # None (unspecified) keeps free-form text/tool-call responses. ADK puts
        # it on the request as `response_schema` (LiteLlm supports it
        # alongside tools).
        live.output_schema = call_settings.get(call_config.STATE_CALL_OUTPUT_SCHEMA)
        live.tools = [] if tools_error else self._resolve_session_tools(state, call_settings)
        call_tools = call_settings.get(call_config.STATE_CALL_TOOLS)
        if call_tools is not None and call_config.TRANSFER_TOOL not in (call_config.call_tool_names(call_tools) or []):
            # ADK adds transfer_to_agent from sub_agents on its own; drop the
            # A2A peers for this call unless the caller named that tool.
            live.sub_agents = []
        live._active_skills = list(state.get(skill_tools.STATE_ACTIVE_SKILLS, []))
        skill_tools.invalidate_canonical_tools_cache(context)
        if model_error or tools_error:
            return model_error or tools_error
        if call_settings.get(call_config.STATE_CALL_MODEL) is not None:
            live.model = self._model_for(model_name)
        return None

    async def _restore_session_config(self, callback_context: CallbackContext) -> Optional[types.Content]:
        """`before_agent_callback`: runs once at the start of every invocation,
        before any model call. Without this, a session resumed on a fresh
        invocation (a new turn, or a brand-new AdaptiveAgent instance in a
        redeployed process) would start from this shared instance's static
        construction-time defaults, forgetting skills/mode enabled earlier in
        the same session.

        Returning Content ends the invocation there, before any model call:
        used to refuse a `dak:model` the operator does not allow."""
        call_settings = call_config.resolve_dak_settings(callback_context)
        call_tools = call_settings.get(call_config.STATE_CALL_TOOLS)
        if call_tools is not None and not (isinstance(call_tools, Mapping) and "mcp_servers" in call_tools):
            try:
                await self.ensure_remote_tools_loaded()  # names for `dak:tools`
            except Exception as e:
                logger.warning(f"Could not list the default MCP tools: {e}")
        servers, _ = call_config.resolve_caller_mcp_servers(call_settings)
        refused = (call_config.resolve_model_selection(call_settings, self._base_model_name)[1]
                   or call_config.validate_call_tools(call_settings))
        if servers and not refused:  # no connections for a call that will be refused
            await self._probe_caller_mcp_servers(callback_context.state, servers)
        elif callback_context.state.get(call_config.STATE_TOOLS_ERROR):
            callback_context.state[call_config.STATE_TOOLS_ERROR] = None  # stale: not about this call
        try:
            self._capture_original_request(callback_context)
            error = self._apply_session_config(callback_context)
        except Exception as e:
            logger.error(f"CRITICAL ERROR restoring session config: {e}", exc_info=True)
            # Still refuse what the operator does not allow (fail closed).
            call_settings = call_config.resolve_dak_settings(callback_context)
            _, error = call_config.resolve_model_selection(call_settings, self._base_model_name)
            error = error or call_config.validate_call_tools(call_settings) or call_config.validate_call_inspection(call_settings)
            if call_settings.get(call_config.STATE_CALL_TOOLS) is not None:
                self._live_agent(callback_context).tools = []  # the caller restricted tools: none, not all
        if error:
            logger.info(f"Refusing call: {error}")
            return types.Content(role="model", parts=[types.Part(text=json.dumps(error))])
        return None

    # --- Callbacks ---

    def _on_tool_error(self, tool, args: dict, tool_context, error: Exception) -> Optional[dict]:
        """
        Gracefully turn tool errors into observations for the LLM.

        AP2 Protocol: a PaymentRequiredError becomes a structured payment
        observation. The LLM decides whether to pay - we never auto-pay.
        """
        tool_name = getattr(tool, "name", str(tool)) if tool else "unknown"

        if self._enable_ap2 and isinstance(error, PaymentRequiredError) and self._payment_handler:
            logger.info(f"AP2: Payment Required for {tool_name}: {error.price} {error.currency}")
            return self._payment_handler.format_payment_error(tool_name, error)

        # ADK hands an unregistered name here as a stand-in tool (functions.py `_get_tool`).
        if (isinstance(error, ValueError) and "not found" in str(error) and tool is not None
                and getattr(tool, "description", "") == "Tool not found"):
            live_tools = tool_context._invocation_context.agent.tools
            names = [n for n in (getattr(t, "name", None) or getattr(t, "__name__", None) for t in live_tools)
                     if isinstance(n, str)]
            # Tools inside a Toolset (MCP) are only known by the names ADK resolved and lists in the error.
            listed = re.search(r"^Available tools: (.*)$", str(error), re.MULTILINE)
            if listed:
                names += [n for n in listed.group(1).split(", ") if n and n not in names]
            matches = difflib.get_close_matches(tool_name, names, n=3, cutoff=0.4)
            if "list_skills" in names:
                hint = ("Call one of the candidates, or list_skills to see everything available." if matches
                        else "Call list_skills to see the available tools.")
            else:
                hint = "Call one of the candidates." if matches else "Call one of the tools you were given."
            logger.warning(f"Unknown tool called: {tool_name} (candidates: {matches})")
            return {"observation": "unknown_tool", "tool": tool_name, "candidates": matches, "hint": hint}

        error_msg = str(error)
        logger.warning(f"Tool error caught: {tool_name} - {error_msg}")
        return {"error": f"Tool '{tool_name}' failed: {error_msg}"}

    def _restore_reject_reason(self, tool, args: dict, tool_context, tool_response) -> Optional[dict]:
        """
        ADK answers a rejected confirmation with a fixed text and drops the
        reason. The reply put it in the confirmation payload
        (`approvals.build_reply_function_response`), so hand it to the model
        as an observation. Anything else is left as is.
        """
        confirmation = getattr(tool_context, "tool_confirmation", None)
        if tool_response != REJECTED_TOOL_CALL or confirmation is None or confirmation.confirmed:
            return None
        payload = confirmation.payload if isinstance(confirmation.payload, dict) else {}
        if payload.get("mode") == "reject":
            return {"observation": "denied_by_user", "reason": payload.get("reason", "")}
        if payload.get("mode") == "timed_out":
            return {"observation": "timed_out"}
        return None

    async def _wrapped_callback(
        self, llm_response: LlmResponse, callback_context: CallbackContext
    ) -> Optional[LlmResponse]:
        """Run the user callback (e.g. Enforcer), then apply mode-switching logic."""
        try:
            # 1. Original callback first (e.g. Enforcer validation)
            if self._original_callback:
                if inspect.iscoroutinefunction(self._original_callback):
                    result = await self._original_callback(
                        llm_response=llm_response, callback_context=callback_context
                    )
                else:
                    result = self._original_callback(
                        llm_response=llm_response, callback_context=callback_context
                    )
                if result is not None:
                    logger.info("Enforcer blocked response")
                    return result

            # 2. A final reply must pass the call's `dak:output_schema` and
            #    `dak:inspection`, or is regenerated. ADK's own schema check
            #    needs `output_key`, which DAK does not use.
            call_settings = call_config.resolve_dak_settings(callback_context)
            inspected = await self._retry_on_inspection_failure(llm_response, callback_context, call_settings)
            if inspected is not None:
                return inspected

            # 3. Mark the session's first turn (ModeManager.should_switch). The
            #    switch itself happens when the switch_mode tool runs
            #    (`apply_switch_request`), after the permission plugin. Context-
            #    window pressure is handled by the context harness, not here.
            if not self._disable_mode_switching and self._mode_manager.should_switch(callback_context.state):
                await self._perform_mode_switch(callback_context)

            return None
        except Exception as e:
            logger.error(f"CRITICAL ERROR in _wrapped_callback: {e}", exc_info=True)
            return None

    async def _retry_on_inspection_failure(
        self, llm_response: LlmResponse, callback_context: CallbackContext, call_settings: Dict[str, Any]
    ) -> Optional[LlmResponse]:
        """Check a final text reply against this call's `dak:output_schema` and
        `dak:inspection`; regenerate a failing one with the errors attached,
        up to the turn's `max_llm_calls` attempts in all (else
        DEFAULT_INSPECTION_RETRIES). Returns None (nothing to check, or the
        first reply passed), the regenerated reply that passed, or a reply
        carrying the structured failure."""
        schema = call_settings.get(call_config.STATE_CALL_OUTPUT_SCHEMA)
        inspection = call_settings.get(call_config.STATE_CALL_INSPECTION)
        content = llm_response.content
        if (schema is None and inspection is None) or llm_response.partial or not content or not content.parts:
            return None
        if any(getattr(part, "function_call", None) for part in content.parts):
            return None
        max_attempts = call_config.resolve_call_limits(call_settings).max_llm_calls or call_config.DEFAULT_INSPECTION_RETRIES
        response, attempt = llm_response, 1
        while True:
            parts = (response.content.parts if response.content else None) or []
            text = "".join(part.text for part in parts if part.text and not part.thought)
            try:
                # `{}` accepts any JSON value: without a schema, only parse.
                parsed, issues = call_config.validate_call_output(schema if schema is not None else {}, text)
                if not issues and inspection is not None:
                    issues = await call_config.run_inspection(inspection, parsed)
            except Exception as e:  # fail closed: never let an unchecked reply through
                logger.error(f"Reply inspection crashed: {e}", exc_info=True)
                issues = [{"path": "", "message": f"validation error: {e}"}]
            if not issues:
                return None if response is llm_response else response
            logger.info(f"Reply failed inspection (attempt {attempt}/{max_attempts}): {issues}")
            if attempt >= max_attempts:
                break
            try:
                response = await self._regenerate_reply(callback_context, schema, text, issues)
            except Exception as e:  # incl. ADK's own LLM call limit: fail closed
                logger.error(f"Regenerating a reply that failed inspection crashed: {e}", exc_info=True)
                issues = issues + [{"path": "", "message": f"regeneration failed: {e}"}]
                break
            attempt += 1
        failure = {"error": "inspection_failed" if inspection is not None else "output_schema_validation_failed",
                   "attempts": attempt, "issues": issues, "last_response": text}
        return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=json.dumps(failure))]))

    async def _regenerate_reply(
        self, callback_context: CallbackContext, schema: Optional[Dict[str, Any]], text: str, issues: list
    ) -> LlmResponse:
        """One more model call, outside ADK's tool loop (no tools): the turn's
        user message, the failed reply, and its errors."""
        live = self._live_agent(callback_context)
        instruction, bypass_state_injection = await live.canonical_instruction(callback_context)
        if not bypass_state_injection:
            instruction = await instructions_utils.inject_session_state(instruction, callback_context)
        prompt = INSPECTION_RETRY_PROMPT.format(errors=json.dumps(issues, ensure_ascii=False))
        contents = [c for c in (callback_context.user_content,) if c is not None] + [
            types.Content(role="model", parts=[types.Part(text=text)]),
            types.Content(role="user", parts=[types.Part(text=prompt)]),
        ]
        model = live.canonical_model
        request = LlmRequest(model=model.model, contents=contents,
                             config=types.GenerateContentConfig(system_instruction=instruction))
        if schema is not None:
            request.set_output_schema(schema)
        callback_context._invocation_context.increment_llm_call_count()  # a retry is a model call
        async for response in model.generate_content_async(request, stream=False):
            if response.content:
                return response
        return LlmResponse()  # no reply: inspected (and failed) like any other

    async def apply_switch_request(self, tool_context, reason: str, new_focus: str) -> None:
        """Switch modes for a `switch_mode` call. Called by the tool itself
        while it runs, so only a call the permission plugin let through
        (allowed, or approved) switches, whatever the after-tool callbacks do
        with its result (#410)."""
        self._mode_manager.request_switch(tool_context.state, reason=reason, new_focus=new_focus)
        if not self._disable_mode_switching and self._mode_manager.should_switch(tool_context.state):
            await self._perform_mode_switch(tool_context)

    def _extract_history_summary(self, context: CallbackContext) -> str:
        """Extract a short summary of the recent conversation history."""
        try:
            messages = []
            for content in self._session_contents(context)[-5:]:
                for part in getattr(content, "parts", []) or []:
                    text = getattr(part, "text", None)
                    if text:
                        messages.append(text[:100])
            if messages:
                return " | ".join(messages)
        except Exception as e:
            logger.warning(f"Could not extract history: {e}")
        return "Conversation in progress."

    @staticmethod
    def _session_contents(context: CallbackContext) -> list:
        """Contents of the session's events (ADK sessions store `events`)."""
        session = getattr(context, "session", None)
        events = getattr(session, "events", None) if session is not None else None
        if not isinstance(events, list):
            return []
        return [event.content for event in events if getattr(event, "content", None) is not None]

    async def _perform_mode_switch(self, context: CallbackContext):
        """
        Executes the mode switch:
        1. Generates a new config (instruction + tool/skill selection) via the Meta-Agent.
        2. Rebuilds the toolset: built-ins + a filtered McpToolset.

        Session history is left intact; the context harness compacts it.
        """
        try:
            logger.info("Initiating Mode Switch...")

            history_summary = self._extract_history_summary(context)
            requested_focus = self._mode_manager.consume_requested_focus(context.state)

            # Expand MCP toolsets into individual tools so the Meta-Agent can see them
            expanded_available_tools = []
            for tool in self._all_available_tools:
                if "Toolset" in type(tool).__name__:
                    if hasattr(tool, "tool_filter"):
                        tool.tool_filter = None  # clear filter to see all tools
                    try:
                        mcp_tools = await tool.get_tools()
                        expanded_available_tools.extend(mcp_tools)
                        logger.info(f"Fetched {len(mcp_tools)} tools from MCP server.")
                    except Exception as e:
                        logger.error(f"Failed to fetch tools from McpToolset: {e}")
                        expanded_available_tools.append(tool)
                else:
                    expanded_available_tools.append(tool)

            # Available skills: curated + zero-config remote tools
            available_skills = []
            if self.skill_registry:
                try:
                    available_skills = self.skill_registry.list_skills()
                except Exception as e:
                    logger.error(f"Failed to list skills from registry: {e}")
            for tool_name, desc in self.available_remote_tools.items():
                available_skills.append({"name": tool_name, "description": f"[Remote Tool] {desc}"})

            new_instruction, selected_tool_names, selected_skills = self._mode_manager.generate_mode_config(
                history_summary,
                expanded_available_tools,
                available_skills,
                requested_focus,
            )

            # A mode switch replaces the session's active skills with the
            # meta-agent's selection (a new, focused mode drops the previous
            # mode's skills). `_resolve_session_instruction`/
            # `_resolve_session_tools` append each active skill's
            # instructions/tools on every rebuild, so they are not added here.
            active_skills: List[str] = []
            for skill_name in selected_skills or []:
                if skill_name in active_skills:
                    continue
                is_known_skill = self.skill_registry and self.skill_registry.get_skill(skill_name)
                if is_known_skill or skill_name in self.available_remote_tools:
                    active_skills.append(skill_name)
                else:
                    logger.warning(f"Skill '{skill_name}' selected but not found.")

            context.state[skill_tools.STATE_ACTIVE_SKILLS] = active_skills
            context.state[skill_tools.STATE_MODE_INSTRUCTION] = new_instruction
            context.state[skill_tools.STATE_MODE_TOOL_NAMES] = list(selected_tool_names or [])

            self._apply_session_config(context)
            live_tools = self._live_agent(context).tools
            logger.info(f"Updated agent tools: {[t.name for t in live_tools if hasattr(t, 'name')]}")

        except Exception as e:
            # Never crash the agent on a failed switch
            logger.error(f"CRITICAL ERROR in _perform_mode_switch: {e}", exc_info=True)

        logger.info("Mode Switch Complete.")
