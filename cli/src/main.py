import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from typing import Optional, List, Dict, Callable
import time
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.styles import Style

from .config import ConfigManager
from .client import AgentClient, ApprovalError
from . import commands as cmd_lib

# Marker used by enforcer_validator to indicate a blocked response
ENFORCER_BLOCKED_MARKER = "[ENFORCER_BLOCKED]"
MAX_ENFORCER_RETRIES = 3  # Maximum auto-retries when enforcer blocks response

def _extract_response_text(response_data) -> tuple[str, list]:
    """
    Extract model text and function outputs from ADK response.
    Returns (response_text, function_outputs)
    """
    response_text = ""
    function_outputs = []
    
    if isinstance(response_data, list):
        for event in response_data:
            content = event.get("content", {})
            role = content.get("role")
            parts = content.get("parts", [])
            
            for part in parts:
                # Extract text from model
                if "text" in part and role == "model":
                    response_text += part["text"]
                
                # Extract function_response output
                if "functionResponse" in part:
                    func_resp = part["functionResponse"]
                    response_content = func_resp.get("response", {})
                    
                    if isinstance(response_content, str):
                        function_outputs.append(response_content)
                    elif isinstance(response_content, dict):
                        text_output = response_content.get("text", response_content.get("result", str(response_content)))
                        function_outputs.append(text_output)
    else:
        # Fallback for old format
        response_text = response_data.get("response", "No response") if isinstance(response_data, dict) else ""
    
    return response_text, function_outputs

def _answer_approvals(client: AgentClient, response_data):
    """Ask the user about each confirmation the turn stopped on and answer it
    through the agent's /approvals (the same route every client uses)."""
    while isinstance(response_data, dict) and response_data.get("status") == "needs_approval":
        tool_call = response_data.get("tool_call", {})
        console.print(Panel(
            f"Tool: [bold cyan]{tool_call.get('tool_name')}[/bold cyan]\nArgs: {tool_call.get('tool_args')}",
            title="[yellow]Approval Required[/yellow]",
            border_style="yellow"
        ))
        approved = typer.confirm("Allow this tool execution?")
        try:
            with console.status("[bold green]Executing tool..." if approved else "[bold red]Denying tool..."):
                response_data = client.reply_approval(
                    tool_call.get("tool_call_id"), client.session_id, "once" if approved else "reject")
        except ApprovalError as e:
            console.print(_refusal(e))
            return []
    return response_data


def _refusal(e: ApprovalError) -> str:
    """409 is not "nothing happened": the agent was resumed with timed_out and the model moved on."""
    if e.status_code == 409:
        return "[yellow]The approval had expired; the agent was told it timed out and has moved on.[/yellow]"
    return f"[red]The agent did not take the answer ({escape(str(e))}).[/red]"

app = typer.Typer(help="DAK CLI - Decentralized Agent Kit Command Line Interface")
console = Console()
config_manager = ConfigManager()

@app.command()
def login(
    username: str = typer.Option(..., prompt="Enter your username"),
    agent_url: str = typer.Option("http://localhost:8000", prompt="Enter Agent API URL"),
):
    """
    Log in to the DAK Agent.
    """
    config_manager.set_user(username)
    config_manager.set_agent_url(agent_url)
    console.print(f"[green]Successfully logged in as [bold]{username}[/bold]![/green]")
    console.print(f"Agent URL set to: [blue]{agent_url}[/blue]")

@app.command()
def run(prompt: str):
    """
    Run a single task/prompt against the agent.
    """
    try:
        client = AgentClient()
        with console.status("[bold green]Waiting for agent response..."):
            response_data = client.run_task(prompt, permissions={"default": "ask"})
        
        response_data = _answer_approvals(client, response_data)

        # ADK returns an array of events, extract both model text and function responses
        response_text = ""
        function_outputs = []
        
        if isinstance(response_data, list):
            for event in response_data:
                content = event.get("content", {})
                role = content.get("role")
                parts = content.get("parts", [])
                
                for part in parts:
                    # Extract text from model
                    if "text" in part and role == "model":
                        response_text += part["text"]
                    
                    # Extract function_response output
                    if "functionResponse" in part:
                        func_resp = part["functionResponse"]
                        response_content = func_resp.get("response", {})
                        # response_content might be a string or dict
                        if isinstance(response_content, str):
                            function_outputs.append(response_content)
                        elif isinstance(response_content, dict):
                            # Try to extract a displayable string
                            text_output = response_content.get("text", response_content.get("result", str(response_content)))
                            function_outputs.append(text_output)
        else:
            # Fallback for old format
            response_text = response_data.get("response", "No response")
        
        # Combine outputs - function outputs first, then model text
        final_output = "\n".join(function_outputs)
        if response_text:
            if final_output:
                final_output += "\n" + response_text
            else:
                final_output = response_text
        
        if final_output:
            console.print(Panel(Markdown(final_output), title="Agent Response", border_style="blue"))
        else:
            console.print("[yellow]No response from agent[/yellow]")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")

