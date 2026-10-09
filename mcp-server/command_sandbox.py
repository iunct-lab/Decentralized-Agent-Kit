"""Run run_command inside anthropics/sandbox-runtime (srt) (docs/design/command-sandbox.md).

This decides only *in what frame* an already-allowed command runs, never
*whether* it may run (the agent's before_tool_callback, #16) and never parses
the command (#109).

MCP_COMMAND_SANDBOX:
  off  (default) the command runs as before (`shell=True`).
  srt  the command runs as `srt --settings <MCP_SRT_SETTINGS> -c <command>`. When
       srt or the settings file is missing, the command is not run at all.
Any other value is an error, so a typo never silently drops the sandbox.
"""
import os
import shutil

COMMAND_SANDBOX_ENV = "MCP_COMMAND_SANDBOX"
SETTINGS_ENV = "MCP_SRT_SETTINGS"
DEFAULT_SETTINGS = "/app/srt-settings.json"
MODES = ("off", "srt")
# Outside allowWrite everything is read-only inside srt, including the server's
# own /app (the image's WORKDIR), so commands start in the workspace instead.
SRT_WORKDIR = "/projects"


def sandbox_mode() -> str:
    mode = os.getenv(COMMAND_SANDBOX_ENV, "off")
    if mode not in MODES:
        raise ValueError(f"{COMMAND_SANDBOX_ENV} must be one of {', '.join(MODES)}; got {mode!r}")
    return mode


def settings_path() -> str:
    return os.getenv(SETTINGS_ENV, DEFAULT_SETTINGS)


def build_argv(command: str, mode: str, settings: str) -> list[str] | str:
    if mode == "srt":
        # -c: srt hands the string to a shell, as shell=True does. Without it srt
        # takes the whole string as one program name ("echo hi: command not found").
        return ["srt", "--settings", settings, "-c", command]
    return command


def check_ready(mode: str, settings: str, which=None) -> str | None:
    """Why `mode` cannot run a command now, or None when it can."""
    if mode != "srt":
        return None
    if (which or shutil.which)("srt") is None:
        return "srt is not installed (build the image with WITH_SRT=1)"
    if not os.path.isfile(settings):
        return f"the srt settings file {settings} does not exist"
    return None
