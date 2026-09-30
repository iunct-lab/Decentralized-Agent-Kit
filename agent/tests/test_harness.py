"""Tests for the context-engineering harness (dak_agent/harness.py)."""
import os
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from dak_agent import harness
from dak_agent.harness import (
    BudgetedEventSummarizer,
    ContextHarnessPlugin,
    HarnessSettings,
    estimate_tokens,
    fit_request_to_budget,
    is_context_overflow_error,
    make_compaction_config,
    make_read_tool_output_tool,
)


def _tool(name="read_file"):
    tool = MagicMock()
    tool.name = name
    return tool


def _tool_context(save_side_effect=None):
    ctx = MagicMock()
    ctx.function_call_id = "call-1"
    ctx.save_artifact = AsyncMock(side_effect=save_side_effect)
    return ctx


class TestEstimateTokens:
    def test_ascii_is_about_four_chars_per_token(self):
        assert estimate_tokens("a" * 400) == 100

    def test_cjk_counts_one_token_per_char(self):
        # len // 4 would say 25 and let a Japanese prompt overflow an 8K model.
        assert estimate_tokens("日本語" * 33 + "a") == 99

    def test_empty(self):
        assert estimate_tokens("") == 0


class TestHarnessSettings:
    def test_small_local_window_gets_tight_budgets(self):
        s = HarnessSettings(context_window=8192)
        assert s.compaction_token_threshold == 4915
        assert s.request_token_budget == 6963
        assert s.tool_output_chars == 2000  # floor

    def test_plan_chars_scales_with_the_window(self):
        assert HarnessSettings(context_window=8192).plan_chars == 1_000       # floor
        assert HarnessSettings(context_window=32_768).plan_chars == 1_638
        assert HarnessSettings(context_window=1_000_000).plan_chars == 8_000  # cap

    def test_large_window_tool_output_is_capped(self):
        assert HarnessSettings(context_window=1_000_000).tool_output_chars == 40_000

    @patch.dict(os.environ, {"MODEL_CONTEXT_WINDOW": "8192", "DAK_TOOL_OUTPUT_MAX_CHARS": "1234",
                             "DAK_COMPACTION_THRESHOLD_RATIO": "0.5", "DAK_COMPACTION_RETAIN_EVENTS": "2",
                             "DAK_MODEL_ERROR_RETRY_ATTEMPTS": "5"})
    def test_from_env(self):
        s = HarnessSettings.from_env("openai/llamacpp")
        assert s.context_window == 8192
        assert s.tool_output_chars == 1234
        assert s.compaction_token_threshold == 4096
        assert s.compaction_retain_events == 2
        assert s.model_error_retry_attempts == 5

    @patch.dict(os.environ, {"MODEL_CONTEXT_WINDOW": "8192", "DAK_COMPACTION_THRESHOLD_RATIO": "7",
                             "DAK_COMPACTION_RETAIN_EVENTS": "-1", "DAK_MODEL_ERROR_RETRY_ATTEMPTS": "-1"})
    def test_invalid_env_falls_back_to_defaults(self):
        s = HarnessSettings.from_env("openai/llamacpp")
        assert s.compaction_threshold_ratio == 0.6
        assert s.compaction_retain_events == 4
        assert s.model_error_retry_attempts == 2

    def test_compaction_config_uses_token_threshold(self):
        config = make_compaction_config(HarnessSettings(context_window=8192), llm=MagicMock())
        assert config.token_threshold == 4915
        assert config.event_retention_size == 4
        assert isinstance(config.summarizer, BudgetedEventSummarizer)
        assert "User request" in config.summarizer._prompt_template

    def test_compaction_budgets_derive_from_window(self):
        s = HarnessSettings(context_window=32768)
        assert s.compaction_input_tokens == 16384  # half the window; the rest is for the summary
        assert s.compaction_entry_chars == 1638
        assert HarnessSettings(context_window=8192).compaction_entry_chars == 409
        assert HarnessSettings(context_window=4096).compaction_entry_chars == 400  # floor
        assert HarnessSettings(context_window=1_000_000).compaction_entry_chars == 2000  # cap


# --- Budgeted compaction summarizer -----------------------------------------


def _event(author, *, text=None, thought=None, call=None, response=None, ts=1.0):
    from google.adk.events.event import Event

    parts = []
    if thought:
        parts.append(types.Part(text=thought, thought=True))
    if text:
        parts.append(types.Part(text=text))
    if call:
        parts.append(types.Part(function_call=types.FunctionCall(id="fc", name=call[0], args=call[1])))
    if response:
        parts.append(types.Part(function_response=types.FunctionResponse(
            id="fc", name=response[0], response=response[1])))
    return Event(author=author, content=types.Content(role="model" if author != "user" else "user", parts=parts),
                 timestamp=ts, invocation_id="inv")


def _summarizer_llm(responses):
    """LLM whose `generate_content_async` yields/raises per `responses` (in order); records prompts."""
    from google.adk.models.llm_response import LlmResponse

    llm = MagicMock()
    llm.model = "scripted"
    llm.prompts = []
    queue = list(responses)

    async def generate_content_async(llm_request, stream=False):
        llm.prompts.append(llm_request.contents[0].parts[0].text)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        # A reasoning model returns its thoughts alongside the summary.
        parts = [types.Part(text="Let me compact this...", thought=True), types.Part(text=item)]
        yield LlmResponse(content=types.Content(role="model", parts=parts),
                          usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=1))

    llm.generate_content_async = generate_content_async
    return llm


def _context_error():
    import litellm

    return litellm.ContextWindowExceededError(
        message="request (50848 tokens) exceeds the available context size (32768 tokens)",
        model="scripted", llm_provider="openai")


class TestContextOverflowClassification:
    def test_litellm_class(self):
        assert is_context_overflow_error(_context_error())

    def test_llamacpp_message(self):
        assert is_context_overflow_error(ValueError("request (50848 tokens) exceeds the available context size"))

    def test_other_errors(self):
        assert not is_context_overflow_error(ConnectionError("connection refused"))
        assert not is_context_overflow_error(ValueError("No user query found in messages"))
        assert not is_context_overflow_error(ValueError("Model does not support context window caching"))


