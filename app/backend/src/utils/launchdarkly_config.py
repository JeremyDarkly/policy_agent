"""LaunchDarkly AI Config integration."""

import os
import re
import time
from functools import lru_cache
from typing import Any, Optional

import ldclient
from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.messages.ai import add_usage
from ldclient import Context
from launchdarkly_ai_server import parse_ai_config, parse_template

from .ai_tracking import ConfigTracker, TokenUsage, track_data_for
from .served_model import CONTENT_FILTER_MESSAGE, is_content_filtered

# Load environment variables
load_dotenv()


def _fill(template: str, variables: dict[str, Any]) -> str:
    """Fill ``{{variable}}`` placeholders, and blank the ones ``variables`` has no value for.

    The AI SDK's ``parse_template`` leaves an unknown placeholder in place. The agents were
    written against the legacy SDK, which rendered it empty (mustache), and add some of those
    values themselves (account_specialist appends the account profile), so that is kept.
    """
    return re.sub(r"\{\{[\w.]+\}\}", "", parse_template(template, variables))


def _tools_of(config: dict[str, Any]) -> list[dict[str, Any]]:
    """The variation's tools as {name, description, parameters}: the root ``tools`` map, or else
    ``model.parameters.tools`` (where tools were set before the tools library)."""
    tools = config.get("tools")
    if isinstance(tools, dict) and tools:
        entries = list(tools.values())  # parse_ai_config has checked each has a name, type and parameters
    else:
        listed = ((config.get("model") or {}).get("parameters") or {}).get("tools")
        entries = [t for t in listed or [] if isinstance(t, dict) and t.get("name")]
    return [{k: v for k, v in t.items() if k in ("name", "description", "type", "parameters", "customParameters")}
            for t in entries]


