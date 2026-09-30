"""Context-engineering harness: keep every model request inside the context window.

Three layers, cheapest first:

0. Tool-call guard (``ContextHarnessPlugin.before_tool_callback``): a tool
   call that repeats the previous one too often, or comes past the
   invocation's tool-call or wall-time limit, is not run; the model gets an
   observation saying why instead (never an exception).
1. Tool-output budget (``ContextHarnessPlugin.after_tool_callback``): an
   oversized tool result is replaced by a head/tail preview and the full text
   is offloaded to an artifact the agent can page through with
   ``read_tool_output`` (the "evict large tool results" pattern of Deep Agents).
2. ADK token-threshold compaction (``EventsCompactionConfig``): before each
   model call ADK summarizes older session events once the last observed prompt
   crossed ``token_threshold`` - this works *within* a single long invocation.
   The summary is produced by ``BudgetedEventSummarizer``, which sizes its own
   request to the window (ADK's summarizer renders every thought verbatim, so
   with a reasoning model its prompt can be 2-3x the prompt it is compacting)
   and never raises: a failed compaction is skipped or replaced by an excerpt,
   so it can no longer wedge a session.
3. Request guard (``ContextHarnessPlugin.before_model_callback``): keeps a
   user turn in the request after compaction (chat templates require one),
   clears old tool results without a summary (``prune_old_tool_results``: the
   results before the last few user turns, beyond a token budget, become a
   pointer to an artifact ``read_tool_output`` can read) and, as a last resort, elides the oldest tool payloads when the assembled request
   would still overflow (e.g. the summarizer failed).
4. Overflow recovery (``ContextHarnessPlugin.on_model_error_callback``): when
   the model still rejects a request for size (the estimate was off, or the
   system instruction alone is huge), the request is retried a bounded number
   of times on a halved budget; if it never fits, the turn ends with an
   explanation (``CONTEXT_OVERFLOW_FAILURE_TEXT``) instead of an exception, so
   the session takes the next user turn as usual.

All limits derive from the model's context window (``MODEL_CONTEXT_WINDOW`` or
LiteLLM's model map), so a llama.cpp server launched with 8K gets tight budgets
while a 1M-token Gemini model is left mostly alone.
"""
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, MutableMapping, Optional

from google.adk.apps.app import EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions, EventCompaction
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import FunctionTool
from google.genai import types

from .config import get_litellm_model_name
from .mode_manager import ModeManager

logger = logging.getLogger(__name__)

READ_TOOL_OUTPUT_NAME = "read_tool_output"

# Fraction of the context window a single tool result may occupy. Several
# results usually accumulate before compaction can run, so keep it small.
_TOOL_OUTPUT_WINDOW_FRACTION = 0.15
_MIN_TOOL_OUTPUT_CHARS = 2_000
_MAX_TOOL_OUTPUT_CHARS = 40_000
# Fraction of the window the session's plan may occupy in the instruction
# (it is sent with every request).
_PLAN_WINDOW_FRACTION = 0.05
_MIN_PLAN_CHARS = 1_000
_MAX_PLAN_CHARS = 8_000
# Fraction of the window the newest tool results before the protected user
# turns may keep occupying before older ones are pruned.
_PRUNE_PROTECT_WINDOW_FRACTION = 0.2

_PRUNED_TEMPLATE = "[cleared; read_tool_output('{artifact}') で再取得可]"

_ELIDED_TEMPLATE = (
    "[elided {chars} chars to fit the context window; call the tool again "
    "(or read_tool_output) if you still need this data]"
)

COMPACTION_PROMPT_TEMPLATE = (
    "You are compacting the working memory of an AI agent that is in the middle "
    "of a task. The raw history below will be replaced by your summary, so keep "
    "everything the agent needs to continue without redoing work.\n"
    "Write the summary with these sections:\n"
    "1. User request: restate the user's original request verbatim, plus any "
    "later corrections.\n"
    "2. Progress: what has been done, which files/tools/sources were already "
    "examined, and the key facts or findings obtained (keep exact paths, names, "
    "numbers and identifiers).\n"
    "3. Decisions: choices made and why.\n"
    "4. Remaining work: open questions and the next concrete steps.\n"
    "Be concise; drop raw tool output once its findings are captured. Keep the "
    "whole summary to a few hundred words. Output only the summary itself, with "
    "no preamble.\n\n"
    "{conversation_history}"
)

# One rendered history entry (a thought, a tool call or a tool response) in the
# compaction prompt. Dense entries (user turns, model answers, the previous
# summary) get several times this.
_ENTRY_WINDOW_FRACTION = 0.05
_MIN_ENTRY_CHARS = 400
_MAX_ENTRY_CHARS = 2_000
_SHRUNK_ENTRY_CHARS = 200
_DENSE_ENTRY_MULTIPLIER = 4
_COMPACTION_ATTEMPTS = 3
_DENSE_KINDS = frozenset({"user", "text"})

