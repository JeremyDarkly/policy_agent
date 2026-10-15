"""
LaunchDarkly AI Observability Setup

CRITICAL: This module must be imported FIRST before any LLM-related imports.
Import order matters for proper span capture and parent-child relationships.

Order of operations:
1. Initialize LaunchDarkly SDK with ObservabilityPlugin
2. Register framework instrumentations (via ldobserve)
3. Import LLM modules (LangChain, Bedrock, etc.)

Usage:
    from src.utils.observability import initialize_observability
    initialize_observability()  # Call before any LLM imports
"""

import os
import logging
from contextvars import ContextVar
from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider

logger = logging.getLogger(__name__)

# Track if observability has been initialized
_observability_initialized = False


class EndedSpanFilter(logging.Filter):
    """Filter to suppress 'ended span' warnings from OpenTelemetry.
    
    Background threads (like judge evaluations) may reference closed spans
    from the main request. This is expected behavior and the warnings are noise.
    """
    def filter(self, record):
        message = record.getMessage()
        # Suppress specific "ended span" warnings
        if "ended span" in message.lower() or "calling _add_event on an ended span" in message.lower():
            return False
        return True


def _env_overrides(*pairs: tuple[str, str]) -> dict[str, str]:
    """Map (keyword argument, env var) pairs to the env vars that are set and non-empty."""
    return {arg: value for arg, var in pairs if (value := os.getenv(var, "").strip())}


# LaunchDarkly groups LLM spans into conversations on this attribute alone, and
# it must be on every span of the conversation.
CONVERSATION_ID_ATTRIBUTE = "gen_ai.conversation.id"

_conversation_id: ContextVar[Optional[str]] = ContextVar("conversation_id", default=None)


class ConversationIdSpanProcessor(SpanProcessor):
    """Copy the current conversation ID onto every span as it starts."""

    def on_start(self, span, parent_context=None):
        conversation_id = _conversation_id.get()
        if conversation_id:
            span.set_attribute(CONVERSATION_ID_ATTRIBUTE, conversation_id)


def _register_conversation_id_processor() -> None:
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        logger.warning("⚠️  No SDK TracerProvider yet; spans will not carry a conversation ID")
        return
    provider.add_span_processor(ConversationIdSpanProcessor())


def bind_conversation_id(conversation_id: str) -> None:
    """Set the conversation ID for the current request.

    Spans started afterwards in this context (including threads started with
    asyncio.to_thread, which copy the context) get the ID from
    ConversationIdSpanProcessor. The span that is already current, normally the
    FastAPI server span, gets it here.
    """
    _conversation_id.set(conversation_id)
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(CONVERSATION_ID_ATTRIBUTE, conversation_id)


def initialize_observability(
    ld_sdk_key: Optional[str] = None,
    service_name: str = "togglebank-agent",
    service_version: str = "1.0.0",
    environment: Optional[str] = None
) -> bool:
    """
    Initialize LaunchDarkly AI Observability using ldobserve.
    
    Must be called BEFORE importing any LLM modules.
    
    Args:
        ld_sdk_key: LaunchDarkly SDK key (defaults to LAUNCHDARKLY_SDK_KEY env var)
        service_name: Name of the service for span attribution
        service_version: Version of the service
        environment: Environment name (e.g., 'production', 'development')
        
    Returns:
        True if successfully initialized, False otherwise
    """
    global _observability_initialized
    
    if _observability_initialized:
        logger.info("✅ Observability already initialized")
        return True
    
    try:
        # Get SDK key from env if not provided
        sdk_key = ld_sdk_key or os.getenv("LAUNCHDARKLY_SDK_KEY")
        if not sdk_key:
            logger.warning("⚠️  No LaunchDarkly SDK key found. Observability disabled.")
            return False
        
        # Get environment from env var if not provided
        if environment is None:
            environment = os.getenv("ENVIRONMENT", "development")
        
        logger.info("🔧 Initializing LaunchDarkly AI Observability with ldobserve...")
        
        # Import LaunchDarkly observability components
        try:
            import ldclient
            from ldclient.config import Config
            from ldobserve import ObservabilityConfig, ObservabilityPlugin
            
        except ImportError as e:
            logger.error(f"❌ LaunchDarkly observability packages not installed: {e}")
            logger.error("   Install: pip install launchdarkly-observability")
            return False
        
        # Configure LaunchDarkly with observability plugin
        try:
            # Optional endpoint overrides for non-production instances (e.g. staging);
            # unset or empty values keep the SDK's production defaults.
            obs_config = ObservabilityConfig(
                service_name=service_name,
                service_version=service_version,
                environment=environment,  # CRITICAL: Must match LaunchDarkly environment
                # The plugin auto-instruments every installed instrumentor. botocore
                # would add a second span to each Bedrock call, next to the
                # OpenLLMetry Bedrock span, so leave it off.
                disabled_instrumentations=["botocore"],
                **_env_overrides(
                    ("otlp_endpoint", "LAUNCHDARKLY_OTLP_ENDPOINT"),
                    ("backend_url", "LAUNCHDARKLY_OBSERVABILITY_BACKEND_URL"),
                ),
            )
            endpoint_overrides = _env_overrides(
                ("base_uri", "LAUNCHDARKLY_BASE_URI"),
                ("stream_uri", "LAUNCHDARKLY_STREAM_URI"),
                ("events_uri", "LAUNCHDARKLY_EVENTS_URI"),
            )

            config = Config(
                sdk_key,
                plugins=[
                    ObservabilityPlugin(obs_config)
                ],
                **endpoint_overrides,
            )
            
            # Initialize LaunchDarkly client
            ldclient.set_config(config)
            
            # Wait for initialization
            import time
            for _ in range(10):
                if ldclient.get().is_initialized():
                    break
                time.sleep(0.1)
            
            if ldclient.get().is_initialized():
                logger.info("✅ LaunchDarkly SDK initialized with observability plugin")
                logger.info(f"   Service: {service_name}")
                logger.info(f"   Version: {service_version}")
                logger.info(f"   Environment: {environment}")
            else:
                logger.warning("⚠️  LaunchDarkly SDK not fully initialized yet")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize LaunchDarkly with observability: {e}")
            return False
        
        # LangChain, Bedrock, OpenAI, Anthropic and FastAPI spans come from the
        # plugin's auto-instrumentation (ObservabilityPlugin.__init__), so no
        # instrumentor is registered here.
        _register_conversation_id_processor()

        # Add filter to suppress "ended span" warnings from OpenTelemetry
        # These occur when background threads (judges) reference closed spans from main request
        ended_span_filter = EndedSpanFilter()
        
        # Apply to OpenTelemetry loggers
        for logger_name in ['opentelemetry', 'opentelemetry.trace', 'opentelemetry.sdk.trace']:
            otel_logger = logging.getLogger(logger_name)
            otel_logger.addFilter(ended_span_filter)
        
        # Also apply to root logger to catch any stragglers
        logging.getLogger().addFilter(ended_span_filter)
        
        # Mark as initialized
        _observability_initialized = True
        
        logger.info("🎉 AI Observability fully initialized!")
        logger.info("   📊 Spans will appear in LaunchDarkly > Monitor > Traces")
        logger.info("   🟢 LLM spans marked with green LLM symbol")
        logger.info("   🔇 'Ended span' warnings suppressed (background threads expected)")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Observability initialization failed: {e}")
        logger.warning("   Continuing without observability...")
        return False


def is_observability_enabled() -> bool:
    """Check if observability has been successfully initialized."""
    return _observability_initialized