class LaunchDarklyClient:
    """LaunchDarkly client wrapper for AI Configs."""

    def __init__(self):
        """Initialize LaunchDarkly client.
        
        Raises:
            ValueError: If LaunchDarkly is not enabled or SDK key is missing
        """
        self.sdk_key = os.getenv("LAUNCHDARKLY_SDK_KEY")
        self.enabled = os.getenv("LAUNCHDARKLY_ENABLED", "false").lower() == "true"

        if not self.enabled:
            raise ValueError(
                "LaunchDarkly is required. Set LAUNCHDARKLY_ENABLED=true in your environment."
            )
        
        if not self.sdk_key:
            raise ValueError(
                "LAUNCHDARKLY_SDK_KEY is required. Set it in your environment variables."
            )

        # DON'T call ldclient.set_config() here - observability.py already initialized it!
        # Calling set_config() again would overwrite the ObservabilityPlugin.
        # Just get the existing client that was initialized with the plugin.
        
        # Wait briefly for async initialization (LaunchDarkly SDK initializes asynchronously)
        import time
        max_wait = 5  # seconds
        start_time = time.time()
        while not ldclient.get().is_initialized():
            if time.time() - start_time > max_wait:
                raise RuntimeError(
                    "LaunchDarkly SDK failed to initialize. Please check your internet connection "
                    "and SDK credential for any typo."
                )
            time.sleep(0.1)
        
        # Use the already-initialized client (with ObservabilityPlugin from observability.py)
        self.client = ldclient.get()
        print("✅ LaunchDarkly AI Config client ready (using observability-enabled SDK)")

    def get_ai_config(
        self,
        config_key: str,
        context: Optional[dict[str, Any]] = None,
        default_config: Optional[dict[str, Any]] = None,
    ) -> tuple[dict[str, Any], ConfigTracker, Context]:
        """Evaluate an AI Config and return (config dict, tracker, LD context).

        The variation is read and validated as the AI SDK reads it (``launchdarkly_ai_server``'s
        ``extract_variation``: the flag value, its ``_ldMeta``, then ``parse_ai_config``). The
        config dict has ``_instructions`` for an agent-mode variation, with ``{{variable}}`` and
        ``{{ldctx.attribute}}`` placeholders filled from ``context``, or ``messages`` for a
        completion-mode one, with only ``{{ldctx.attribute}}`` filled. Both as the legacy SDK did.

        Args:
            config_key: The AI config key in LaunchDarkly
            context: User/session context for targeting, and the template variables
            default_config: Served (as variation ``default-fallback``) when the config is missing,
                disabled or invalid. Without one those raise.

        Raises:
            RuntimeError: If the config can't be served and there is no default_config
        """
        ld_context = self._create_context(context or {})
        try:
            raw = self.client.variation(config_key, ld_context, None)
        except Exception as e:
            raw = None
            problem = f"evaluation failed: {e}"
        else:
            problem = "not found in LaunchDarkly"

        meta: dict[str, Any] = {}
        config: Optional[dict[str, Any]] = None
        if isinstance(raw, dict):
            meta = raw.get("_ldMeta") or {}
            if not meta.get("enabled"):
                problem = "is disabled (targeting serves a disabled variation)"
            else:
                parsed = parse_ai_config({k: v for k, v in raw.items() if k != "_ldMeta"})
                if parsed.success:
                    config = parsed.data
                else:
                    problem = f"is invalid: {parsed.error['message']}"

        if config is None:
            if default_config:
                print(f"⚠️  AI config '{config_key}' {problem} - using default fallback")
                config_dict = self._default_config_dict(default_config)
                tracker = ConfigTracker(self.client, ld_context, track_data_for(
                    config_key,
                    {"model": default_config.get("model") or {}, "provider": {"name": config_dict["provider"]}},
                    None,
                ))
                return config_dict, tracker, ld_context
            error_msg = f"CATASTROPHIC: AI config '{config_key}' {problem}!"
            print(f"❌ {error_msg}")
            raise RuntimeError(error_msg)

        variables: dict[str, Any] = {
            k: str(v) for k, v in (context or {}).items() if isinstance(v, (str, int, float, bool))
        }
        variables["ldctx"] = ld_context.to_dict()

        model = config.get("model") or {}
        config_dict: dict[str, Any] = {
            "enabled": True,
            "provider": (config.get("provider") or {}).get("name", ""),
            "_variation": meta.get("variationKey") or "unknown",
            "model": {"name": model.get("name", ""), "parameters": model.get("parameters") or {}},
        }
        # Custom parameters (like awskbid or baseModel) set on the model config
        if model.get("custom"):
            config_dict["model"]["custom"] = model["custom"]
        if config.get("custom"):
            config_dict["_custom"] = config["custom"]

        if isinstance(config.get("instructions"), str):
            # Agent mode ("Goal or task" in the UI); instructions are kept apart from messages
            config_dict["_instructions"] = _fill(config["instructions"], variables)
            # Library tools attached to the served variation, as {name, description, parameters}.
            # The tool loop (utils/tool_loop.py) runs the ones the backend implements.
            config_dict["tools"] = _tools_of(config)
        else:
            config_dict["messages"] = [
                {"role": m.get("role", "user"), "content": _fill(m.get("content") or "", {"ldctx": variables["ldctx"]})}
                for m in config.get("messages") or []
            ]

        tracker = ConfigTracker(self.client, ld_context, track_data_for(config_key, config, meta))
        return config_dict, tracker, ld_context

    def _create_context(self, context_dict: dict[str, Any]) -> Context:
        """Create LaunchDarkly context from dictionary.

        Args:
            context_dict: Context attributes

        Returns:
            LaunchDarkly Context object
        """
        # Extract user key (or use default)
        user_key = context_dict.get("user_key", "anonymous")

        # Build context with additional attributes
        context_builder = Context.builder(user_key)

        # Add custom attributes
        for key, value in context_dict.items():
            if key != "user_key":
                context_builder.set(key, value)

        return context_builder.build()

    def _default_config_dict(self, default_config: dict[str, Any]) -> dict[str, Any]:
        """A caller's fallback config, in the shape get_ai_config returns."""
        model = default_config.get("model") or {}
        provider = default_config.get("provider") or os.getenv("LLM_PROVIDER", "bedrock")
        if isinstance(provider, dict):
            provider = provider.get("name", "")
        config_dict: dict[str, Any] = {
            "enabled": default_config.get("enabled", True),
            "provider": provider,
            "_variation": "default-fallback",
            "model": {
                "name": model.get("name") or os.getenv("LLM_MODEL", "claude-3-5-sonnet"),
                "parameters": model.get("parameters") or {"maxTokens": 2000},
            },
        }
        if default_config.get("instructions"):
            config_dict["_instructions"] = default_config["instructions"]
            config_dict["tools"] = []
        else:
            config_dict["messages"] = [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in default_config.get("messages") or []
            ]
        return config_dict

    def build_langchain_messages(self, ld_config: dict[str, Any], context_vars: dict[str, Any]) -> list:
        """Build LangChain messages from LaunchDarkly config (agent-based or completion-based).
        
        Args:
            ld_config: The LaunchDarkly AI config dict
            context_vars: Variables for template replacement
            
        Returns:
            List of LangChain message objects (SystemMessage, HumanMessage, AIMessage)
        """
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
        import json
        
        langchain_messages = []
        
        if "_instructions" in ld_config:
            # Agent-based config: instructions + user query
            instructions = ld_config["_instructions"]
            # Replace template variables in instructions
            for key, value in context_vars.items():
                instructions = instructions.replace(f"{{{{{key}}}}}", str(value))
                instructions = instructions.replace(f"{{{{ldctx.{key}}}}}", str(value))
            
            # System message with instructions, then user message with query
            query = context_vars.get("query", "")
            user_context = {k: v for k, v in context_vars.items() if k != "query"}
            langchain_messages = [
                SystemMessage(content=instructions),
                HumanMessage(content=f"User query: {query}\n\nUser context:\n{json.dumps(user_context, indent=2, default=str)}")
            ]
        elif "messages" in ld_config:
            # Completion-based config: use messages from LaunchDarkly
            ld_messages = ld_config["messages"]
            formatted_messages = self.format_messages(ld_messages, context_vars)
            
            # Convert to LangChain message format
            has_user_message = False
            for msg in formatted_messages:
                if msg["role"] == "system":
                    langchain_messages.append(SystemMessage(content=msg["content"]))
                elif msg["role"] == "user":
                    langchain_messages.append(HumanMessage(content=msg["content"]))
                    has_user_message = True
                else:
                    langchain_messages.append(AIMessage(content=msg["content"]))
            
            # AWS Bedrock requires conversations to start with a user message
            # If we only have a system message, add a user message with query
            if not has_user_message and langchain_messages:
                query = context_vars.get("query", "")
                user_context_minimal = {k: v for k, v in context_vars.items() if k not in ["query"]}
                user_msg = f"User query: {query}\n\nContext:\n{json.dumps(user_context_minimal, indent=2, default=str)}"
                langchain_messages.append(HumanMessage(content=user_msg))
        else:
            raise RuntimeError(
                f"❌ CATASTROPHIC: No messages or instructions found in LaunchDarkly AI Config!\n"
                f"  This means the config exists but has no prompt/instructions configured.\n"
                f"  Please configure either:\n"
                f"  - For agent-based: Set 'Goal or task' field\n"
                f"  - For completion-based: Add messages in 'Prompt' section"
            )
        
        return langchain_messages

    def format_messages(self, messages: list[dict], context_vars: dict[str, Any]) -> list[dict]:
        """Format LaunchDarkly messages with context variables.
        
        Replaces template variables like {{ldctx.name}} or {{variable_name}} with actual values.
        
        Args:
            messages: List of message dicts with 'role' and 'content' from LaunchDarkly
            context_vars: Dictionary of variables to substitute
            
        Returns:
            List of formatted messages
        """
        import re
        
        formatted_messages = []
        for msg in messages:
            content = msg.get("content", "")
            
            # Replace {{ldctx.attribute}} with context values
            def replace_ldctx(match):
                attr = match.group(1)
                return str(context_vars.get(attr, f"{{{{ldctx.{attr}}}}}"))  # Keep original if not found
            
            content = re.sub(r'\{\{ldctx\.(\w+)\}\}', replace_ldctx, content)
            
            # Replace {{variable_name}} with values
            def replace_var(match):
                var = match.group(1)
                return str(context_vars.get(var, f"{{{{{var}}}}}"))  # Keep original if not found
            
            content = re.sub(r'\{\{(\w+)\}\}', replace_var, content)
            
            formatted_messages.append({
                "role": msg.get("role", "user"),
                "content": content
            })
        
        return formatted_messages

    def close(self):
        """Close the LaunchDarkly client."""
        if self.client:
            self.client.close()


