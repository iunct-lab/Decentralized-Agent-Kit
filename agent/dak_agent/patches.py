"""Runtime patches and telemetry setup applied once at agent startup."""
import logging
import os

logger = logging.getLogger(__name__)


def apply_patches() -> None:
    """Monkey-patch mcp.ClientSession so Pydantic can generate schemas for it."""
    try:
        from mcp.client.session import ClientSession
        from pydantic import GetCoreSchemaHandler
        from pydantic_core import CoreSchema, core_schema

        def _get_pydantic_core_schema(cls, source_type, handler: GetCoreSchemaHandler) -> CoreSchema:
            return core_schema.is_instance_schema(source_type)

        ClientSession.__get_pydantic_core_schema__ = classmethod(_get_pydantic_core_schema)
        logger.info("Monkey-patched ClientSession for Pydantic compatibility.")
    except ImportError:
        logger.warning("Could not monkey-patch ClientSession (mcp not installed?)")
    except Exception as e:
        logger.warning(f"Failed to monkey-patch ClientSession: {e}")


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