# Slash Command Implementation

@app.command()
def chat(
    resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Resume a specific session ID")
):
    """
    Start an interactive chat session with the agent.
    Supports slash commands like /help, /history.
    """
    try:
        client = AgentClient(session_id=resume)
        user = config_manager.get_user()
        if not user:
            console.print("[red]Not logged in. Please run 'dak-cli login' first.[/red]")
            return

        ctx = cmd_lib.CommandContext(client, console, config_manager)
        
        # Load custom commands
        cmd_lib.load_markdown_commands(ctx)
        
        console.print(Panel(
            f"Welcome to DAK CLI Chat, [bold]{user}[/bold]!\n"
            f"Session ID: [bold cyan]{client.session_id}[/bold cyan]\n"
            "Type '/help' for commands, or 'exit' to quit.", 
            title="DAK Chat", 
            border_style="green"
        ))

        # Setup autocomplete using registry
        commands = cmd_lib.registry.get_all_commands()
        command_completer = WordCompleter(list(commands.keys()), ignore_case=True)
        prompt_session = PromptSession(completer=command_completer)
        
        # Custom style for the prompt
        style = Style.from_dict({
            'prompt': 'cyan bold',
        })

        while True:
            try:
                # Use prompt_toolkit for input
                user_input = prompt_session.prompt(
                    f"{user}> ", 
                    style=style
                )
                
                if not user_input.strip():
                    continue

                # Check for slash commands
                if user_input.startswith("/"):
                    parts = user_input.split()
                    cmd = parts[0].lower()
                    args = parts[1:]
                    
                    # Dispatch via registry
                    if cmd_lib.registry.get_command(cmd):
                        cmd_lib.registry.dispatch(cmd, args, ctx)
                        continue
                    else:
                        console.print(f"[yellow]Unknown command: {cmd}. Type /help for available commands.[/yellow]")
                        continue
                
                # Normal chat interaction
                if user_input.lower() in ('exit', 'quit'): # Keep legacy exit support without slash
                    break

                with console.status("[bold green]Thinking..."):
                    # Default to asking for permission for all tools for now, or configurable
                    # For this implementation, we'll set a default policy of "ask" to demonstrate the feature
                    response_data = client.run_task(user_input, permissions={"default": "ask"})
                
                response_data = _answer_approvals(client, response_data)

                # Use helper function to extract response
                response_text, function_outputs = _extract_response_text(response_data)
                
                # Combine outputs - function outputs first, then model text
                final_output = "\n".join(function_outputs)
                
                if response_text:
                    if final_output:
                        final_output += "\n" + response_text
                    else:
                        final_output = response_text
                
                # Check for ENFORCER_BLOCKED and auto-retry
                enforcer_retries = 0
                while ENFORCER_BLOCKED_MARKER in final_output and enforcer_retries < MAX_ENFORCER_RETRIES:
                    enforcer_retries += 1
                    console.print(f"[yellow]⚠️ Enforcer blocked response (retry {enforcer_retries}/{MAX_ENFORCER_RETRIES})...[/yellow]")
                    
                    # Send a retry prompt that encourages tool usage
                    retry_prompt = "You must use a tool to respond. Please use planner, ask_question, attempt_answer, or another available tool. Do not respond with plain text."
                    
                    with console.status("[bold green]Retrying with tool prompt..."):
                        response_data = client.run_task(retry_prompt, permissions={"default": "ask"})
                    
                    # Extract new response
                    response_text, function_outputs = _extract_response_text(response_data)
                    final_output = "\n".join(function_outputs)
                    if response_text:
                        if final_output:
                            final_output += "\n" + response_text
                        else:
                            final_output = response_text
                
                # If still blocked after max retries, show the error
                if ENFORCER_BLOCKED_MARKER in final_output:
                    console.print(f"[red]⚠️ Model failed to use tools after {MAX_ENFORCER_RETRIES} attempts.[/red]")
                    console.print("[yellow]The following error was returned:[/yellow]")
                
                if final_output:
                    # Filter out the marker for cleaner display
                    display_output = final_output.replace(ENFORCER_BLOCKED_MARKER, "⚠️ BLOCKED:") if ENFORCER_BLOCKED_MARKER in final_output else final_output
                    console.print(Markdown(display_output))
                else:
                    console.print("[yellow]No response from agent[/yellow]")
                console.print() # Add some spacing


            except KeyboardInterrupt:
                console.print("\n[yellow]Goodbye![/yellow]")
                break
            except Exception as e:
                console.print(f"[red]Error:[/red] {e}")
                
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")

@app.command()
def config():
    """
    Show current configuration.
    """
    user = config_manager.get_user()
    url = config_manager.get_agent_url()
    
    console.print(Panel(f"Username: [bold]{user}[/bold]\nAgent URL: [blue]{url}[/blue]", title="Current Configuration"))