# The turn's answer when a request still overflows after every retry. The
# leading marker lets clients tell it from an ordinary answer.
CONTEXT_OVERFLOW_FAILURE_TEXT = (
    "[CONTEXT_OVERFLOW]\nThis request could not be processed even after compaction and "
    "budget tightening. Start a new session, or narrow the request.\n"
)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate.

    ~4 ASCII chars per token for code/English, but CJK text is closer to one
    token per character; a plain ``len // 4`` underestimates Japanese prompts
    by 3-4x, which is exactly how an 8K local model overflows.
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars // 4 + (len(text) - ascii_chars)


def _env_float(name: str, default: float, low: float, high: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
        if not low < value <= high:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s.", name, raw, default)
        return default


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value < minimum:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s.", name, raw, default)
        return default


@dataclass(frozen=True)
class HarnessSettings:
    """Context budgets, all derived from the model's context window."""

    context_window: int
    compaction_threshold_ratio: float = 0.6
    compaction_retain_events: int = 4
    compaction_interval: int = 20
    request_budget_ratio: float = 0.85
    tool_output_max_chars: Optional[int] = None
    compaction_input_ratio: float = 0.5
    model_error_retry_attempts: int = 2
    max_repeated_tool_calls: int = 3
    max_invocation_tool_calls: int = 40
    max_wall_seconds: float = 300.0
    prune_protect_tokens: Optional[int] = None
    prune_protect_user_turns: int = 2
    prune_minimum_tokens: int = 512

    @classmethod
    def from_env(cls, model_name: str) -> "HarnessSettings":
        return cls(
            context_window=ModeManager.resolve_context_window(model_name),
            compaction_threshold_ratio=_env_float("DAK_COMPACTION_THRESHOLD_RATIO", 0.6, 0.0, 1.0),
            compaction_retain_events=_env_int("DAK_COMPACTION_RETAIN_EVENTS", 4, 0),
            compaction_interval=_env_int("DAK_COMPACTION_INTERVAL", 20, 1),
            request_budget_ratio=_env_float("DAK_REQUEST_BUDGET_RATIO", 0.85, 0.0, 1.0),
            tool_output_max_chars=_env_int("DAK_TOOL_OUTPUT_MAX_CHARS", 0, 0) or None,
            compaction_input_ratio=_env_float("DAK_COMPACTION_INPUT_RATIO", 0.5, 0.0, 1.0),
            model_error_retry_attempts=_env_int("DAK_MODEL_ERROR_RETRY_ATTEMPTS", 2, 0),
            max_repeated_tool_calls=_env_int("DAK_MAX_REPEATED_TOOL_CALLS", 3, 1),
            max_invocation_tool_calls=_env_int("DAK_MAX_TOOL_CALLS", 40, 1),
            max_wall_seconds=_env_float("DAK_MAX_WALL_SECONDS", 300.0, 0.0, float("inf")),
            prune_protect_tokens=_env_int("DAK_PRUNE_PROTECT_TOKENS", 0, 0) or None,
            prune_protect_user_turns=_env_int("DAK_PRUNE_PROTECT_USER_TURNS", 2, 0),
            prune_minimum_tokens=_env_int("DAK_PRUNE_MINIMUM_TOKENS", 512, 0),
        )

    @property
    def compaction_token_threshold(self) -> int:
        return max(1, int(self.context_window * self.compaction_threshold_ratio))

    @property
    def compaction_input_tokens(self) -> int:
        """Budget for the history rendered into one compaction request; the rest
        of the window is left for the summary itself."""
        return max(256, int(self.context_window * self.compaction_input_ratio))

    @property
    def compaction_entry_chars(self) -> int:
        budget = int(self.context_window * _ENTRY_WINDOW_FRACTION)
        return max(_MIN_ENTRY_CHARS, min(_MAX_ENTRY_CHARS, budget))

    @property
    def request_token_budget(self) -> int:
        return max(1, int(self.context_window * self.request_budget_ratio))

    @property
    def tool_output_chars(self) -> int:
        if self.tool_output_max_chars:
            return self.tool_output_max_chars
        # Budget in tokens -> chars, pessimistically at ~1 char/token so that
        # CJK output also fits.
        budget = int(self.context_window * _TOOL_OUTPUT_WINDOW_FRACTION)
        return max(_MIN_TOOL_OUTPUT_CHARS, min(_MAX_TOOL_OUTPUT_CHARS, budget))

    @property
    def prune_protect_token_budget(self) -> int:
        if self.prune_protect_tokens:
            return self.prune_protect_tokens
        return max(1, int(self.context_window * _PRUNE_PROTECT_WINDOW_FRACTION))

    @property
    def plan_chars(self) -> int:
        """Budget for the plan injected into the instruction (`# Current Plan`),
        in chars at the same pessimistic ~1 char/token as tool output."""
        budget = int(self.context_window * _PLAN_WINDOW_FRACTION)
        return max(_MIN_PLAN_CHARS, min(_MAX_PLAN_CHARS, budget))


def harness_enabled() -> bool:
    return os.getenv("DAK_CONTEXT_HARNESS", "true").lower() != "false"


def is_context_overflow_error(exc: BaseException) -> bool:
    """True if a model error means "the request does not fit the window"."""
    if "ContextWindowExceeded" in type(exc).__name__:
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "exceeds the available context size",  # llama.cpp
            "context_length_exceeded",  # OpenAI
            "maximum context length",
            "context window exceeded",
            "prompt is too long",  # Anthropic
            "input token count exceeds",  # Gemini
        )
    )