class TestBudgetedEventSummarizer:
    settings = HarnessSettings(context_window=8192)  # entry cap 409 chars, input budget 4096 tokens

    def _events(self, thought_chars=6000, n=6):
        # Mirrors the wedged session: a rolling summary seed, then thought-heavy tool turns.
        events = [_event("model", text="User request: 調査して。Progress: docs を読んだ。" * 10, ts=0.5)]
        for i in range(n):
            events.append(_event("dak_agent", thought="考察" * (thought_chars // 2), call=("read_file", {"path": f"f{i}"}),
                                 ts=float(i + 1)))
            events.append(_event("dak_agent", response=("read_file", {"result": "x" * 3000}), ts=float(i + 1) + 0.5))
        return events

    @pytest.mark.asyncio
    async def test_thought_heavy_history_is_fitted_to_the_input_budget(self):
        summarizer = BudgetedEventSummarizer(_summarizer_llm(["summary"]), self.settings)
        events = self._events()
        # ADK's summarizer would send every thought verbatim: ~30K tokens on an 8K window.
        assert sum(estimate_tokens(p.text) for e in events for p in e.content.parts if p.thought) > 30_000

        event = await summarizer.maybe_summarize_events(events=events)

        prompt = summarizer._llm.prompts[0]
        assert estimate_tokens(prompt) <= self.settings.compaction_input_tokens
        assert "called tool: read_file" in prompt and "truncated" in prompt
        assert "User request: 調査して" in prompt  # the dense seed survives
        assert [p.text for p in event.actions.compaction.compacted_content.parts] == ["summary"]  # no thoughts
        assert event.actions.compaction.start_timestamp == 0.5
        assert event.actions.compaction.end_timestamp == 6.5

    def test_streamed_thought_fragments_become_one_entry(self):
        # Streaming stored one turn of reasoning as ~150 thought parts of a few chars each.
        from google.adk.events.event import Event

        parts = [types.Part(text=f"断片{i}", thought=True) for i in range(150)]
        parts.append(types.Part(function_call=types.FunctionCall(id="fc", name="list_skills", args={})))
        event = Event(author="dak_agent", content=types.Content(role="model", parts=parts), timestamp=1.0,
                      invocation_id="inv")
        summarizer = BudgetedEventSummarizer(_summarizer_llm([]), self.settings)

        entries = summarizer._history_entries([event])

        assert [e.kind for e in entries] == ["thought", "call"]
        assert entries[0].text.startswith("dak_agent (thought): 断片0断片1")

    def test_separate_events_are_not_merged(self):
        summarizer = BudgetedEventSummarizer(_summarizer_llm([]), self.settings)
        entries = summarizer._history_entries([
            _event("dak_agent", text="First answer.", ts=1.0), _event("dak_agent", text="Second answer.", ts=2.0)])
        assert [e.text for e in entries] == ["dak_agent: First answer.", "dak_agent: Second answer."]

    def test_fit_never_drops_the_last_entry(self):
        summarizer = BudgetedEventSummarizer(_summarizer_llm([]), self.settings)
        entries = summarizer._history_entries(
            [_event("user", response=("planner", {"result": "x" * 3000}), ts=float(i)) for i in range(5)])
        rendered = summarizer._fit_history(entries, budget_tokens=5)
        assert len(rendered) == 1

    @pytest.mark.asyncio
    async def test_retry_stops_early_when_nothing_is_left_to_shrink(self):
        llm = _summarizer_llm([_context_error()] * 3)
        summarizer = BudgetedEventSummarizer(llm, HarnessSettings(context_window=512))  # budget already tiny

        event = await summarizer.maybe_summarize_events(events=self._events(n=1))

        assert len(llm.prompts) < 3  # identical prompts are not re-sent
        assert event.actions.compaction.compacted_content.parts[0].text.startswith("[Automatic excerpt")

    @pytest.mark.asyncio
    async def test_thought_only_summary_falls_back_to_the_excerpt(self):
        """A reasoning model that ran out of output tokens returns thoughts only."""
        from google.adk.models.llm_response import LlmResponse

        llm = _summarizer_llm([])

        async def thoughts_only(llm_request, stream=False):
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="hmm", thought=True)]))

        llm.generate_content_async = thoughts_only
        summarizer = BudgetedEventSummarizer(llm, self.settings)

        event = await summarizer.maybe_summarize_events(events=self._events())

        parts = event.actions.compaction.compacted_content.parts
        assert len(parts) == 1 and not parts[0].thought
        assert parts[0].text.startswith("[Automatic excerpt")

    def test_fit_loses_bulk_before_facts(self):
        summarizer = BudgetedEventSummarizer(_summarizer_llm([]), self.settings)
        entries = summarizer._history_entries(self._events(n=40))
        rendered = summarizer._fit_history(entries, budget_tokens=600)
        # Oldest bulky entries were dropped; the first dense entry never is.
        assert rendered[0].startswith("model: User request: 調査して")
        assert sum(estimate_tokens(r) for r in rendered) <= 600
        assert len(rendered) < len(entries)
        assert rendered[-1].startswith("Tool response from read_file")  # newest kept

    @pytest.mark.asyncio
    async def test_context_error_halves_the_budget_and_retries(self):
        llm = _summarizer_llm([_context_error(), "summary"])
        summarizer = BudgetedEventSummarizer(llm, self.settings)

        event = await summarizer.maybe_summarize_events(events=self._events())

        assert event is not None
        assert len(llm.prompts) == 2
        assert estimate_tokens(llm.prompts[1]) < estimate_tokens(llm.prompts[0])

    @pytest.mark.asyncio
    async def test_persistent_context_error_falls_back_to_an_excerpt(self):
        """The wedged-session case: instead of raising on every turn, compact anyway."""
        llm = _summarizer_llm([_context_error()] * 3)
        summarizer = BudgetedEventSummarizer(llm, self.settings)

        event = await summarizer.maybe_summarize_events(events=self._events())

        assert len(llm.prompts) == 3
        text = event.actions.compaction.compacted_content.parts[0].text
        assert text.startswith("[Automatic excerpt")
        assert "User request: 調査して" in text
        assert event.actions.compaction.compacted_content.role == "model"

    @pytest.mark.asyncio
    async def test_other_model_errors_skip_compaction_instead_of_raising(self):
        llm = _summarizer_llm([ConnectionError("llama-server is down")])
        summarizer = BudgetedEventSummarizer(llm, self.settings)

        assert await summarizer.maybe_summarize_events(events=self._events()) is None

    @pytest.mark.asyncio
    async def test_empty(self):
        summarizer = BudgetedEventSummarizer(_summarizer_llm([]), self.settings)
        assert await summarizer.maybe_summarize_events(events=[]) is None