@app.command()
def resume(
    session_id: Optional[str] = typer.Argument(None, help="Session ID to resume")
):
    """
    Resume a chat session. If no ID provided, allows interactive selection.
    """
    # Reuse chat command logic but initialize with specific resume logic if needed
    # Actually, 'resume' as a standalone command implies starting chat in that session.
    # So we can just call chat(resume=session_id) but we need to handle the interactive part BEFORE calling chat
    # OR we can make chat() handle the interactive part if resume is True but no ID?
    # Typer doesn't easily support "flag present but no value" for Option if it expects a string.
    # Let's keep it simple: 'dak-cli resume' calls interactive selection then starts chat.
    
    try:
        client = AgentClient() # Temp client to list sessions
        user = config_manager.get_user()
        if not user:
            console.print("[red]Not logged in. Please run 'dak-cli login' first.[/red]")
            return

        ctx = cmd_lib.CommandContext(client, console, config_manager)
        
        if not session_id:
            # Interactive selection
            cmd_lib.resume_session(ctx)
            session_id = ctx.client.session_id
            if not session_id: # User cancelled
                return

        # Start chat with selected session
        chat(resume=session_id)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")


@app.command()
def approvals(
    session: str = typer.Option(..., "--session", "-s", help="Session ID (any client's, e.g. the BFF's)"),
    user: Optional[str] = typer.Option(None, "--user", "-u", help="User ID of the session (default: logged-in user)"),
):
    """
    List the pending approvals and questions of a session.
    """
    try:
        pending = AgentClient(session_id=session).list_approvals(session, user_id=user)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    if not pending:
        console.print("[green]Nothing is pending.[/green]")
        return
    # One block per item, not a table: the ID is what `dak-cli approve` takes and must not wrap
    for item in pending:
        console.print(f"[bold]{escape(item.get('id') or '')}[/bold]  {item.get('kind')}  {item.get('status')}",
                      soft_wrap=True)
        if item.get("kind") == "question":
            lines = [*(item.get("questions") or []), item.get("context", "")]
        else:
            lines = [f"{item.get('tool_name')} {item.get('tool_args') or {}}"]
        for line in filter(None, lines):
            console.print(f"  {escape(str(line))}", soft_wrap=True)


@app.command()
def approve(
    approval_id: str = typer.Argument(..., help="ID from `dak-cli approvals`"),
    session: str = typer.Option(..., "--session", "-s", help="Session ID (any client's, e.g. the BFF's)"),
    user: Optional[str] = typer.Option(None, "--user", "-u", help="User ID of the session (default: logged-in user)"),
    reject: bool = typer.Option(False, "--reject", help="Reject instead of approving"),
    reason: str = typer.Option("", "--reason", help="Why it is rejected (reaches the model)"),
    always: bool = typer.Option(False, "--always", help="Approve this call for good"),
):
    """
    Answer a pending approval, in this or another client's session.
    """
    if reject and always:
        console.print("[red]--reject and --always cannot be used together.[/red]")
        raise typer.Exit(2)
    if reason and not reject:
        console.print("[red]--reason goes with --reject.[/red]")
        raise typer.Exit(2)
    mode = "reject" if reject else "always" if always else "once"
    client = AgentClient(session_id=session)
    try:
        with console.status("[bold green]Answering..."):
            response_data = client.reply_approval(approval_id, session, mode, reason=reason, user_id=user)
    except ApprovalError as e:
        console.print(_refusal(e))
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    if isinstance(response_data, dict) and response_data.get("status") == "needs_approval":
        tool_call = response_data["tool_call"]
        console.print(f"[yellow]Another approval is pending: {tool_call.get('tool_call_id')} "
                      f"({tool_call.get('tool_name')})[/yellow]")
        return
    response_text, function_outputs = _extract_response_text(response_data)
    output = "\n".join([*function_outputs, response_text] if response_text else function_outputs)
    console.print(Panel(Markdown(output), title=f"Answered ({mode})", border_style="blue") if output
                  else f"[green]Answered ({mode}).[/green]")


history_app = typer.Typer(help="Manage chat history")
app.add_typer(history_app, name="history")

@history_app.command("list")
def list_sessions():
    """
    List all chat sessions.
    """
    client = AgentClient()
    ctx = cmd_lib.CommandContext(client, console, config_manager)
    cmd_lib.list_sessions(ctx)

@history_app.command("show")
def show_session(session_id: str):
    """
    Show details of a specific chat session.
    """
    client = AgentClient()
    ctx = cmd_lib.CommandContext(client, console, config_manager)
    cmd_lib.show_session_history(ctx, session_id)

@history_app.command("delete")
def delete_session(session_id: str):
    """
    Delete a specific chat session.
    """
    client = AgentClient()
    ctx = cmd_lib.CommandContext(client, console, config_manager)
    if typer.confirm(f"Are you sure you want to delete session {session_id}?", default=False):
        cmd_lib.delete_session(ctx, session_id, confirm=False)
    else:
        console.print("[yellow]Deletion cancelled.[/yellow]")


if __name__ == "__main__":
    app()