class NoOpTracker:
    """No-op tracker for when LaunchDarkly is disabled.
    
    Matches LaunchDarkly tracker API.
    """

    def track_duration_of(self, func):
        """Execute function and return result (no tracking)."""
        return func()

    def track_success(self):
        """No-op track success (no arguments)."""
        pass

    def track_error(self):
        """No-op track error (no arguments)."""
        pass

    def track_tokens(self, token_usage):
        """No-op track tokens."""
        pass

    def track_duration(self, duration):
        """No-op track duration."""
        pass

    def track_time_to_first_token(self, time_to_first_token):
        """No-op track time to first token."""
        pass

    def track_tool_call(self, tool_key):
        """No-op track tool call."""
        pass


def token_usage_from(usage: Optional[dict]) -> Optional[TokenUsage]:
    """The ``TokenUsage`` to track for a LangChain ``usage_metadata`` block, or None if there is none.

    Some reasoning models report reasoning tokens outside ``output_tokens``: Grok on Azure AI Foundry returned
    input 11, output 1, reasoning 97, total 109. Those tokens are billed, so they are counted as output. OpenAI
    reasoning models already include them in ``output_tokens`` (total = input + output), so reasoning is added only
    up to the gap between ``total_tokens`` and input + output, never twice. ``total`` is the provider's
    ``total_tokens`` when it sends one.
    """
    if not usage:
        return None
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    reasoning = (usage.get("output_token_details") or {}).get("reasoning") or 0
    total = usage.get("total_tokens")
    if total:
        uncounted = total - input_tokens - output_tokens
        if reasoning > 0 and uncounted > 0:
            output_tokens += min(reasoning, uncounted)
    else:
        total = input_tokens + output_tokens
    return TokenUsage(input=input_tokens, output=output_tokens, total=total)