class TestToolOutputBudget:
    plugin = ContextHarnessPlugin(HarnessSettings(context_window=8192), "test-model")  # 2000 chars

    @pytest.mark.asyncio
    async def test_small_result_untouched(self):
        result = await self.plugin.after_tool_callback(
            tool=_tool(), tool_args={}, tool_context=_tool_context(), result="short")
        assert result is None

    @pytest.mark.asyncio
    async def test_mcp_structured_duplicate_dropped(self):
        mcp_result = {"content": [{"type": "text", "text": "hello"}],
                      "structuredContent": {"result": "hello"}, "isError": False}
        result = await self.plugin.after_tool_callback(
            tool=_tool(), tool_args={}, tool_context=_tool_context(), result=mcp_result)
        assert result == {"content": [{"type": "text", "text": "hello"}], "isError": False}

    @pytest.mark.asyncio
    async def test_large_result_truncated_and_offloaded(self):
        text = "".join(f"line {i}\n" for i in range(2000))
        ctx = _tool_context()
        mcp_result = {"content": [{"type": "text", "text": text}], "structuredContent": {"result": text}}
        result = await self.plugin.after_tool_callback(
            tool=_tool(), tool_args={}, tool_context=ctx, result=mcp_result)

        assert result["truncated"] is True
        assert result["original_chars"] == len(text)
        assert len(result["result"]) < 2100
        assert result["result"].startswith("line 0\n")
        assert result["result"].rstrip().endswith("line 1999")
        assert result["full_output_artifact"] == "tool_output_read_file_call-1.txt"
        saved_name, saved_part = ctx.save_artifact.call_args.args
        assert saved_name == result["full_output_artifact"]
        assert saved_part.text == text

    @pytest.mark.asyncio
    async def test_without_artifact_service_still_truncates(self):
        ctx = _tool_context(save_side_effect=ValueError("Artifact service is not initialized."))
        result = await self.plugin.after_tool_callback(
            tool=_tool(), tool_args={}, tool_context=ctx, result="x" * 10_000)
        assert result["truncated"] is True
        assert "full_output_artifact" not in result
        assert "narrow the tool call" in result["hint"]

    @pytest.mark.asyncio
    async def test_media_results_untouched(self):
        mcp_result = {"content": [{"type": "image", "data": "x" * 10_000, "mimeType": "image/png"}]}
        result = await self.plugin.after_tool_callback(
            tool=_tool(), tool_args={}, tool_context=_tool_context(), result=mcp_result)
        assert result is None

    @pytest.mark.asyncio
    async def test_read_tool_output_is_exempt(self):
        result = await self.plugin.after_tool_callback(
            tool=_tool(harness.READ_TOOL_OUTPUT_NAME), tool_args={}, tool_context=_tool_context(),
            result={"content": "x" * 10_000})
        assert result is None


class TestToolCallGuard:
    def _plugin(self, **overrides):
        return ContextHarnessPlugin(HarnessSettings(context_window=8192, **overrides), "test-model")

    def _ctx(self, invocation_id="inv-1", state=None):
        ctx = MagicMock()
        ctx.invocation_id = invocation_id
        ctx.state = {} if state is None else state
        return ctx

    async def _call(self, plugin, ctx, name="read_file", args=None):
        return await plugin.before_tool_callback(
            tool=_tool(name), tool_args={"path": "a.txt"} if args is None else args, tool_context=ctx)

    @pytest.mark.asyncio
    async def test_repeated_identical_call_is_blocked_after_threshold(self):
        plugin, ctx = self._plugin(), self._ctx()
        results = [await self._call(plugin, ctx) for _ in range(4)]
        assert results[:3] == [None, None, None]
        assert results[3]["observation"] == "repeated_call"
        assert results[3]["tool"] == "read_file"
        assert results[3]["count"] == 4

    @pytest.mark.asyncio
    async def test_different_args_or_tools_break_the_streak(self):
        plugin, ctx = self._plugin(), self._ctx()
        for i in range(8):
            assert await self._call(plugin, ctx, args={"path": f"{i}.txt"}) is None
        for name in ("read_file", "read_file", "read_file", "list_dir", "read_file"):
            assert await self._call(plugin, ctx, name=name) is None

    @pytest.mark.asyncio
    async def test_step_limit_stops_further_calls(self):
        plugin, ctx = self._plugin(max_invocation_tool_calls=5), self._ctx()
        for i in range(5):
            assert await self._call(plugin, ctx, args={"path": f"{i}.txt"}) is None
        blocked = await self._call(plugin, ctx, args={"path": "5.txt"})
        assert blocked["observation"] == "step_limit_exceeded"
        assert blocked["limit"] == 5

    @pytest.mark.asyncio
    async def test_wall_time_limit_stops_further_calls(self):
        plugin, ctx = self._plugin(max_wall_seconds=60.0), self._ctx()
        with patch.object(harness.time, "time", return_value=1_000.0):
            assert await self._call(plugin, ctx, args={"path": "0.txt"}) is None
        with patch.object(harness.time, "time", return_value=1_061.0):
            blocked = await self._call(plugin, ctx, args={"path": "1.txt"})
        assert blocked["observation"] == "wall_time_exceeded"
        assert blocked["elapsed_seconds"] == 61.0
        assert blocked["limit_seconds"] == 60.0

    @pytest.mark.asyncio
    async def test_different_invocation_ids_have_independent_guards(self):
        plugin, state = self._plugin(max_invocation_tool_calls=3), {}
        first, second = self._ctx("inv-1", state), self._ctx("inv-2", state)
        for _ in range(3):
            assert await self._call(plugin, first) is None
        assert (await self._call(plugin, first))["observation"] == "step_limit_exceeded"
        for _ in range(3):
            assert await self._call(plugin, second) is None

    @pytest.mark.asyncio
    async def test_calls_intercepted_by_an_earlier_plugin_are_still_counted(self):
        """In the app the PermissionPlugin runs first; when it answers a call
        (denied / needs approval) ADK skips the later plugins' before_tool_callback,
        but still runs every after_tool_callback."""
        from google.adk.plugins.base_plugin import BasePlugin
        from google.adk.plugins.plugin_manager import PluginManager

        class DenyRunCommand(BasePlugin):
            async def before_tool_callback(self, *, tool, tool_args, tool_context):
                return {"observation": "denied_by_policy"} if tool.name == "run_command" else None

        plugin, ctx = self._plugin(max_invocation_tool_calls=6), self._ctx()
        manager = PluginManager(plugins=[DenyRunCommand(name="deny"), plugin])

        async def call(name, n):
            ctx.function_call_id = f"call-{n}"
            tool, args = _tool(name), {"path": "a.txt"}
            result = await manager.run_before_tool_callback(tool=tool, tool_args=args, tool_context=ctx)
            await manager.run_after_tool_callback(
                tool=tool, tool_args=args, tool_context=ctx, result=result or {"result": "ok"})
            return result

        calls = ["read_file"] * 3 + ["run_command", "read_file", "run_command"]
        assert [await call(name, n) for n, name in enumerate(calls)] == [None] * 3 + [
            {"observation": "denied_by_policy"}, None, {"observation": "denied_by_policy"}]
        # The two denied calls broke the read_file streak and count toward the limit.
        assert (await call("read_file", 6))["observation"] == "step_limit_exceeded"

    @pytest.mark.asyncio
    async def test_guard_state_is_invocation_scoped_temp_state(self):
        plugin, ctx = self._plugin(), self._ctx()
        await self._call(plugin, ctx)
        assert list(ctx.state) == ["temp:dak_tool_guard"]  # `temp:` is never persisted by ADK

    def test_note_argument_violation_stops_after_the_limit(self):
        plugin, ctx = self._plugin(), self._ctx()
        limit = plugin.settings.max_repeated_tool_calls
        results = [plugin.note_argument_violation(ctx, "inv-1") for _ in range(limit + 1)]
        assert results == [False] * limit + [True]

    def test_note_call_success_resets_the_streak(self):
        plugin, ctx = self._plugin(), self._ctx()
        limit = plugin.settings.max_repeated_tool_calls
        for _ in range(limit):
            assert plugin.note_argument_violation(ctx, "inv-1") is False
        plugin.note_call_success(ctx, "inv-1")
        assert [plugin.note_argument_violation(ctx, "inv-1") for _ in range(limit)] == [False] * limit

    @patch.dict(os.environ, {"MODEL_CONTEXT_WINDOW": "8192", "DAK_MAX_REPEATED_TOOL_CALLS": "5",
                             "DAK_MAX_TOOL_CALLS": "12", "DAK_MAX_WALL_SECONDS": "90"})
    def test_limits_from_env(self):
        s = HarnessSettings.from_env("openai/llamacpp")
        assert (s.max_repeated_tool_calls, s.max_invocation_tool_calls, s.max_wall_seconds) == (5, 12, 90.0)


