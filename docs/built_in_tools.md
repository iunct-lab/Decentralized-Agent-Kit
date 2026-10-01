# Built-in Tools Reference

This document describes the built-in tools available to the DAK Agent. These tools are implemented within the Agent itself (Client-Side) or provided by the base `LlmAgent` class, and are essential for the agent's autonomy, skill management, and control flow.

## 1. Skill Management Tools

These tools allow the agent to dynamically discover and load new capabilities (Skills) or individual tools from the MCP server.

### `list_skills`
*   **Description**: Lists all available Agent Skills (curated YAML definitions) and individual Remote Tools (Zero-Config) available on the MCP server.
*   **Usage**: The agent uses this when it needs a capability it doesn't currently have.
*   **Returns**: A formatted list of skill names and descriptions.
*   **Implementation**: `agent/dak_agent/adaptive_agent.py` (Client-Side)

### `enable_skill`
*   **Description**: Enables a specific skill or tool.
    *   **Curated Skill**: Loads the skill's instructions into the System Prompt and activates its associated tools.
    *   **Remote Tool**: Loads the tool's schema and activates it for use.
*   **Arguments**:
    *   `skill_name` (str): The name of the skill or tool to enable.
*   **Implementation**: `agent/dak_agent/adaptive_agent.py` (Client-Side)

---

## 2. Mode Switching Tools

These tools manage the agent's context and focus.

### `switch_mode`
*   **Description**: Requests a switch in the agent's "Mode" (Context). This triggers the Dynamic Mode Switching logic, which may update the System Instruction and active toolset based on the new focus.
*   **Arguments**:
    *   `reason` (str): The reason for switching modes.
    *   `new_focus` (str): The new topic or task to focus on.
*   **Implementation**: `agent/dak_agent/adaptive_agent.py` (Overrides base implementation)

---

## 3. Plan and Progress Tools

Always available (in every mode), and always allowed by the Ulysses Pact. The plan lives in session state (`dak_todos`), so compacting the conversation history does not lose it; it is also added to the system instruction as `# Current Plan`, capped at 5% of the context window (1,000–8,000 chars): done steps are collapsed first, then the rest is cut at a step boundary (the first open step is always shown, shortened if needed) with a pointer to `read_plan`. `read_plan` itself is never shortened by this cap; a very long result is still subject to the context harness's tool-output limit like any tool, and the model can page it with `read_tool_output`.

### `write_todos`
*   **Description**: Records (or replaces) the whole plan and each step's progress. Call it again whenever a status changes. A non-list `items` is rejected and the saved plan is kept.
*   **Arguments**:
    *   `items` (List[dict]): `[{"step": "...", "status": "pending" | "in_progress" | "done"}]`. Unknown statuses become `pending` (`completed` becomes `done`).

### `read_plan`
*   **Description**: Returns the current plan and progress, or `No plan recorded yet.`

### `read_original_request`
*   **Description**: Returns the session's first user message in full (state `dak_original_request`), or `No original request recorded yet.` The system instruction carries it as `# Original Request`, capped like the plan; when cut, it ends with a pointer to this tool. Always available and always allowed by the Ulysses Pact.

### `write_handoff`
*   **Description**: Records (or replaces) the handoff for resuming a long task after its context is reset (state `dak_handoff`): what someone starting from scratch needs to go on without redoing finished work. Returns the handoff as text under the headings Objective / Done / Decisions / Next steps / Files / Open questions (an empty list shows `(none)`). Always available and always allowed by the Ulysses Pact.
*   **Arguments**:
    *   `objective` (str): What the task is for.
    *   `done`, `decisions`, `next_steps`, `files`, `open_questions` (List[str]): One item per line. A JSON string of a list is read as the list; any other string is one item.

---

## 4. Enforcer Mode Tools

These tools are active when the agent is in **Enforcer Mode** (Strict Mode), enforcing a "Think, Plan, Act" loop.

### `planner`
*   **Description**: Creates a plan of execution. The agent *must* call this before using any other tools in Enforcer Mode.
*   **Arguments**:
    *   `steps` (List[str]): A list of steps to execute.
    *   `allowed_tools` (List[str]): (Optional) Tools allowed for this plan.

### `attempt_answer`
*   **Description**: Provides the final answer to the user. This signals the end of the task or turn.
*   **Arguments**:
    *   `answer` (str): The final response text.

### `ask_question`
*   **Description**: Asks the user a clarifying question. This pauses execution to wait for user input.
*   **Arguments**:
    *   `question` (str): The question to ask.

---

## 5. Standard MCP Tools (Server-Side)

These tools are provided by the MCP Server (`mcp-server/`) and are what the agent typically uses to perform actual work. They are loaded dynamically or via Skills.

*   `read_file`: Read file contents.
*   `write_file`: Write to a file.
*   `list_files`: List directory contents.
*   `search_files`: Search for files.
*   `run_command`: Execute a shell command (if enabled).
