"""Runtime patches and telemetry setup applied once at agent startup."""
import functools
import logging
import os

logger = logging.getLogger(__name__)


def apply_patches() -> None:
    """Monkey-patch mcp.ClientSession so Pydantic can generate schemas for it,
    and so every MCP connection logs what it negotiated (`log_mcp_negotiation`)."""
    try:
        from mcp.client.session import ClientSession
        from pydantic import GetCoreSchemaHandler
        from pydantic_core import CoreSchema, core_schema

        def _get_pydantic_core_schema(cls, source_type, handler: GetCoreSchemaHandler) -> CoreSchema:
            return core_schema.is_instance_schema(source_type)

        ClientSession.__get_pydantic_core_schema__ = classmethod(_get_pydantic_core_schema)
        logger.info("Monkey-patched ClientSession for Pydantic compatibility.")

        # Both `initialize` and `server/discover` end in `adopt` once the
        # server has answered. Wrapped once, however often this runs.
        original_adopt = ClientSession.adopt
        if not getattr(original_adopt, "_dak_logs_negotiation", False):

            @functools.wraps(original_adopt)
            def _adopt_and_log(self, result):
                original_adopt(self, result)
                try:
                    log_mcp_negotiation(self)
                except Exception as e:  # a log line must never fail the connection
                    logger.warning(f"Could not log the MCP negotiation: {type(e).__name__}")

            _adopt_and_log._dak_logs_negotiation = True
            ClientSession.adopt = _adopt_and_log
    except ImportError:
        logger.warning("Could not monkey-patch ClientSession (mcp not installed?)")
    except Exception as e:
        logger.warning(f"Failed to monkey-patch ClientSession: {e}")


def log_mcp_negotiation(session) -> None:
    """Log what an MCP connection negotiated, as one INFO line (#672).

    Reads only the session's public `protocol_version`, `server_info` and
    `server_capabilities`, so no header, URL or credential reaches the log."""
    info = session.server_info
    server = f"{info.name}/{info.version}" if info else "unknown"
    caps = session.server_capabilities
    names = ",".join(sorted(caps.model_dump(exclude_none=True))) if caps else ""
    logger.info(f"MCP connected: protocol={session.protocol_version} server={server} capabilities={names or 'none'}")


def ensure_tracer_provider() -> None:
    """Give spans real IDs without LangFuse: an SDK TracerProvider with no exporter.

    The mcp SDK puts the current trace context in each request's `_meta.traceparent`
    (SEP-414); with OpenTelemetry's default proxy provider every ID is 0. A provider
    LangFuse already set is kept."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


def setup_telemetry() -> None:
    """Instrument google-adk with OpenTelemetry and verify LangFuse credentials if configured."""
    try:
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor
        from langfuse import get_client

        GoogleADKInstrumentor().instrument()
        logger.info("[Langfuse] Instrumentation initialized.")

        if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
            try:
                langfuse = get_client()
                if langfuse.auth_check():
                    logger.info("[Langfuse] Authentication successful - monitoring enabled")
                else:
                    logger.warning("[Langfuse] Authentication failed - check your credentials")
            except Exception as e:
                logger.warning(f"[Langfuse] Connection error: {e}")
    except Exception as e:
        logger.warning(f"[Langfuse] Skipping instrumentation: {type(e).__name__} - {e}")
    ensure_tracer_provider()