class TestReadToolOutput:
    def _ctx(self, text):
        ctx = MagicMock()
        ctx.load_artifact = AsyncMock(return_value=types.Part.from_text(text=text))
        return ctx

    @pytest.mark.asyncio
    async def test_pages_through_output(self):
        tool = make_read_tool_output_tool(max_chars=100)
        ctx = self._ctx("abcdefghij" * 25)
        first = await tool.func(artifact_name="a.txt", tool_context=ctx)
        assert len(first["content"]) == 100 and first["next_offset"] == 100 and not first["done"]
        last = await tool.func(artifact_name="a.txt", offset=200, limit=500, tool_context=ctx)
        assert len(last["content"]) == 50 and last["done"]

    @pytest.mark.asyncio
    async def test_pattern_returns_matching_lines(self):
        tool = make_read_tool_output_tool(max_chars=1000)
        ctx = self._ctx("alpha\nbeta\nalphabet\n")
        result = await tool.func(artifact_name="a.txt", pattern="^alpha", tool_context=ctx)
        assert result["matches"] == "1: alpha\n3: alphabet"
        assert result["match_count"] == 2

    @pytest.mark.asyncio
    async def test_missing_artifact(self):
        ctx = MagicMock()
        ctx.load_artifact = AsyncMock(return_value=None)
        tool = make_read_tool_output_tool(max_chars=100)
        assert "error" in await tool.func(artifact_name="nope", tool_context=ctx)


def _fr(name, size):
    return types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=f"id-{name}", name=name, response={"result": "z" * size}))])


class TestRequestBudgetGuard:
    def test_thoughts_count_and_old_unsigned_ones_are_dropped_first(self):
        # LiteLLM sends stored reasoning back (`reasoning_content`) and the Qwen3
        # template renders it, so thoughts must be budgeted and are the first to go.
        assert harness._part_tokens(types.Part(text="a" * 400, thought=True)) == 100
        request = LlmRequest(contents=[
            types.Content(role="user", parts=[types.Part(text="q")]),
            types.Content(role="model", parts=[
                types.Part(text="x" * 4000, thought=True),
                types.Part(text="y" * 4000, thought=True, thought_signature=b"sig"),
                types.Part(function_call=types.FunctionCall(id="1", name="t", args={}))]),
            types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
                id="1", name="t", response={"result": "z" * 2000}))]),
            types.Content(role="user", parts=[types.Part(text="next")]),
        ])
        elided = fit_request_to_budget(request, budget_tokens=2000, keep_last=1)
        parts = request.contents[1].parts
        assert elided == 1
        assert [bool(p.thought_signature) for p in parts if p.thought] == [True]  # unsigned gone, signed kept
        assert parts[-1].function_call is not None
        assert request.contents[2].parts[0].function_response.response == {"result": "z" * 2000}  # untouched

    def test_thought_only_turn_is_not_left_empty(self):
        request = LlmRequest(contents=[
            types.Content(role="model", parts=[types.Part(text="x" * 4000, thought=True)]),
            types.Content(role="user", parts=[types.Part(text="next")]),
        ])
        fit_request_to_budget(request, budget_tokens=100, keep_last=1)
        assert request.contents[0].parts == [types.Part(text="[thoughts elided]")]

    def test_under_budget_is_noop(self):
        request = LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text="hi")])])
        assert fit_request_to_budget(request, budget_tokens=100) == 0

    def test_elides_oldest_tool_payloads_first(self):
        old, mid = _fr("old", 4000), _fr("mid", 4000)
        recent = [types.Content(role="model", parts=[types.Part(text="thinking")]), _fr("new", 4000)]
        request = LlmRequest(contents=[old, mid, *recent])

        elided = fit_request_to_budget(request, budget_tokens=2200, keep_last=2)

        assert elided == 1  # dropping "old" alone gets under budget
        assert "elided" in request.contents[0].parts[0].function_response.response["result"]
        assert request.contents[0].parts[0].function_response.id == "id-old"
        assert request.contents[1] is mid
        assert request.contents[2:] == recent
        # The session's own Content object must not be mutated.
        assert old.parts[0].function_response.response["result"] == "z" * 4000


    def test_never_guts_the_compaction_summary(self):
        """The summary is a model-role text part; eliding it would drop the task
        the request has just been told to continue."""
        summary = types.Content(role="model", parts=[types.Part(text="User request: " + "s" * 3000)])
        request = LlmRequest(contents=[summary, _fr("a", 4000), _fr("b", 4000),
                                       types.Content(role="user", parts=[types.Part(text="hi")])])

        fit_request_to_budget(request, budget_tokens=10, keep_last=1)

        assert request.contents[0] is summary
        assert len(summary.parts[0].text) == len("User request: ") + 3000


class _ArtifactContext:
    """The artifact half of a CallbackContext / ToolContext, kept in a dict."""

    def __init__(self, saved=None):
        self.saved = dict(saved or {})
        self.save_calls = 0

    async def save_artifact(self, name, part):
        self.save_calls += 1
        self.saved[name] = part
        return 0

    async def load_artifact(self, name):
        return self.saved.get(name)

    async def list_artifacts(self):
        return list(self.saved)


def _turns(count, size=4000):
    """`count` finished user turns, each: question, tool call, ~`size`//4-token tool result, answer."""
    contents = []
    for n in range(1, count + 1):
        contents += [
            types.Content(role="user", parts=[types.Part(text=f"q{n}")]),
            types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
                id=f"c{n}", name="t", args={}))]),
            types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
                id=f"c{n}", name="t", response={"result": str(n) * size}))]),
            types.Content(role="model", parts=[types.Part(text=f"a{n}")]),
        ]
    return contents


