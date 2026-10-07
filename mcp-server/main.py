import asyncio
import contextlib
import io
import os
import posixpath
import re
import subprocess
import glob
import tarfile
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount
import uvicorn

from sandbox import DOCKER_SOCKET, DOCKER_WORKDIR, SANDBOX_TTL_SECONDS, SandboxManager, check_socket_exposure

# DNS rebinding protection: since mcp 1.23 FastMCP auto-enables it for its
# default host (127.0.0.1) and then accepts only localhost Host headers, which
# rejects the agent's `Host: mcp-server:8000` with 421. Keep the protection on
# and allow the compose service name; MCP_ALLOWED_HOSTS (comma-separated
# host:port patterns, `*` port wildcard) adds hosts for other deployments.
DEFAULT_ALLOWED_HOSTS = ["mcp-server:*", "localhost:*", "127.0.0.1:*", "[::1]:*"]


def _allowed_hosts() -> list[str]:
    extra = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return DEFAULT_ALLOWED_HOSTS + extra


def _transport_security() -> TransportSecuritySettings:
    hosts = _allowed_hosts()
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")],
    )


# Initialize FastMCP server with recommended settings
mcp = FastMCP("dak-agent-mcp", json_response=True, transport_security=_transport_security())

# Output bounds: an unbounded tool result (a whole file, a recursive listing)
# can overflow the calling model's context window in one call. Tools return at
# most this much and tell the caller how to fetch the rest.
def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment; a bad value must not crash the server."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        print(f"Warning: ignoring invalid {name}={raw!r}; using {default}.")
        return default


MAX_OUTPUT_CHARS = _env_int("MCP_MAX_OUTPUT_CHARS", 50000)
MAX_LIST_ENTRIES = _env_int("MCP_MAX_LIST_ENTRIES", 500)
MAX_GREP_MATCHES = _env_int("MCP_MAX_GREP_MATCHES", 100)


def _cap_text(text: str, hint: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n[truncated: {len(text) - limit} more chars. {hint}]"


def _cap_entries(entries: list, hint: str, limit: int = MAX_LIST_ENTRIES) -> str:
    if len(entries) <= limit:
        return "\n".join(entries)
    shown = "\n".join(entries[:limit])
    return f"{shown}\n\n[truncated: {len(entries) - limit} more entries. {hint}]"


# Per-session isolation (docs/design/session-sandbox.md). SANDBOX_MODE=off, the
# default, keeps every tool on the shared /projects exactly as before; an
# unknown SANDBOX_MODE stops the server here.
_sandbox = SandboxManager()
check_socket_exposure(_sandbox.mode, os.path.exists("/.dockerenv"), os.path.exists(DOCKER_SOCKET))


def _session(ctx: Context | None) -> tuple[str, dict]:
    """The caller's session key (the agent's X-DAK-Session-Key, #19) and its environment."""
    request = ctx.request_context.request if ctx is not None else None
    key = request.headers.get("x-dak-session-key", "default") if request is not None else "default"
    return key, _sandbox.ensure_session(key)


def _session_path(ctx: Context | None, path: str) -> str:
    """Where a file tool's path points for this session; raises ValueError outside it."""
    if _sandbox.mode == "docker":
        # The workspace lives only inside the container: docker goes through _docker_*,
        # never this server's files.
        raise ValueError("SANDBOX_MODE=docker has no workspace on this server")
    _, entry = _session(ctx)
    if entry["workdir"] is None:
        return path
    root = os.path.realpath(entry["workdir"])
    resolved = os.path.realpath(os.path.join(root, path))
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError(f"{path} is outside the session workspace")
    return resolved


def _docker_path(path: str) -> str:
    """A file tool's path inside the session container, relative to its /workspace; raises ValueError outside it."""
    rel = posixpath.normpath(path)
    if posixpath.isabs(rel) or rel == ".." or rel.startswith("../"):
        raise ValueError(f"{path} is outside the session workspace")
    return rel


def _docker_exec(ctx: Context | None, command: list[str], input: str | None = None, text: bool = True):
    """Run a command in the caller's session container; its stdout, or OSError with its stderr."""
    key, _ = _session(ctx)
    result = _sandbox.exec_in_session(key, command, input=input, text=text)
    if result.returncode != 0:
        err = result.stderr if text else result.stderr.decode("utf-8", errors="replace")
        raise OSError(err.strip() or f"exit code {result.returncode}")
    return result.stdout


# The file tools' I/O. In docker mode it runs in the session container with tools
# every image has (cat, sh, ls, tar), not Python: SANDBOX_IMAGE can be anything.
def _read_text(ctx: Context | None, path: str) -> str:
    if _sandbox.mode == "docker":
        return _docker_exec(ctx, ["cat", "--", _docker_path(path)])
    with open(_session_path(ctx, path), "r", encoding="utf-8") as f:
        return f.read()


def _write_text(ctx: Context | None, path: str, content: str) -> None:
    if _sandbox.mode == "docker":
        script = 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"'
        _docker_exec(ctx, ["sh", "-c", script, "sh", _docker_path(path)], input=content)
        return
    target = _session_path(ctx, path)
    # Ensure directory exists
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)