@dataclass
class _HistoryEntry:
    kind: str  # user | text | thought | call | response
    text: str
    event_index: int = 0


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


class BudgetedEventSummarizer(LlmEventSummarizer):
    """``LlmEventSummarizer`` that fits its own request to the window and never raises.

    Why this exists: ADK's summarizer renders every thought of every event
    verbatim and calls the model outside the agent's callback chain, so the
    request guard never sees it. With a reasoning model (Qwen3 thinking) the
    compaction prompt grew to 50K tokens on a 32K window while the prompt it was
    meant to shrink was 20K. The resulting ``ContextWindowExceededError`` was
    re-raised on every following turn (the trigger condition never changes),
    which wedged the session permanently.

    Behaviour:

    * Each history entry is capped; bulky kinds (thoughts, tool calls, tool
      responses) get ``compaction_entry_chars``, dense kinds (user turns, model
      answers, the previous rolling summary) several times that.
    * The rendered history is fitted to ``compaction_input_tokens`` by shrinking
      bulky entries first, then dense ones, then dropping the oldest bulky
      entries, then the oldest dense ones. The first dense entry (original
      request or rolling summary) is never dropped.
    * If the model still rejects the request for size, the budget is halved and
      retried; after ``_COMPACTION_ATTEMPTS`` the fitted excerpt itself becomes
      the summary so the session keeps moving.
    * Any other model failure is logged and compaction is skipped for this call
      (returns ``None``): the request guard keeps the next model call inside the
      window, and the failure surfaces there if it is persistent.
    """

    def __init__(self, llm: Any, settings: HarnessSettings, prompt_template: Optional[str] = None):
        super().__init__(llm=llm, prompt_template=prompt_template or COMPACTION_PROMPT_TEMPLATE)
        self.settings = settings

    # -- rendering -----------------------------------------------------------

    def _history_entries(self, events: list[Event]) -> list[_HistoryEntry]:
        entries: list[_HistoryEntry] = []
        for index, event in enumerate(events):
            if not (event.content and event.content.parts):
                continue
            is_compaction = bool(event.actions and event.actions.compaction)
            for part in event.content.parts:
                if part.thought and part.text:
                    if not is_compaction:
                        self._append_text(entries, index, "thought", f"{event.author} (thought): ", part.text)
                elif part.text:
                    kind = "user" if event.author == "user" else "text"
                    self._append_text(entries, index, kind, f"{event.author}: ", part.text)
                if part.function_call:
                    call = part.function_call
                    entries.append(_HistoryEntry(
                        "call", f"{event.author} called tool: {call.name}({call.args})", index))
                if part.function_response:
                    response = part.function_response
                    entries.append(_HistoryEntry(
                        "response", f"Tool response from {response.name}: {response.response}", index))
        return entries

    @staticmethod
    def _append_text(entries: list[_HistoryEntry], index: int, kind: str, prefix: str, text: str) -> None:
        """Add a text part, merging it into the previous entry when that came from
        the same event and kind. Streaming stores a model's reasoning as hundreds
        of tiny thought parts per event; rendering each on its own prefixed line
        (as ADK does) turned 23K chars of thought into 176K chars of prompt in
        the wedged session. Separate events stay separate entries.
        """
        last = entries[-1] if entries else None
        if last is not None and last.event_index == index and last.kind == kind:
            last.text += text
        else:
            entries.append(_HistoryEntry(kind, prefix + text, index))

    def _fit_history(self, entries: list[_HistoryEntry], budget_tokens: int) -> list[str]:
        """Render `entries` under `budget_tokens` (estimated), losing bulk before facts."""
        entry_cap = self.settings.compaction_entry_chars
        dense_cap = entry_cap * _DENSE_ENTRY_MULTIPLIER
        kept = list(entries)

        def render() -> list[str]:
            return [_cap(e.text, dense_cap if e.kind in _DENSE_KINDS else entry_cap) for e in kept]

        def size(rendered: list[str]) -> int:
            return sum(estimate_tokens(r) + 1 for r in rendered)

        rendered = render()
        while size(rendered) > budget_tokens:
            if entry_cap > _SHRUNK_ENTRY_CHARS:
                entry_cap = max(_SHRUNK_ENTRY_CHARS, entry_cap // 2)
            elif dense_cap > entry_cap:
                dense_cap = max(entry_cap, dense_cap // 2)
            else:
                # Drop the oldest bulky entry; if none is left, the oldest dense
                # entry after the first one (the request / rolling summary).
                bulky = [i for i, e in enumerate(kept) if e.kind not in _DENSE_KINDS]
                if bulky and len(kept) > 1:
                    del kept[bulky[0]]
                elif len(kept) > 2:
                    del kept[1]
                else:
                    break  # a single entry, or the first dense entry + the newest one
            rendered = render()
        dropped = len(entries) - len(kept)
        if dropped:
            logger.warning(
                "Context harness: compaction dropped %d oldest history entries to fit %d tokens", dropped, budget_tokens)
        return rendered

    # -- model call ----------------------------------------------------------

    async def _summarize(self, history: str):
        prompt = self._prompt_template.format(conversation_history=history)
        llm_request = LlmRequest(
            model=self._llm.model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
        )
        async for llm_response in self._llm.generate_content_async(llm_request, stream=False):
            if llm_response.content:
                return llm_response.content, llm_response.usage_metadata
        return None, None

    @staticmethod
    def _compaction_event(events: list[Event], content: types.Content, usage_metadata: Any) -> Event:
        # Keep only the summary text: a reasoning model also returns its thoughts,
        # and ADK seeds the next compaction with this content as a plain model
        # event, so stored thoughts would leak into every later summary.
        content = types.Content(role="model", parts=[p for p in content.parts or [] if not p.thought])
        return Event(
            author="user",
            actions=EventActions(compaction=EventCompaction(
                start_timestamp=events[0].timestamp,
                end_timestamp=events[-1].timestamp,
                compacted_content=content,
            )),
            invocation_id=Event.new_id(),
            usage_metadata=usage_metadata,
        )

    async def maybe_summarize_events(self, *, events: list[Event]) -> Optional[Event]:
        if not events:
            return None
        entries = self._history_entries(events)
        if not entries:
            return None

        budget = self.settings.compaction_input_tokens - estimate_tokens(self._prompt_template)
        rendered: list[str] = []
        previous: Optional[list[str]] = None
        for attempt in range(1, _COMPACTION_ATTEMPTS + 1):
            rendered = self._fit_history(entries, max(64, budget))
            if rendered == previous:
                break  # nothing left to shrink; re-sending the same request would fail the same way
            previous = rendered
            try:
                content, usage = await self._summarize("\n".join(rendered))
            except Exception as e:
                if not is_context_overflow_error(e):
                    logger.warning(
                        "Context harness: compaction skipped, summarizer failed (%s: %s)", type(e).__name__, e)
                    return None
                logger.warning(
                    "Context harness: compaction request rejected for size (attempt %d/%d, budget %d tokens): %s",
                    attempt, _COMPACTION_ATTEMPTS, budget, e)
                budget //= 2
                continue
            if content is None:
                return None
            if not any(p.text and not p.thought for p in content.parts or []):
                # A reasoning model that ran out of output tokens while thinking
                # returns thoughts only; storing that would hide the whole range
                # behind an empty summary.
                logger.warning("Context harness: summarizer returned no summary text (thoughts only)")
                break
            logger.info("Context harness: compacted %d events (%d history entries)", len(events), len(rendered))
            return self._compaction_event(events, content, usage)

        # The model could not produce a summary: keep the task alive with the
        # excerpt itself rather than failing every turn from here on.
        logger.error(
            "Context harness: summarizer failed after %d attempt(s); compacting %d events into a verbatim excerpt",
            attempt, len(events))
        excerpt = (
            "[Automatic excerpt: the summarizer could not process the earlier history, "
            "so these are its capped raw entries. Continue the task from them.]\n" + "\n".join(rendered)
        )
        return self._compaction_event(
            events, types.Content(role="model", parts=[types.Part(text=excerpt)]), None)


def make_compaction_config(settings: HarnessSettings, llm: Any = None) -> EventsCompactionConfig:
    """ADK events compaction: token-threshold (in-invocation) + sliding window.

    ``llm`` is the summarizer model; without it ADK falls back to the root
    agent's model with its generic (unbudgeted) summarizer.
    """
    summarizer = BudgetedEventSummarizer(llm=llm, settings=settings) if llm is not None else None
    return EventsCompactionConfig(
        summarizer=summarizer,
        token_threshold=settings.compaction_token_threshold,
        event_retention_size=settings.compaction_retain_events,
        compaction_interval=settings.compaction_interval,
        overlap_size=1,
    )


# --- Tool-output budget ---


def _result_text(result: Any) -> Optional[str]:
    """Flatten a tool result to the text the model would see.

    Returns None for results we must not rewrite (e.g. MCP media content).
    """
    if result is None:
        return None
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            if not all(isinstance(item, dict) and item.get("type") == "text" for item in content):
                return None
            return "\n".join(item.get("text", "") for item in content)
        if set(result) == {"result"} and isinstance(result["result"], str):
            return result["result"]
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


def _drop_duplicate_structured_content(result: Any, text: str) -> Optional[dict]:
    """FastMCP mirrors a str return value into ``structuredContent.result``, so
    the model would read every MCP result twice. Drop the mirror (None if the
    result is not such a duplicate)."""
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if not isinstance(structured, dict) or list(structured.values()) != [text]:
        return None
    return {k: v for k, v in result.items() if k != "structuredContent"}


def _preview(text: str, max_chars: int) -> str:
    head = int(max_chars * 0.7)
    tail = max_chars - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n... [{omitted} chars omitted] ...\n\n{text[-tail:]}"


def _artifact_name(tool_name: str, call_id: Optional[str]) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{tool_name}_{call_id or 'call'}")
    return f"tool_output_{safe}.txt"


def make_read_tool_output_tool(max_chars: int) -> FunctionTool:
    """Paging tool for results offloaded by the tool-output budget."""

    async def read_tool_output(
        artifact_name: str,
        offset: int = 0,
        limit: int = 0,
        pattern: str = "",
        tool_context=None,
    ) -> dict:
        """
        Read part of a tool result that was too large to return in full.

        Args:
            artifact_name: The `full_output_artifact` value from a truncated tool result.
            offset: Character offset to start reading from.
            limit: Maximum characters to return (0 = the largest allowed page).
            pattern: Optional regular expression; if set, return only matching lines
                     (with line numbers) instead of a character range.
        """
        part = await tool_context.load_artifact(artifact_name)
        if part is None:
            return {"error": f"Artifact '{artifact_name}' not found."}
        text = part.text
        if text is None and part.inline_data and part.inline_data.data:
            text = part.inline_data.data.decode("utf-8", errors="replace")
        text = text or ""

        page = max_chars if limit <= 0 else min(limit, max_chars)
        if pattern:
            try:
                regex = re.compile(pattern)
            except re.error as e:
                return {"error": f"Invalid pattern: {e}"}
            matches = [f"{i}: {line}" for i, line in enumerate(text.splitlines(), 1) if regex.search(line)]
            joined = "\n".join(matches)
            return {
                "matches": joined[:page],
                "match_count": len(matches),
                "truncated": len(joined) > page,
            }

        offset = max(0, offset)
        chunk = text[offset : offset + page]
        next_offset = offset + len(chunk)
        return {
            "content": chunk,
            "offset": offset,
            "next_offset": next_offset,
            "total_chars": len(text),
            "done": next_offset >= len(text),
        }

    return FunctionTool(read_tool_output, require_confirmation=False)


# --- Request budget guard ---


def _part_tokens(part: types.Part) -> int:
    total = 0
    if part.text:
        # Thoughts count too: LiteLLM sends stored reasoning back as
        # `reasoning_content` and e.g. the Qwen3 chat template renders it as
        # <think> blocks for every assistant turn (verified with /apply-template).
        total += estimate_tokens(part.text)
    if part.function_call:
        total += estimate_tokens(json.dumps(part.function_call.args or {}, ensure_ascii=False, default=str))
    if part.function_response:
        total += estimate_tokens(json.dumps(part.function_response.response or {}, ensure_ascii=False, default=str))
    return total


def _content_tokens(content: types.Content) -> int:
    return sum(_part_tokens(p) for p in content.parts or [])


def _fixed_request_tokens(llm_request) -> int:
    """System instruction + tool declarations: present in every request, not trimmable."""
    config = llm_request.config
    if config is None:
        return 0
    total = 0
    si = config.system_instruction
    if isinstance(si, str):
        total += estimate_tokens(si)
    elif si is not None:
        try:
            total += estimate_tokens(si.model_dump_json(exclude_none=True))
        except Exception:
            total += estimate_tokens(str(si))
    for tool in config.tools or []:
        try:
            total += estimate_tokens(tool.model_dump_json(exclude_none=True))
        except Exception:
            total += estimate_tokens(str(tool))
    return total


_DROP_PART = types.Part()  # marker: remove the part entirely


def _elide_part(part: types.Part, kind: str) -> Optional[types.Part]:
    """Return a slimmed copy of `part` for `kind`, or None if nothing to remove."""
    if kind == "thought":
        # Old reasoning is the least useful payload in a request and, without a
        # signature, no provider needs it back (Anthropic/Gemini signed thoughts
        # are opaque state and must stay).
        if part.thought and part.text and not part.thought_signature:
            return _DROP_PART
        return None
    if kind == "function_response" and part.function_response:
        response = part.function_response.response or {}
        size = len(json.dumps(response, ensure_ascii=False, default=str))
        if size <= 200:
            return None
        return types.Part(
            function_response=types.FunctionResponse(
                id=part.function_response.id,
                name=part.function_response.name,
                response={"result": _ELIDED_TEMPLATE.format(chars=size)},
            )
        )
    if kind == "text" and part.text and not part.thought and len(part.text) > 1_000:
        return types.Part(text=part.text[:500] + "\n" + _ELIDED_TEMPLATE.format(chars=len(part.text) - 500))
    return None


def fit_request_to_budget(llm_request, budget_tokens: int, keep_last: int = 2) -> int:
    """Elide the oldest tool payloads / long texts until the request fits.

    Contents are replaced, never mutated in place: ADK builds the request from
    the session's own Content objects, so in-place edits would corrupt history.
    The newest `keep_last` contents (the turn being answered) are untouched.
    Returns the number of parts elided.
    """
    contents = llm_request.contents or []
    fixed = _fixed_request_tokens(llm_request)
    total = fixed + sum(_content_tokens(c) for c in contents)
    if total <= budget_tokens:
        return 0

    elided = 0
    # Three passes: old thoughts (worthless to the model), then tool responses
    # (bulky, re-fetchable), then long texts.
    for kind in ("thought", "function_response", "text"):
        for index in range(max(0, len(contents) - keep_last)):
            if total <= budget_tokens:
                break
            content = contents[index]
            if kind == "text" and content.role == "model":
                # Model turns carry the compaction summary that `ensure_user_query`
                # just told the model to continue from; gutting it loses the task.
                continue
            new_parts = []
            changed = False
            for part in content.parts or []:
                slim = _elide_part(part, kind)
                if slim is None:
                    new_parts.append(part)
                    continue
                total -= _part_tokens(part) - _part_tokens(slim)
                changed = True
                elided += 1
                if slim is not _DROP_PART:
                    new_parts.append(slim)
            if changed and not new_parts:
                new_parts = [types.Part(text="[thoughts elided]")]  # never leave an empty turn
            if changed:
                contents[index] = types.Content(role=content.role, parts=new_parts)

    if total > budget_tokens:
        logger.warning(
            "Context harness: request still ~%d tokens after eliding %d part(s) (budget %d, fixed %d). "
            "Consider fewer enabled tools or a larger MODEL_CONTEXT_WINDOW.",
            total, elided, budget_tokens, fixed,
        )
    return elided


# --- Pruning old tool results ---


def _is_user_turn(content: types.Content) -> bool:
    return content.role == "user" and any(p.text for p in content.parts or [])


def _protected_start(contents: list, protect_user_turns: int) -> int:
    """Index of the oldest of the last `protect_user_turns` user turns (a user
    text, not a tool result); it and everything after it are protected. 0 if
    the request has no more user turns than that."""
    if protect_user_turns <= 0:
        return len(contents)
    seen = 0
    for index in range(len(contents) - 1, -1, -1):
        if _is_user_turn(contents[index]):
            seen += 1
            if seen == protect_user_turns:
                return index
    return 0


async def prune_old_tool_results(llm_request, callback_context, protect_tokens: int, protect_user_turns: int,
                                 minimum_tokens: int) -> int:
    """Clear old tool results without summarizing them.

    Before the last `protect_user_turns` user turns, the newest tool results up
    to `protect_tokens` stay; every older one is replaced by a pointer to an
    artifact holding it (the one the tool-output budget already saved, else a
    new one with the whole response), which ``read_tool_output`` reads back. Nothing happens
    unless the cleared results add up to `minimum_tokens`. Like
    `fit_request_to_budget`, contents are replaced, never mutated, and only
    function_response parts change (their id and name stay, so every call
    keeps its response). Returns the number of results cleared.
    """
    contents = llm_request.contents or []
    seen = 0
    candidates = []  # (content index, part index), newest first
    candidate_tokens = 0
    for index in range(_protected_start(contents, protect_user_turns) - 1, -1, -1):
        for part_index in range(len(contents[index].parts or []) - 1, -1, -1):
            part = contents[index].parts[part_index]
            if not part.function_response:
                continue
            tokens = _part_tokens(part)
            seen += tokens
            # A result no bigger than its pointer (template + artifact name) is not worth clearing.
            if seen > protect_tokens and tokens > estimate_tokens(_PRUNED_TEMPLATE) * 2:
                candidates.append((index, part_index))
                candidate_tokens += tokens
    if not candidates or candidate_tokens < minimum_tokens:
        return 0

    existing: Optional[set] = None
    pruned = 0
    for index, part_index in candidates:
        response = contents[index].parts[part_index].function_response
        data = response.response or {}
        artifact = data.get("full_output_artifact")
        if not isinstance(artifact, str):
            if not response.id:
                continue  # the artifact name would be shared by every id-less result of the tool
            if set(data) == {"result"} and isinstance(data["result"], str):
                text = data["result"]
            else:
                text = json.dumps(data, ensure_ascii=False, default=str)  # every field, not just the text
            artifact = _artifact_name(response.name or "tool", response.id)
            try:
                if existing is None:
                    existing = set(await callback_context.list_artifacts())
                if artifact not in existing:
                    await callback_context.save_artifact(artifact, types.Part.from_text(text=text))
                    existing.add(artifact)
            except Exception as e:  # no artifact service configured
                logger.info("Context harness: could not offload %s output for pruning: %s", response.name, e)
                continue
        content = contents[index]
        parts = list(content.parts)
        parts[part_index] = types.Part(function_response=types.FunctionResponse(
            id=response.id, name=response.name, response={"result": _PRUNED_TEMPLATE.format(artifact=artifact)}))
        contents[index] = types.Content(role=content.role, parts=parts)
        pruned += 1
    if pruned:
        logger.info("Context harness: pruned %d old tool result(s)", pruned)
    return pruned


# --- Tool-call guard ---

# `temp:` state lives only for the current invocation and is never persisted,
# so the per-invocation counters do not pile up in the session.
_GUARD_STATE_KEY = "temp:dak_tool_guard"


def _call_signature(tool_name: str, tool_args: dict) -> str:
    canonical = json.dumps(tool_args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256((tool_name + canonical).encode()).hexdigest()


def _guard_state(state: MutableMapping, invocation_id: str) -> dict:
    return state.setdefault(_GUARD_STATE_KEY, {}).setdefault(invocation_id, {
        "step_count": 0,
        "start_ts": time.time(),
        "last_signature": None,
        "streak": 0,
        "violation_streak": 0,
        "checked_calls": [],
    })


def _count_call(guard: dict, tool_name: str, tool_args: Optional[dict]) -> None:
    guard["step_count"] += 1
    signature = _call_signature(tool_name, tool_args or {})
    guard["streak"] = guard["streak"] + 1 if signature == guard["last_signature"] else 1
    guard["last_signature"] = signature


COMPACTED_USER_QUERY = (
    "[The earlier conversation, including my original request, was compacted "
    "into the summary that follows. Continue the task from it.]"
)


def ensure_user_query(llm_request) -> bool:
    """Guarantee the request carries a user text turn.

    ADK materializes a compaction summary as a *model* message, so once the
    user's request has been compacted a mid-task request can consist only of
    model turns and tool results. Many chat templates reject that (llama.cpp's
    Qwen template: "No user query found in messages"; Anthropic requires a
    leading user turn). Prepend a short user turn pointing at the summary.
    Returns True if a turn was inserted.
    """
    contents = llm_request.contents
    if contents is None:
        return False
    if any(c.role == "user" and any(p.text for p in c.parts or []) for c in contents):
        return False
    contents.insert(0, types.Content(role="user", parts=[types.Part(text=COMPACTED_USER_QUERY)]))
    return True


class ContextHarnessPlugin(BasePlugin):
    """App-wide plugin implementing the tool-call guard, the tool-output budget
    and the request guard."""

    def __init__(self, settings: HarnessSettings, default_model_name: str, name: str = "dak_context_harness"):
        super().__init__(name=name)
        self.settings = settings
        self._default_model_name = default_model_name
        self._settings_cache: Dict[str, HarnessSettings] = {default_model_name: settings}

    def _settings_for(self, callback_context) -> HarnessSettings:
        """Settings for the model this call runs on (`dak:model`, else the
        startup model). Only the context window differs per model; the ADK
        compaction trigger stays at the startup model's (it is fixed when
        the App is built)."""
        from . import call_config  # one-way: call_config never imports harness

        requested = call_config.resolve_dak_settings(callback_context).get(call_config.STATE_CALL_MODEL)
        model_name = get_litellm_model_name(requested) if isinstance(requested, str) else self._default_model_name
        settings = self._settings_cache.get(model_name)
        if settings is None:
            # Not `from_env`: MODEL_CONTEXT_WINDOW states the startup model's
            # window (e.g. a llama-server alias) and must not cap other models.
            # A model litellm does not know (another local alias) has no known
            # window; keep the startup one rather than assume 128K and overflow
            # a small server.
            window = ModeManager.known_context_window(model_name) or self.settings.context_window
            settings = replace(self.settings, context_window=window)
            self._settings_cache[model_name] = settings
        return settings

    async def before_tool_callback(self, *, tool, tool_args, tool_context) -> Optional[dict]:
        """Stop a runaway tool loop: past the invocation's wall-time or tool-call
        limit, or when the same call (tool + args) repeats more than
        ``max_repeated_tool_calls`` times in a row, the tool is not run and the
        returned dict is the observation the model sees instead."""
        settings = self.settings
        guard = _guard_state(tool_context.state, tool_context.invocation_id)
        tool_name = getattr(tool, "name", "tool")
        _count_call(guard, tool_name, tool_args)
        guard["checked_calls"].append(tool_context.function_call_id)

        elapsed = time.time() - guard["start_ts"]
        if elapsed > settings.max_wall_seconds:
            logger.warning("Tool guard: %s blocked, invocation ran %.0fs (limit %.0fs)",
                           tool_name, elapsed, settings.max_wall_seconds)
            return {
                "observation": "wall_time_exceeded",
                "elapsed_seconds": round(elapsed, 1),
                "limit_seconds": settings.max_wall_seconds,
                "hint": "This invocation has run too long; summarize progress and finish with attempt_answer.",
            }
        if guard["step_count"] > settings.max_invocation_tool_calls:
            logger.warning("Tool guard: %s blocked, tool-call limit %d reached",
                           tool_name, settings.max_invocation_tool_calls)
            return {
                "observation": "step_limit_exceeded",
                "limit": settings.max_invocation_tool_calls,
                "hint": "No more tool calls are allowed in this invocation; answer with what you have "
                        "(attempt_answer) or ask the user to continue.",
            }
        if guard["streak"] > settings.max_repeated_tool_calls:
            logger.warning("Tool guard: %s blocked, same call repeated %d times", tool_name, guard["streak"])
            return {
                "observation": "repeated_call",
                "tool": tool_name,
                "count": guard["streak"],
                "hint": "Try a different approach instead of repeating the same call.",
            }
        return None

    def note_argument_violation(self, tool_context, invocation_id: str) -> bool:
        """Count a failed call that is not a repeat (e.g. arguments that break
        the tool's schema). True once more than ``max_repeated_tool_calls``
        such failures came in a row: the caller should answer with a blocking
        observation instead of another correction hint."""
        guard = _guard_state(tool_context.state, invocation_id)
        guard["violation_streak"] += 1
        return guard["violation_streak"] > self.settings.max_repeated_tool_calls

    def note_call_success(self, tool_context, invocation_id: str) -> None:
        """A tool call ran normally: the run of consecutive violations is over."""
        _guard_state(tool_context.state, invocation_id)["violation_streak"] = 0

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result) -> Optional[dict]:
        tool_name = getattr(tool, "name", "tool")
        # A call answered by an earlier plugin (the PermissionPlugin denying it or
        # asking for approval) never reached before_tool_callback, but ADK runs
        # every after_tool_callback: count it here so it still breaks a streak
        # and counts toward the limit.
        guard = _guard_state(tool_context.state, tool_context.invocation_id)
        if tool_context.function_call_id in guard["checked_calls"]:
            guard["checked_calls"].remove(tool_context.function_call_id)
        else:
            _count_call(guard, tool_name, tool_args)
        if tool_name == READ_TOOL_OUTPUT_NAME:
            return None  # already paged to the budget
        max_chars = self.settings.tool_output_chars
        text = _result_text(result)
        if text is None:
            return None
        if len(text) <= max_chars:
            return _drop_duplicate_structured_content(result, text)

        artifact = _artifact_name(tool_name, getattr(tool_context, "function_call_id", None))
        try:
            await tool_context.save_artifact(artifact, types.Part.from_text(text=text))
        except Exception as e:  # no artifact service configured
            logger.info("Context harness: could not offload %s output: %s", tool_name, e)
            artifact = None

        logger.info("Context harness: truncated %s output %d -> %d chars", tool_name, len(text), max_chars)
        replacement = {
            "result": _preview(text, max_chars),
            "truncated": True,
            "original_chars": len(text),
        }
        if isinstance(result, dict) and (result.get("isError") or result.get("is_error")):
            replacement["isError"] = True
        if artifact:
            replacement["full_output_artifact"] = artifact
            replacement["hint"] = (
                f"Output was too large for the context window. Use {READ_TOOL_OUTPUT_NAME}"
                f"(artifact_name='{artifact}', offset=..., pattern=...) to read the rest, "
                "or narrow the tool call."
            )
        else:
            replacement["hint"] = "Output was too large for the context window; narrow the tool call."
        return replacement

    async def before_model_callback(self, *, callback_context, llm_request) -> None:
        settings = self._settings_for(callback_context)
        ensure_user_query(llm_request)
        await prune_old_tool_results(llm_request, callback_context, settings.prune_protect_token_budget,
                                     settings.prune_protect_user_turns, settings.prune_minimum_tokens)
        fit_request_to_budget(llm_request, settings.request_token_budget)
        return None

    async def on_model_error_callback(self, *, callback_context, llm_request, error) -> Optional[LlmResponse]:
        """Retry a request the model rejected for size on a halved budget, a
        bounded number of times within this one call (no state carries over to
        the next turn); then answer with an explanation instead of raising.
        Other errors return None, so ADK re-raises them unchanged; a
        different error during a retry is raised (ADK's plugin manager wraps
        it in a RuntimeError, with this error as the cause)."""
        if not is_context_overflow_error(error):
            return None
        settings = self._settings_for(callback_context)
        invocation = callback_context._invocation_context
        model = invocation.agent.canonical_model
        budget = settings.request_token_budget
        for attempt in range(1, settings.model_error_retry_attempts + 1):
            budget = max(1, budget // 2)
            elided = fit_request_to_budget(llm_request, budget)
            logger.warning(
                "Context harness: model rejected the request for size; retry %d/%d on %d tokens "
                "(%d part(s) elided): %s", attempt, settings.model_error_retry_attempts, budget, elided, error)
            invocation.increment_llm_call_count()  # a retry is a model call: honour max_llm_calls
            try:
                async for llm_response in model.generate_content_async(llm_request, stream=False):
                    if llm_response.content:
                        return llm_response
            except Exception as e:
                if not is_context_overflow_error(e):
                    raise  # not a size problem: report it as it is
                error = e
                continue
            logger.warning("Context harness: retry %d returned no content", attempt)
        logger.error("Context harness: request still rejected after %d retry(ies); ending the turn with an "
                     "explanation. Last error: %s", settings.model_error_retry_attempts, error)
        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=CONTEXT_OVERFLOW_FAILURE_TEXT)]),
            turn_complete=True,
        )