def _response(contents, n):
    """The function_response of turn `n` (1-based) in `_turns` contents."""
    return contents[(n - 1) * 4 + 2].parts[0].function_response


def _cleared(response):
    return str(response.response.get("result", "")).startswith("[cleared;")


class TestPruneOldToolResults:
    """PBI #102 AC2: old tool results are cleared without a summary, the recent
    user turns and call/response pairing are kept, and the output stays
    readable through read_tool_output."""

    async def _prune(self, contents, ctx=None, protect_tokens=0, protect_user_turns=2, minimum_tokens=0):
        request = LlmRequest(contents=contents)
        pruned = await harness.prune_old_tool_results(
            request, ctx or _ArtifactContext(), protect_tokens, protect_user_turns, minimum_tokens)
        return request, pruned

    def test_settings_default_to_a_fifth_of_the_window(self):
        s = HarnessSettings(context_window=8192)
        assert s.prune_protect_token_budget == 1638
        assert s.prune_protect_user_turns == 2
        assert s.prune_minimum_tokens == 512

    @patch.dict(os.environ, {"MODEL_CONTEXT_WINDOW": "8192", "DAK_PRUNE_PROTECT_TOKENS": "3000",
                             "DAK_PRUNE_PROTECT_USER_TURNS": "1", "DAK_PRUNE_MINIMUM_TOKENS": "100"})
    def test_settings_from_env(self):
        s = HarnessSettings.from_env("openai/llamacpp")
        assert s.prune_protect_token_budget == 3000
        assert s.prune_protect_user_turns == 1
        assert s.prune_minimum_tokens == 100

    @pytest.mark.asyncio
    async def test_prunes_only_beyond_the_protect_budget(self):
        original = _turns(5)
        contents = list(original)

        request, pruned = await self._prune(contents, protect_tokens=1500)

        # Turns 4-5 are protected; of the older results the newest (turn 3,
        # ~1000 tokens) fits the 1500-token budget, turns 2 and 1 do not.
        assert pruned == 2
        assert [_cleared(_response(request.contents, n)) for n in range(1, 6)] == [True, True, False, False, False]
        # The session's own Content objects are not mutated.
        assert _response(original, 1).response == {"result": "1" * 4000}

    @pytest.mark.asyncio
    async def test_never_touches_the_protected_recent_user_turns(self):
        contents = _turns(4)
        protected = contents[8:]

        request, pruned = await self._prune(contents, protect_tokens=0, protect_user_turns=2)

        assert pruned == 2
        assert all(a is b for a, b in zip(request.contents[8:], protected))

    @pytest.mark.asyncio
    async def test_a_single_turn_is_never_pruned(self):
        """Within one user turn (a long tool loop) nothing is older than the
        protected turns; compaction is what handles that case."""
        contents = _turns(1) + [
            types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
                id="c9", name="t", args={}))]),
            types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
                id="c9", name="t", response={"result": "9" * 4000}))]),
        ]
        _, pruned = await self._prune(contents, protect_tokens=0, protect_user_turns=1)
        assert pruned == 0

    @pytest.mark.asyncio
    async def test_below_minimum_does_nothing(self):
        contents = _turns(4)
        before = list(contents)

        request, pruned = await self._prune(contents, protect_tokens=0, minimum_tokens=10_000)

        assert pruned == 0
        assert all(a is b for a, b in zip(request.contents, before))

    @pytest.mark.asyncio
    async def test_pruned_result_can_be_reread_via_read_tool_output(self):
        ctx = _ArtifactContext()
        request, _ = await self._prune(_turns(3), ctx=ctx)

        placeholder = _response(request.contents, 1).response["result"]
        artifact = re.search(r"read_tool_output\('([^']+)'\)", placeholder).group(1)
        page = await make_read_tool_output_tool(max_chars=10_000).func(artifact_name=artifact, tool_context=ctx)
        assert page["content"] == "1" * 4000
        assert page["done"]

    @pytest.mark.asyncio
    async def test_reuses_an_existing_artifact_instead_of_saving_again(self):
        contents = _turns(3)
        contents[2] = types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            id="c1", name="t", response={"result": "preview " * 500, "truncated": True,
                                         "full_output_artifact": "tool_output_t_c1.txt"}))])
        ctx = _ArtifactContext(saved={"tool_output_t_c1.txt": types.Part.from_text(text="full")})

        request, pruned = await self._prune(contents, ctx=ctx)

        assert pruned == 1
        assert "tool_output_t_c1.txt" in _response(request.contents, 1).response["result"]
        assert ctx.save_calls == 0

    @pytest.mark.asyncio
    async def test_the_same_history_is_saved_once_across_requests(self):
        """Every model call re-prunes the session's history; the artifact
        written for a result on the first call is not written again."""
        ctx = _ArtifactContext()
        for _ in range(3):
            request, pruned = await self._prune(_turns(3), ctx=ctx)
            assert pruned == 1
        assert ctx.save_calls == 1

    @pytest.mark.asyncio
    async def test_without_artifact_service_the_result_is_kept(self):
        ctx = MagicMock()
        ctx.list_artifacts = AsyncMock(side_effect=ValueError("Artifact service is not initialized."))
        ctx.save_artifact = AsyncMock(side_effect=ValueError("Artifact service is not initialized."))

        request, pruned = await self._prune(_turns(3), ctx=ctx)

        assert pruned == 0
        assert _response(request.contents, 1).response == {"result": "1" * 4000}

    @pytest.mark.asyncio
    async def test_keeps_function_call_response_pairing(self):
        request, pruned = await self._prune(_turns(5))

        assert pruned == 3
        calls = [p.function_call for c in request.contents for p in c.parts if p.function_call]
        responses = [p.function_response for c in request.contents for p in c.parts if p.function_response]
        assert [(c.id, c.name) for c in calls] == [(r.id, r.name) for r in responses]
        assert [len(c.parts) for c in request.contents] == [1] * len(request.contents)

    @pytest.mark.asyncio
    async def test_before_model_callback_prunes_before_the_budget_guard(self):
        plugin = ContextHarnessPlugin(HarnessSettings(context_window=8192, prune_minimum_tokens=0), "test-model")
        request = LlmRequest(contents=_turns(4))
        with patch("dak_agent.call_config.resolve_dak_settings", return_value={}), \
                patch.object(harness, "fit_request_to_budget") as fit:
            await plugin.before_model_callback(callback_context=_ArtifactContext(), llm_request=request)

        assert _cleared(_response(request.contents, 1))
        assert fit.call_args.args[0] is request