class ModelInvoker:
    """Helper class to invoke models with tracking.
    
    Follows the LaunchDarkly AI SDK tracking pattern from:
    https://github.com/launchdarkly/hello-python-ai/blob/main/examples/langchain_example.py
    """

    def __init__(
        self,
        model: BaseChatModel,
        tracker: Any,
        config_key: str = "",
        is_agent_config: bool = False,
        user_context: Optional[Context] = None,
        skip_span_annotation: bool = False,
        variation_key: str = "",
        provider: str = "",
        expected_model: str = "",
    ):
        """Initialize model invoker.

        Args:
            model: The LangChain model to invoke
            tracker: LaunchDarkly tracker for metrics
            config_key: The AI Config key (for observability linking)
            is_agent_config: Whether this is an agent-based config
            user_context: LaunchDarkly user context (for span annotation)
            skip_span_annotation: If True, skip all span annotation (for background threads)
            variation_key: The served variation's key (sent with the served-model events)
            provider: Provider name (bedrock, openai, anthropic, azure ai foundry)
            expected_model: The model the served one is checked against, when the request's model ID isn't a model
                (an Azure AI Foundry deployment name; from the model config's ``baseModel``). Empty: the request's.
        """
        self.model = model
        self.tracker = tracker
        self.config_key = config_key
        self._is_agent_config = is_agent_config
        self.user_context = user_context
        self.skip_span_annotation = skip_span_annotation
        self.variation_key = variation_key
        self.provider = provider
        self.expected_model = expected_model

    def _annotate_current_span(self) -> None:
        """Set ld.ai_config.key on the current span (the agent's node span).

        The AI Config was evaluated inside this span (get_model_invoker runs in
        the node), so the observability plugin's flag hook has already added the
        feature_flag event that links the trace to the config.
        """
        # Background threads (judges) have no request span to annotate
        if self.skip_span_annotation or not self.config_key:
            return
        try:
            from opentelemetry import trace

            current_span = trace.get_current_span()
            if current_span.is_recording():
                current_span.set_attribute("ld.ai_config.key", self.config_key)
        except Exception:
            # Don't let span annotation break LLM calls
            pass

    def invoke(self, messages: list[BaseMessage]) -> Any:
        """Invoke the model with tracking.

        Args:
            messages: List of messages to send to the model

        Returns:
            Model response
        """
        return self.run_tracked(lambda: self.model.invoke(messages))

    def run_tracked(self, run: Any) -> Any:
        """Run one generation with tracking: ``run()`` returns the final message.

        The tracker records duration, success, tokens and TTFT once per request, so a
        generation that takes several model calls (a tool loop) is run through here as one,
        with its token usage summed on the message it returns.
        """
        try:
            self._annotate_current_span()

            # Track the LLM call
            result = self.tracker.track_duration_of(run)

            # Azure withholds a filtered response and says so in finish_reason; count it as an error, not a reply
            metadata = getattr(result, "response_metadata", None)
            if is_content_filtered(metadata):
                self.report_content_filtered(metadata)
                raise ValueError(CONTENT_FILTER_MESSAGE)

            # Track success (no arguments)
            self.tracker.track_success()
            
            # Extract and track token usage if available
            token_usage = token_usage_from(getattr(result, "usage_metadata", None))
            if token_usage is not None:
                self.tracker.track_tokens(token_usage)
            
            # Extract and track Time to First Token (TTFT) if available
            ttft_ms = None
            if hasattr(result, "response_metadata") and isinstance(result.response_metadata, dict):
                ttft_ms = result.response_metadata.get("ttft_ms")
            elif hasattr(result, "generations") and len(result.generations) > 0:
                gen = result.generations[0]
                if hasattr(gen, "message") and hasattr(gen.message, "response_metadata"):
                    ttft_ms = gen.message.response_metadata.get("ttft_ms")
            
            # Track TTFT in LaunchDarkly if available
            if ttft_ms is not None:
                try:
                    self.tracker.track_time_to_first_token(ttft_ms)
                except Exception as e:
                    # Don't fail if TTFT tracking fails
                    import logging
                    logging.debug(f"Failed to track TTFT: {e}")
            
            self.report_served_model_of(getattr(result, "response_metadata", None))
            
            return result

        except Exception as e:
            # Track error (no arguments)
            self.tracker.track_error()
            
            # Mark span as error if available
            try:
                from opentelemetry import trace
                span = trace.get_current_span()
                if span and span.is_recording():
                    from opentelemetry.trace import Status, StatusCode
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
            except:
                pass
                
            raise
    
    def stream(self, messages: list[BaseMessage]):
        """Stream the model response with tracking.
        
        Args:
            messages: List of messages to send to the model
            
        Yields:
            Chunks from the model as they arrive
        """
        try:
            self._annotate_current_span()

            # Track start time for duration
            import time
            start_time = time.time()
            
            # Stream from the model
            usage = None
            ttft_ms = None
            served_metadata = None
            
            for chunk in self.model.stream(messages):
                chunk_message = getattr(chunk, "message", chunk)
                chunk_meta = getattr(chunk_message, "response_metadata", None) or {}
                if served_metadata is None and chunk_meta.get("model_name"):
                    served_metadata = chunk_meta
                
                # Extract TTFT from first chunk
                if ttft_ms is None:
                    chunk_metadata = chunk.message.response_metadata if hasattr(chunk, 'message') else {}
                    if isinstance(chunk_metadata, dict):
                        ttft_ms = chunk_metadata.get("ttft_ms")
                
                # Providers split usage across chunks (Anthropic sends input first and output last), so add it up
                chunk_usage = getattr(chunk_message, "usage_metadata", None)
                if chunk_usage:
                    usage = chunk_usage if usage is None else add_usage(usage, chunk_usage)
                
                yield chunk
            
            # Track metrics after streaming completes
            duration_ms = int((time.time() - start_time) * 1000)
            
            # Track success
            self.tracker.track_success()
            
            # Track duration manually
            self.tracker.track_duration(duration_ms)
            
            # Track tokens if we got them
            token_usage = token_usage_from(usage)
            if token_usage is not None and token_usage.total > 0:
                self.tracker.track_tokens(token_usage)
            
            # Track TTFT if we got it
            if ttft_ms is not None:
                try:
                    self.tracker.track_time_to_first_token(ttft_ms)
                except Exception as e:
                    import logging
                    logging.debug(f"Failed to track TTFT: {e}")
            
            self.report_served_model_of(served_metadata)
        
        except Exception as e:
            self.tracker.track_error()
            
            try:
                from opentelemetry import trace
                span = trace.get_current_span()
                if span and span.is_recording():
                    from opentelemetry.trace import Status, StatusCode
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
            except:
                pass
            
            raise

    def _served_event_data(self, metadata: Optional[dict]) -> dict:
        """Keyword args for the served-model events: the expected model, the served one and the deployment."""
        from .served_model import requested_model_id, served_model

        sent = requested_model_id(self.model)
        requested = self.expected_model or sent
        served, source = served_model(metadata, requested)
        return {
            "config_key": self.config_key,
            "variation_key": self.variation_key,
            "provider": self.provider,
            "requested": requested,
            "served": served,
            "source": source,
            "deployment": sent if self.expected_model else "",
        }

    def report_served_model_of(self, metadata: Optional[dict]) -> None:
        """Send which model served this call to LaunchDarkly (see utils/served_model.py)."""
        if self.user_context is None:
            return
        try:
            from .served_model import report_served_model

            event = self._served_event_data(metadata)
            mismatch = report_served_model(ldclient.get(), self.user_context, **event)
            if mismatch:
                print(f"  ⚠️ {self.config_key}: requested {event['requested']}, served by {event['served']}")
        except Exception as e:
            import logging
            logging.debug(f"Failed to report served model: {e}")

    def report_content_filtered(self, metadata: Optional[dict]) -> None:
        """Send ``ai-content-filtered`` for a call a content filter blocked (see utils/served_model.py)."""
        print(f"  ⚠️ {self.config_key}: response blocked by a content filter")
        if self.user_context is None:
            return
        try:
            from .served_model import report_content_filtered

            report_content_filtered(ldclient.get(), self.user_context, **self._served_event_data(metadata))
        except Exception as e:
            import logging
            logging.debug(f"Failed to report content filter: {e}")

    def _extract_tokens(self, response: Any) -> dict[str, int]:
        """Extract token counts from response.

        Args:
            response: Model response

        Returns:
            Dictionary with token counts
        """
        tokens = {"input": 0, "output": 0, "total": 0}

        try:
            # Try to get usage metadata from response
            if hasattr(response, "usage_metadata"):
                usage = response.usage_metadata
                tokens["input"] = usage.get("input_tokens", 0)
                tokens["output"] = usage.get("output_tokens", 0)
                tokens["total"] = usage.get("total_tokens", 0)
            elif hasattr(response, "response_metadata"):
                # Alternative location for token data
                metadata = response.response_metadata
                if "token_usage" in metadata:
                    usage = metadata["token_usage"]
                    tokens["input"] = usage.get("prompt_tokens", 0)
                    tokens["output"] = usage.get("completion_tokens", 0)
                    tokens["total"] = usage.get("total_tokens", 0)
        except Exception:
            # If we can't extract tokens, just return zeros
            pass

        return tokens


@lru_cache(maxsize=1)
def get_ld_client() -> LaunchDarklyClient:
    """Get the singleton LaunchDarkly client.

    Returns:
        LaunchDarkly client instance
    """
    return LaunchDarklyClient()