def _list_dir(ctx: Context | None, path: str) -> list[str]:
    if _sandbox.mode == "docker":
        # The trailing / makes ls fail on a file, as os.listdir does.
        return _docker_exec(ctx, ["ls", "-A1", "--", _docker_path(path) + "/"]).splitlines()
    return os.listdir(_session_path(ctx, path))


def _docker_tree(ctx: Context | None, path: str) -> tuple[str, list[tuple[str, bytes]]]:
    """The regular files under `path` in the session container, read with one tar.

    Returns the resolved base and (path, content) pairs, both under /workspace, for _shown.
    """
    rel = _docker_path(path)
    data = _docker_exec(ctx, ["tar", "-cf", "-", "--", rel], text=False)
    files = []
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar:
            if member.isfile():
                name = posixpath.join(DOCKER_WORKDIR, posixpath.normpath(member.name))
                files.append((name, tar.extractfile(member).read()))
    return posixpath.normpath(posixpath.join(DOCKER_WORKDIR, rel)), files


def _shown(path: str, base: str, found: str) -> str:
    """A path found under `base` (the resolved `path`), shown as the caller wrote it."""
    if base == path:  # off: nothing was resolved, keep the output as before
        return found
    return path if found == base else os.path.join(path, os.path.relpath(found, base))


@mcp.tool()
async def deep_think(thought: str) -> str:
    """
    A tool for deep thinking and complex reasoning.
    Use this when the user asks for a deep analysis or "deep think" on a topic.
    Returns a thought process.
    """
    return thought

@mcp.tool()
async def read_file(path: str, offset: int = 0, limit: int = 0, ctx: Context | None = None) -> str:
    """
    Read the content of a file. For large files, read a range of lines.
    Args:
        path: The path to the file to read (relative to /projects).
        offset: 0-based line number to start reading from (default: 0).
        limit: Maximum number of lines to return (default: 0 = to the end of the file).
    """
    try:
        content = _read_text(ctx, path)
    except Exception as e:
        return f"Error reading file: {e}"
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    if offset > 0 or limit > 0:
        start = max(0, offset)
        end = start + limit if limit > 0 else total_lines
        content = "".join(lines[start:end])
    return _cap_text(
        content,
        f"The file has {total_lines} lines; call read_file(path, offset=<line>, limit=<lines>) to read a range.",
    )

@mcp.tool()
async def write_file(path: str, content: str, ctx: Context | None = None) -> str:
    """
    Write content to a file. Overwrites existing content.
    Args:
        path: The path to the file to write.
        content: The content to write.
    """
    try:
        _write_text(ctx, path, content)
        return f"Successfully wrote to {path}"
    except Exception as e:
        return f"Error writing file: {e}"

@mcp.tool()
async def list_files(path: str = ".", ctx: Context | None = None) -> str:
    """
    List files and directories in a given path.
    Args:
        path: The directory path to list (default: current directory).
    """
    try:
        items = sorted(_list_dir(ctx, path))
        return _cap_entries(items, "List a subdirectory or use search_files with a pattern.")
    except Exception as e:
        return f"Error listing files: {e}"

@mcp.tool()
async def run_command(command: str, ctx: Context | None = None) -> str:
    """
    Execute a shell command.
    Args:
        command: The command to execute.
    """
    try:
        key, entry = _session(ctx)
        if entry["mode"] == "docker":
            result = _sandbox.exec_in_session(key, ["sh", "-c", command])
        else:
            # off: cwd=None, the shared /projects as before; inproc: the session directory.
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=entry["workdir"],
            )
        # Cap each stream on its own: capping the concatenation would drop the
        # stderr of a command that wrote a lot to stdout before failing.
        hint = "Narrow the command output (e.g. pipe through head, tail or grep)."
        output = f"Exit code: {result.returncode}\nStdout:\n{_cap_text(result.stdout, hint)}\n"
        if result.stderr:
            output += f"\nStderr:\n{_cap_text(result.stderr, hint)}"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Command timed out"
    except Exception as e:
        return f"Error executing command: {e}"

@mcp.tool()
async def search_files(pattern: str, path: str = ".", ctx: Context | None = None) -> str:
    """
    Search for files matching a glob pattern.
    Args:
        pattern: The glob pattern to search for (e.g., "*.py").
        path: The root path to search in.
    """
    try:
        if _sandbox.mode == "docker":
            base, tree = _docker_tree(ctx, path)
            matches = [_shown(path, base, name) for name, _ in tree
                       if glob.fnmatch.fnmatch(posixpath.basename(name), pattern)]
            return _cap_entries(matches, "Use a more specific pattern or path.")
        base = _session_path(ctx, path)
        matches = []
        for root, _, files in os.walk(base):
            for file in files:
                if glob.fnmatch.fnmatch(file, pattern):
                    matches.append(_shown(path, base, os.path.join(root, file)))
        return _cap_entries(matches, "Use a more specific pattern or path.")
    except Exception as e:
        return f"Error searching files: {e}"