class TestPerCallHarnessSettings:
    """PBI #138 AC3: the request budget follows the model chosen per call
    (`dak:model`); the default model keeps the startup settings."""

    DEFAULT = "openai/llamacpp"
    OTHER = "gemini/gemini-2.5-flash"  # 1,048,576 tokens in litellm's model map

    def _plugin(self):
        return harness.ContextHarnessPlugin(HarnessSettings(context_window=8192), self.DEFAULT)

    def _settings_for(self, plugin, call_settings):
        with patch("dak_agent.call_config.resolve_dak_settings", return_value=call_settings):
            return plugin._settings_for(MagicMock())

    def test_settings_for_uses_default_when_no_call_model(self):
        plugin = self._plugin()
        assert self._settings_for(plugin, {}) is plugin.settings
        assert self._settings_for(plugin, {"dak:model": self.DEFAULT}) is plugin.settings

    @patch.dict(os.environ, {}, clear=False)
    def test_settings_for_recomputes_for_different_model(self):
        os.environ.pop("MODEL_CONTEXT_WINDOW", None)
        plugin = self._plugin()

        settings = self._settings_for(plugin, {"dak:model": self.OTHER})

        assert settings.context_window == 1_048_576
        assert settings.request_token_budget != plugin.settings.request_token_budget
        # Cached per model: the same object on the next call.
        assert self._settings_for(plugin, {"dak:model": self.OTHER}) is settings

    @patch.dict(os.environ, {"MODEL_CONTEXT_WINDOW": "8192"})
    def test_default_model_window_override_does_not_apply_to_other_models(self):
        """MODEL_CONTEXT_WINDOW states the window of the startup MODEL_NAME
        (e.g. a llama-server alias); it must not cap another model."""
        settings = self._settings_for(self._plugin(), {"dak:model": self.OTHER})
        assert settings.context_window == 1_048_576

    def test_unknown_model_does_not_get_a_larger_window_than_the_startup_model(self):
        """A model id absent from litellm's map (e.g. another llama-server
        alias) has no known window; assuming 128K would let requests overflow
        a small local server, so it keeps the startup model's window."""
        settings = self._settings_for(self._plugin(), {"dak:model": "openai/another-local-alias"})
        assert settings.context_window == 8192

    def test_budget_ratio_is_kept_for_other_models(self):
        plugin = harness.ContextHarnessPlugin(
            HarnessSettings(context_window=8192, request_budget_ratio=0.5), self.DEFAULT)
        assert self._settings_for(plugin, {"dak:model": self.OTHER}).request_token_budget == 1_048_576 // 2

    @pytest.mark.asyncio
    async def test_before_model_callback_uses_the_call_models_budget(self):
        plugin = self._plugin()
        request = LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text="hi")])])
        with patch("dak_agent.call_config.resolve_dak_settings", return_value={"dak:model": self.OTHER}), \
                patch.object(harness, "fit_request_to_budget") as fit:
            await plugin.before_model_callback(callback_context=MagicMock(), llm_request=request)
        fit.assert_called_once_with(request, int(1_048_576 * 0.85))


class TestEnsureUserQuery:
    def test_inserts_user_turn_when_only_model_and_tool_turns_remain(self):
        summary = types.Content(role="model", parts=[types.Part(text="User request: ...")])
        request = LlmRequest(contents=[summary, _fr("read_file", 10)])
        assert harness.ensure_user_query(request) is True
        assert request.contents[0].role == "user"
        assert request.contents[1] is summary

    def test_noop_when_user_text_present(self):
        request = LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text="hi")]),
                                       _fr("read_file", 10)])
        assert harness.ensure_user_query(request) is False
        assert len(request.contents) == 2


class TestOnModelErrorCallback:
    """PBI #88: a model call rejected for size is retried a bounded number of
    times with a tighter budget, then fails with an explanation instead of
    raising."""

    settings = HarnessSettings(context_window=8192, model_error_retry_attempts=2)

    def _request(self):
        # Two old 4K-char tool results the budget can elide, then the turn being answered.
        return LlmRequest(contents=[
            types.Content(role="user", parts=[types.Part(text="ログを読んで")]),
            _fr("read_file", 16_000), _fr("read_file", 16_000),
            types.Content(role="user", parts=[types.Part(text="続けて")]),
            types.Content(role="model", parts=[types.Part(text="...")]),
        ])

    def _context(self, llm):
        ctx = MagicMock()
        ctx._invocation_context.agent.canonical_model = llm
        return ctx

    def _llm(self, responses):
        """`responses`: per call, an exception to raise, a text to answer, or None
        for an answer without content; records request sizes."""
        from google.adk.models.llm_response import LlmResponse

        llm = MagicMock()
        llm.request_tokens = []
        queue = list(responses)

        async def generate_content_async(llm_request, stream=False):
            llm.request_tokens.append(_request_tokens(llm_request))
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=item)]) if item else None)

        llm.generate_content_async = generate_content_async
        return llm

    async def _callback(self, llm, error, request=None, context=None):
        plugin = ContextHarnessPlugin(self.settings, "test-model")
        request = request or self._request()
        with patch("dak_agent.call_config.resolve_dak_settings", return_value={}):
            response = await plugin.on_model_error_callback(
                callback_context=context or self._context(llm), llm_request=request, error=error)
        return response, request

    @pytest.mark.asyncio
    async def test_recovers_after_tightening_the_budget(self):
        llm = self._llm([_context_error(), "recovered"])

        response, request = await self._callback(llm, _context_error())

        assert response.content.parts[0].text == "recovered"
        assert len(llm.request_tokens) == 2
        # Each attempt ran on a tighter budget: the old tool payloads were elided.
        assert llm.request_tokens[0] <= self.settings.request_token_budget // 2
        assert llm.request_tokens[1] <= llm.request_tokens[0]
        assert all(p.function_response.response["result"].startswith("[elided")
                   for c in request.contents[1:3] for p in c.parts)

    @pytest.mark.asyncio
    async def test_gives_up_after_exhausting_attempts_with_an_explicit_failure(self):
        llm = self._llm([_context_error()] * 10)

        response, _ = await self._callback(llm, _context_error())

        assert len(llm.request_tokens) == self.settings.model_error_retry_attempts
        assert harness.CONTEXT_OVERFLOW_FAILURE_TEXT in response.content.parts[0].text
        assert response.content.role == "model"
        assert response.turn_complete

    @pytest.mark.asyncio
    async def test_the_last_retrys_error_is_logged(self, caplog):
        last = ValueError("request (7777 tokens) exceeds the available context size")
        llm = self._llm([_context_error(), last])

        with caplog.at_level("ERROR", logger="dak_agent.harness"):
            await self._callback(llm, _context_error())

        assert "7777 tokens" in caplog.text

    @pytest.mark.asyncio
    async def test_zero_attempts_fails_explicitly_without_calling_the_model(self):
        self.settings = HarnessSettings(context_window=8192, model_error_retry_attempts=0)
        llm = self._llm([])

        response, _ = await self._callback(llm, _context_error())

        assert llm.request_tokens == []
        assert harness.CONTEXT_OVERFLOW_FAILURE_TEXT in response.content.parts[0].text

    @pytest.mark.asyncio
    async def test_a_different_error_during_a_retry_is_raised_not_reported_as_overflow(self):
        """Raised from the callback; ADK's plugin manager then wraps it in a
        RuntimeError whose __cause__ is this error."""
        llm = self._llm([ConnectionError("connection refused")])

        with pytest.raises(ConnectionError):
            await self._callback(llm, _context_error())
        assert len(llm.request_tokens) == 1

    @pytest.mark.asyncio
    async def test_each_retry_counts_against_max_llm_calls(self):
        llm = self._llm([_context_error(), "recovered"])
        context = self._context(llm)

        await self._callback(llm, _context_error(), context=context)

        assert context._invocation_context.increment_llm_call_count.call_count == 2

    @pytest.mark.asyncio
    async def test_a_retry_without_content_is_logged_and_retried(self, caplog):
        llm = self._llm([None, "recovered"])

        with caplog.at_level("WARNING", logger="dak_agent.harness"):
            response, _ = await self._callback(llm, _context_error())

        assert response.content.parts[0].text == "recovered"
        assert "returned no content" in caplog.text

    @pytest.mark.asyncio
    async def test_non_overflow_errors_are_not_handled(self):
        llm = self._llm([])

        response, _ = await self._callback(llm, ConnectionError("connection refused"))

        assert response is None  # ADK re-raises the original error
        assert llm.request_tokens == []


