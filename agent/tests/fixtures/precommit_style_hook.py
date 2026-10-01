"""A PreToolUse hook written for Claude Code, unchanged: reads the hook input on
stdin and blocks `rm -rf` through hookSpecificOutput (exit 0 either way)."""
import json
import sys

data = json.load(sys.stdin)
if data.get("tool_name") == "run_command" and data.get("tool_input", {}).get("command", "").startswith("rm -rf"):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "destructive command blocked by hook",
    }}))
sys.exit(0)
