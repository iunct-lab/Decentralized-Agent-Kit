import logging
import os
from typing import Any, List, MutableMapping, Tuple, Optional

from . import meta_llm
from .config import get_litellm_model_name

logger = logging.getLogger(__name__)

# Session-state keys (ADK persists `callback_context.state` per session, even
# across process restarts when a database SessionService is used). One
# `ModeManager` is shared by every session in the process, so the mode-switch
# flags themselves must live in session state, not on `self` — otherwise
# session A's first turn / switch_mode request bleeds into session B. Mirrors
# the pattern in `enforcer.py`'s `PLAN_KEY`.
FIRST_TURN_DONE_KEY = "dak_mode_first_turn_done"
SWITCH_REQUESTED_KEY = "dak_mode_switch_requested"
REQUESTED_FOCUS_KEY = "dak_mode_requested_focus"


class ModeManager:
    """
    Manages the "Mode" of the agent.
    A Mode consists of:
    1. A specific System Instruction (Prompt).
    2. A specific set of Allowed Tools.

    Trigger: the `switch_mode` tool runs (after the permission plugin let it
    through). The first model response of a session marks the first turn.

    Context-window pressure is NOT handled here: the context harness
    (harness.py + ADK events compaction) owns that.

    One instance is shared by every session in the process; per-session
    mode-switch flags are passed in explicitly as a `state` mapping
    (`callback_context.state`) rather than stored on `self`.
    """

    # Context-window sizes normally come from litellm's model map (see
    # _lookup_max_tokens). This table only overrides models the pinned litellm
    # doesn't know yet, plus the conservative default for unknown/local models
    # (a too-small value just makes compaction trigger earlier; a too-large
    # one would let the context overflow before it ever fires).
    MODEL_MAX_TOKENS = {
        # Overrides for models newer than the pinned litellm's model map go here
        # (bare name -> context window); drop each once litellm knows it.
        "default": 128000,
    }

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.max_context_tokens = self.resolve_context_window(model_name)

    @classmethod
    def resolve_context_window(cls, model_name: str) -> int:
        """Resolve the context limit, allowing self-hosted endpoints to state it.

        A llama-server alias is intentionally provider-neutral and therefore is
        absent from LiteLLM's model map. Without an explicit override the agent
        could assume 128K while the server was launched with a smaller context.
        """
        configured = os.getenv("MODEL_CONTEXT_WINDOW")
        if configured is not None:
            try:
                value = int(configured)
                if value <= 0:
                    raise ValueError
                return value
            except ValueError:
                logger.warning(
                    "Ignoring invalid MODEL_CONTEXT_WINDOW=%r; expected a positive integer.",
                    configured,
                )
        return cls._lookup_max_tokens(model_name)

    @classmethod
    def _lookup_max_tokens(cls, model_name: str) -> int:
        known = cls.known_context_window(model_name)
        if known is None:
            logger.info(
                f"Model '{model_name}' not in litellm's model map; "
                f"assuming {cls.MODEL_MAX_TOKENS['default']} context tokens."
            )
            return cls.MODEL_MAX_TOKENS["default"]
        return known

    @classmethod
    def known_context_window(cls, model_name: str) -> Optional[int]:
        """The model's context window from the override table or litellm's
        model map, or None when neither knows the model."""
        # The model name may carry a LiteLLM provider prefix
        # (e.g. "gemini/gemini-2.5-flash"); the override table is keyed bare,
        # while litellm resolves prefixed IDs as-is (Bedrock inference
        # profiles included).
        bare_model_name = model_name.rsplit("/", 1)[-1]
        if bare_model_name in cls.MODEL_MAX_TOKENS:
            return cls.MODEL_MAX_TOKENS[bare_model_name]
        try:
            import litellm  # deferred: keeps module import light

            max_input = litellm.get_model_info(model_name).get("max_input_tokens")
            if max_input:  # some entries carry None
                return max_input
        except Exception:
            pass
        return None

    def should_switch(self, state: MutableMapping[str, Any]) -> bool:
        """Decide whether to switch modes: after a model response (marks the
        first turn) and when the switch_mode tool runs.

        Only an explicit `switch_mode` call triggers a switch. The session's
        first model response only marks the first turn; a `switch_mode` call
        in it switches once the tool runs.

        Args:
            state: The current session's state (`callback_context.state`).
        """
        if not state.get(FIRST_TURN_DONE_KEY, False):
            logger.info("First turn: Using default minimal toolset (no mode switch).")
            state[FIRST_TURN_DONE_KEY] = True
            return False

        if state.get(SWITCH_REQUESTED_KEY, False):
            logger.info("Mode Switch Triggered: LLM requested via switch_mode tool")
            state[SWITCH_REQUESTED_KEY] = False
            return True

        return False

    def request_switch(self, state: MutableMapping[str, Any], reason: str, new_focus: str):
        """Called when LLM uses the switch_mode tool."""
        logger.info(f"Switch requested by LLM. Reason: {reason}, New focus: {new_focus}")
        state[SWITCH_REQUESTED_KEY] = True
        state[REQUESTED_FOCUS_KEY] = new_focus

    def consume_requested_focus(self, state: MutableMapping[str, Any]) -> Optional[str]:
        """Return the LLM-requested focus (if any) and clear it, so a later
        threshold-triggered switch doesn't inherit a stale objective."""
        focus = state.get(REQUESTED_FOCUS_KEY)
        state[REQUESTED_FOCUS_KEY] = None
        return focus

    def generate_mode_config(
        self,
        history_summary: str,
        available_tools: List[Any],
        available_skills: List[dict],
        requested_focus: Optional[str] = None
    ) -> Tuple[str, List[Any], List[str]]:
        """
        Generates a new mode configuration (Instruction, Tools, Skills) using a Meta-LLM call.

        Args:
            history_summary: A summary of the conversation so far.
            available_tools: The full list of tools available to the agent.
            available_skills: The list of available skills (metadata).
            requested_focus: If LLM requested a specific focus via switch_mode.

        Returns:
            Tuple[str, List[Any], List[str]]: (New System Instruction, List of Selected Tools, List of Selected Skills)
        """
        
        # Prepare Tool Descriptions for the Meta-Agent
        tool_descriptions = []
        tool_map = {}
        for tool in available_tools:
            name = getattr(tool, 'name', str(tool))
            description = getattr(tool, 'description', "No description")
            tool_descriptions.append(f"- {name}: {description}")
            tool_map[name] = tool

        tools_block = "\n".join(tool_descriptions)

        # Prepare Skill Descriptions
        skill_descriptions = []
        for skill in available_skills:
            name = skill.get("name", "unknown")
            description = skill.get("description", "No description")
            skill_descriptions.append(f"- {name}: {description}")
        
        skills_block = "\n".join(skill_descriptions) if skill_descriptions else "No skills available."

        # Construct the Meta-Prompt
        focus_hint = ""
        if requested_focus:
            focus_hint = f"\n# LLM Requested Focus\n{requested_focus}\n"
        
        meta_prompt = f"""
You are a "Meta-Agent" responsible for optimizing another AI agent's performance.
The current agent is transitioning to a new phase of its task.
You need to create a NEW, focused configuration for this agent to continue the task efficiently.

# Current Context / Goal
{history_summary}
{focus_hint}
# Available Tools
{tools_block}

# Available Skills
Skills are modular capabilities that provide specialized instructions and best practices.
{skills_block}

# Your Task
1. Analyze the current situation. What is the immediate next step?
2. Write a CONCISE System Instruction for the agent to focus ONLY on this next step.
   - The instruction should be specific, not generic.
   - It MUST summarize the relevant past context so the agent knows what happened (older history may be compacted).
   - Do NOT mention "context is full" or "switching modes". Just describe the role and the current objective.
   - **CRITICAL**: Append this standard instruction at the end:
     "If the user requests an action that requires tools you do not currently have, you MUST follow this 2-step process:
      1. Call `list_skills` to see ALL available tools and skills.
      2. Review the list and call `enable_skill(skill_name='...')` or `switch_mode(reason='...', new_focus='...')` to get the correct tools.
      Do NOT guess tool names. Do NOT try to call tools that are not in your list."
3. Select ONLY the strictly necessary tools from the list above.
   - Fewer tools = better focus.
   - ALWAYS include `switch_mode` so the agent can switch again later.
4. Select relevant Skills from the list above.
   - Skills provide specialized instructions (e.g., "git-automation" gives rules for git usage).
   - Select a skill if the task involves that domain.

# Output Format
You must output a JSON object with this structure:
{{
  "instruction": "The new system prompt...",
  "selected_tools": ["tool_name_1", "tool_name_2"],
  "selected_skills": ["skill_name_1"]
}}
"""

        logger.debug("--- META-AGENT PROMPT ---")
        logger.debug(meta_prompt)
        logger.debug("-------------------------")

        try:
            # The Meta-Agent uses the same provider as the main model (via LiteLLM)
            config_data = meta_llm.complete_json(get_litellm_model_name(self.model_name), meta_prompt)

            if not config_data:
                logger.warning("Meta-Agent returned no config. Keeping current configuration.")
                return "Continue with current task.", [], []

            new_instruction = config_data.get("instruction", "Continue with current task.")
            selected_tool_names = config_data.get("selected_tools", [])
            selected_skills = config_data.get("selected_skills", [])

            logger.info(f"Meta-Agent selected tools: {selected_tool_names}")
            logger.info(f"Meta-Agent selected skills: {selected_skills}")

            return new_instruction, selected_tool_names, selected_skills

        except Exception as e:
            logger.error(f"Meta-Agent failed: {e}. Reverting to default configuration.")
            # Fallback: generic instruction, no tool filtering
            return "Continue with current task.", [], []