# --- End-to-end: a real ADK Runner with a scripted model --------------------

WINDOW = 8192
BIG_OUTPUT = "日本語のログ行です\n" * 4000  # ~40K chars, CJK-heavy like a real Japanese session


def big_tool(page: int = 0) -> str:
    """Return a huge log."""
    return BIG_OUTPUT


def _request_tokens(llm_request) -> int:
    return harness._fixed_request_tokens(llm_request) + sum(
        harness._content_tokens(c) for c in llm_request.contents or [])


# ~3.4K CJK chars of reasoning per step, stored the way streaming stores it:
# one thought part of a few chars per chunk (the wedged session had ~5,600 of
# them for 23K chars). ADK's summarizer renders each on its own prefixed line.
THOUGHT_FRAGMENTS = ["考える。"] * 850


PLAN = [{"step": "read repo", "status": "done"}, {"step": "write summary", "status": "pending"}]


def _make_fake_llm(tool_calls: int, thoughts: bool = False, plan: bool = False, overflows: int = 0):
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse

    class ScriptedLlm(BaseLlm):
        """Calls big_tool `tool_calls` times (a new page each time, so the
        repeated-call guard does not cut the loop short), then answers. Records request sizes.
        With `plan`, the first step records a plan with write_todos. The first
        `overflows` requests are rejected as too large whatever their size."""
        steps: int = 0
        overflows_left: int = overflows
        request_tokens: list = []
        system_instructions: list = []
        summaries_before_request: list = []
        summary_tokens: list = []
        summaries: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            tokens = _request_tokens(llm_request)
            usage = types.GenerateContentResponseUsageMetadata(prompt_token_count=tokens)
            text = "".join(p.text or "" for c in llm_request.contents for p in c.parts or [])
            if "compacting the working memory" in text or "conversation history between a user" in text:
                self.summary_tokens.append(tokens)
                if tokens > WINDOW:
                    raise ValueError(f"the request exceeds the available context size ({tokens} > {WINDOW})")
                self.summaries += 1
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(
                    text="User request: inspect logs. Progress: read logs.")]), usage_metadata=usage)
                return
            self.request_tokens.append(tokens)
            self.system_instructions.append(llm_request.config.system_instruction or "")
            self.summaries_before_request.append(self.summaries)
            if not any(c.role == "user" and any(p.text for p in c.parts or []) for c in llm_request.contents):
                # Mirrors llama.cpp's Qwen chat template (--jinja).
                raise ValueError("Jinja Exception: No user query found in messages.")
            if tokens > WINDOW or self.overflows_left > 0:
                self.overflows_left = max(0, self.overflows_left - 1)
                raise ValueError(f"the request exceeds the available context size ({tokens} > {WINDOW})")
            self.steps += 1
            if plan and self.steps == 1:
                part = types.Part(function_call=types.FunctionCall(
                    id="fc-plan", name="write_todos", args={"items": PLAN}))
            elif self.steps <= tool_calls + int(plan):
                part = types.Part(function_call=types.FunctionCall(
                    id=f"fc-{self.steps}", name="big_tool", args={"page": self.steps}))
            else:
                part = types.Part(text="done")
            parts = [types.Part(text=f, thought=True) for f in THOUGHT_FRAGMENTS] + [part] if thoughts else [part]
            yield LlmResponse(content=types.Content(role="model", parts=parts), usage_metadata=usage)

    return ScriptedLlm(model="scripted")


async def _run(use_harness: bool, tool_calls: int = 6, thoughts: bool = False, adk_summarizer: bool = False,
               plan: bool = False):
    """`plan`: run DAK's AdaptiveAgent (which injects the session's plan into
    the instruction) with write_todos, instead of a bare LlmAgent."""
    from google.adk.agents import LlmAgent
    from google.adk.apps import App
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import FunctionTool

    llm = _make_fake_llm(tool_calls, thoughts=thoughts, plan=plan)
    settings = HarnessSettings(context_window=WINDOW)
    tools = [FunctionTool(big_tool)]
    if use_harness:
        tools.append(make_read_tool_output_tool(settings.tool_output_chars))
    if plan:
        from dak_agent.adaptive_agent import AdaptiveAgent
        from dak_agent.builtin_tools import write_todos

        tools.append(FunctionTool(write_todos))
        agent = AdaptiveAgent(model=llm, name="dak_agent", instruction="Inspect the logs.", tools=tools)
    else:
        agent = LlmAgent(name="dak_agent", model=llm, instruction="Inspect the logs.", tools=tools)
    compaction = make_compaction_config(settings, llm=llm) if use_harness else None
    if compaction is not None and adk_summarizer:
        from google.adk.apps.llm_event_summarizer import LlmEventSummarizer

        compaction.summarizer = LlmEventSummarizer(llm=llm)  # what shipped before this fix
    app = App(
        name="dak_agent",
        root_agent=agent,
        plugins=[ContextHarnessPlugin(settings, "test-model")] if use_harness else [],
        events_compaction_config=compaction,
    )
    sessions = InMemorySessionService()
    artifacts = InMemoryArtifactService()
    runner = Runner(app=app, session_service=sessions, artifact_service=artifacts)
    session = await sessions.create_session(app_name="dak_agent", user_id="u")

    final_text = None
    error = None
    try:
        async for event in runner.run_async(
            user_id="u", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="ログを全部読んで要約して")]),
        ):
            for part in (event.content.parts if event.content else None) or []:
                if part.text and not part.thought:
                    final_text = part.text
    except ValueError as e:
        error = e
    session = await sessions.get_session(app_name="dak_agent", user_id="u", session_id=session.id)
    return llm, session, final_text, error, artifacts