@mcp.tool()
async def grep(pattern: str, path: str = ".", glob_pattern: str = "*", ignore_case: bool = False,
               ctx: Context | None = None) -> str:
    """
    Search file contents for lines matching a regular expression.
    Args:
        pattern: Regular expression to search for.
        path: Directory to search in, or a single file.
        glob_pattern: Glob pattern selecting which files to search (default: all).
        ignore_case: Match case-insensitively when True.
    """
    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
        matches = []
        hint = "Narrow the search (a more specific pattern, path or glob_pattern)."

        def scan(shown: str, lines) -> bool:
            """Collect the matching lines; True once the cap is reached."""
            for line_no, line in enumerate(lines, start=1):
                if regex.search(line):
                    matches.append(f"{shown}:{line_no}: {line.rstrip()}")
                    if len(matches) >= MAX_GREP_MATCHES:
                        return True
            return False

        if _sandbox.mode == "docker":
            base, tree = _docker_tree(ctx, path)
            for name, content in sorted(tree):
                if name != base and not glob.fnmatch.fnmatch(posixpath.basename(name), glob_pattern):
                    continue
                if scan(_shown(path, base, name), content.decode("utf-8", errors="replace").splitlines()):
                    break
        else:
            base = _session_path(ctx, path)
            if os.path.isfile(base):
                files = [base]
            else:
                files = []
                for root, _, file_names in os.walk(base):
                    for name in file_names:
                        if glob.fnmatch.fnmatch(name, glob_pattern):
                            files.append(os.path.join(root, name))
            for file in sorted(files):
                try:
                    with open(file, "r", encoding="utf-8", errors="replace") as f:
                        if scan(_shown(path, base, file), f):
                            break
                except Exception:
                    continue
        if not matches:
            return "No matches found."
        if len(matches) >= MAX_GREP_MATCHES:
            shown = "\n".join(matches)
            return f"{shown}\n\n[truncated: more matches than the {MAX_GREP_MATCHES}-match cap. Narrow the search.]"
        return _cap_text("\n".join(matches), hint)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"
    except Exception as e:
        return f"Error searching content: {e}"

@mcp.tool()
async def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False,
                    ctx: Context | None = None) -> str:
    """
    Replace an exact string in a file.
    Args:
        path: The path to the file to edit.
        old_string: The exact text to find and replace.
        new_string: The text to replace it with.
        replace_all: Replace every occurrence when True; otherwise the
                     occurrence must be unique or the edit is refused.
    """
    try:
        content = _read_text(ctx, path)
    except Exception as e:
        return f"Error reading file: {e}"
    count = content.count(old_string)
    if count == 0:
        return "Edit failed: old_string not found in the file."
    if not replace_all and count > 1:
        return (
            f"Edit failed: old_string occurs {count} times; "
            "widen it to a unique snippet or pass replace_all=True."
        )
    new_content = content.replace(old_string, new_string)
    try:
        _write_text(ctx, path, new_content)
    except Exception as e:
        return f"Error writing file: {e}"
    if replace_all:
        return f"Replaced {count} occurrence(s) in {path}"
    return f"Replaced 1 occurrence in {path}"


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    # Switch to the projects directory to ensure tools operate on the user's workspace
    try:
        if os.path.exists("/projects"):
            os.chdir("/projects")
            print("Changed working directory to /projects")
        else:
            print("Warning: /projects directory not found. File tools may not work as expected.")
    except Exception as e:
        print(f"Error changing directory: {e}")

    # Sessions are destroyed after SANDBOX_TTL_SECONDS without a call, and all of
    # them when the server stops. Tool bodies are synchronous and block the event
    # loop, so the reaper never runs in the middle of a call.
    async def reap_loop():
        while True:
            await asyncio.sleep(min(SANDBOX_TTL_SECONDS, 60))
            try:
                _sandbox.reap_expired()
            except Exception as e:  # a failed removal must not end the loop
                print(f"Warning: sandbox reap failed: {e}")

    _sandbox.sweep()
    reaper = asyncio.create_task(reap_loop())
    try:
        async with contextlib.AsyncExitStack() as stack:
            # Initialize the FastMCP session manager
            await stack.enter_async_context(mcp.session_manager.run())
            yield
    finally:
        reaper.cancel()
        _sandbox.destroy_all()

# Mount the StreamableHTTP server to a Starlette app
app = Starlette(
    routes=[
        Mount("/", app=mcp.streamable_http_app()),
    ],
    lifespan=lifespan
)

if __name__ == "__main__":
    # Run with uvicorn, binding to 0.0.0.0 for Docker access
    uvicorn.run(app, host="0.0.0.0", port=8000)