@pytest.mark.asyncio
async def test_without_harness_a_long_tool_loop_overflows_the_window():
    """Reproduces the reported failure: one request, several big tool results."""
    llm, _, final_text, error, _ = await _run(use_harness=False)
    assert error is not None and "context size" in str(error)
    assert final_text is None


@pytest.mark.asyncio
async def test_harness_keeps_every_request_inside_the_window():
    llm, session, final_text, error, artifacts = await _run(use_harness=True)

    assert error is None
    assert final_text == "done"
    assert llm.steps == 7
    assert max(llm.request_tokens) <= WINDOW
    # Compaction ran inside the single invocation and left a summary event.
    assert llm.summaries >= 1
    assert any(e.actions.compaction for e in session.events)
    # Full outputs were offloaded for read_tool_output.
    keys = await artifacts.list_artifact_keys(app_name="dak_agent", user_id="u", session_id=session.id)
    assert any(k.startswith("tool_output_big_tool") for k in keys)


@pytest.mark.asyncio
async def test_adk_summarizer_overflows_on_a_reasoning_model():
    """Reproduces the 2026-09-14 wedge: thoughts are not in the model prompt, but
    ADK's summarizer renders them verbatim, so the *compaction* request overflows."""
    llm, _, final_text, error, _ = await _run(use_harness=True, thoughts=True, adk_summarizer=True)
    assert error is not None and "context size" in str(error)
    assert final_text is None
    assert max(llm.summary_tokens) > WINDOW
    assert max(llm.request_tokens) <= WINDOW  # the request guard did its job; compaction killed the run


@pytest.mark.asyncio
async def test_budgeted_summarizer_keeps_compaction_inside_the_window():
    llm, session, final_text, error, _ = await _run(use_harness=True, thoughts=True)

    assert error is None
    assert final_text == "done"
    assert llm.summaries >= 1
    assert max(llm.summary_tokens) <= WINDOW
    assert max(llm.request_tokens) <= WINDOW


@pytest.mark.asyncio
async def test_plan_survives_compaction():
    """PBI #87: the plan recorded before compaction is still in the model's
    request after the history was summarised (it lives in state, and the
    instruction is rebuilt from state every turn)."""
    with patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
        llm, session, final_text, error, _ = await _run(use_harness=True, plan=True)

    assert error is None
    assert final_text == "done"
    # A compaction summarised the event that recorded the plan...
    plan_event = next(e for e in session.events if e.content and any(
        p.function_call and p.function_call.name == "write_todos" for p in e.content.parts or []))
    assert any(e.actions.compaction.start_timestamp <= plan_event.timestamp <= e.actions.compaction.end_timestamp
               for e in session.events if e.actions.compaction)
    # ...before the final request, which still carries the plan.
    assert llm.summaries_before_request[-1] >= 1
    last_system = llm.system_instructions[-1]
    assert "# Current Plan" in last_system
    assert "[pending] write summary" in last_system
    assert session.state["dak_todos"] == PLAN


@pytest.mark.asyncio
async def test_plan_state_is_not_lost_by_compaction_summary():
    """Compaction replaces history with a summary event; it never writes state."""
    with patch("dak_agent.remote_tools.discover_remote_tools", AsyncMock(return_value={})):
        _, session, _, error, _ = await _run(use_harness=True, plan=True)

    assert error is None
    compaction_events = [e for e in session.events if e.actions.compaction]
    assert compaction_events
    assert all("dak_todos" not in (e.actions.state_delta or {}) for e in compaction_events)
    assert session.state["dak_todos"] == PLAN


@pytest.mark.asyncio
async def test_tool_loop_stops_at_step_limit_without_raising():
    """PBI #99: a model that never stops calling a tool is stopped by the step
    limit with an observation, across repeated compactions, without an exception."""
    limit = HarnessSettings(context_window=WINDOW).max_invocation_tool_calls
    llm, session, final_text, error, _ = await _run(use_harness=True, tool_calls=limit + 3)

    assert error is None
    assert final_text == "done"
    assert llm.summaries >= 2
    responses = [p.function_response for e in session.events if e.content
                 for p in e.content.parts or [] if p.function_response]
    assert len(responses) == limit + 3
    assert "step_limit_exceeded" in str(responses[-1].response)


async def _run_turns(overflows: int, messages: list[str]):
    """Send `messages` one after another to the same session, the first
    `overflows` model requests being rejected as too large. Returns the model's
    final text per turn (the run must not raise)."""
    from google.adk.agents import LlmAgent
    from google.adk.apps import App
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    llm = _make_fake_llm(tool_calls=0, overflows=overflows)
    settings = HarnessSettings(context_window=WINDOW)
    agent = LlmAgent(name="dak_agent", model=llm, instruction="Answer.")
    app = App(name="dak_agent", root_agent=agent, plugins=[ContextHarnessPlugin(settings, "test-model")])
    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions)
    session = await sessions.create_session(app_name="dak_agent", user_id="u")

    finals = []
    for message in messages:
        final_text = None
        async for event in runner.run_async(
            user_id="u", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
        ):
            for part in (event.content.parts if event.content else None) or []:
                if part.text and not part.thought:
                    final_text = part.text
        finals.append(final_text)
    return llm, finals


@pytest.mark.asyncio
async def test_an_injected_overflow_is_recovered_and_the_session_continues():
    """PBI #88 AC1/AC2: one overflow is absorbed by a retry; the next user turn
    in the same session runs normally."""
    llm, finals = await _run_turns(overflows=1, messages=["最初の質問", "次の質問"])

    assert finals == ["done", "done"]
    assert llm.steps == 2
    assert llm.overflows_left == 0


@pytest.mark.asyncio
async def test_a_persistent_overflow_fails_with_an_explanation_and_the_session_continues():
    """PBI #88 AC1/AC2: when every retry overflows, the turn ends with an
    explanation instead of an exception, and the next turn still works."""
    retries = HarnessSettings(context_window=WINDOW).model_error_retry_attempts
    llm, finals = await _run_turns(overflows=1 + retries, messages=["最初の質問", "次の質問"])

    assert harness.CONTEXT_OVERFLOW_FAILURE_TEXT in finals[0]
    assert finals[1] == "done"
    assert llm.steps == 1  # only the second turn reached the model successfully
