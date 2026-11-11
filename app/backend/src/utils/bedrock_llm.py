"""AWS Bedrock LLM wrapper using the Converse API with streaming support."""

import json
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Union

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

from .aws_sso import get_sso_manager
from .served_model import SOURCE_REQUESTED_FALLBACK, bedrock_served_model


def converse_tool_spec(tool: Any) -> Dict[str, Any]:
    """A Converse ``toolSpec`` for a tool in any form ``bind_tools`` accepts.

    That includes a LaunchDarkly Library tool as ``{"name", "description", "parameters"}``.
    """
    function = convert_to_openai_tool(tool)["function"]
    schema = function.get("parameters") or {"type": "object", "properties": {}}
    return {
        "toolSpec": {
            "name": function["name"],
            "description": function.get("description") or function["name"],
            "inputSchema": {"json": schema},
        }
    }


def _tool_result_content(content: Any) -> List[Dict[str, Any]]:
    """A ToolMessage's content as Converse toolResult content blocks: JSON when it parses, else text."""
    if isinstance(content, (dict, list)):
        value = content
    else:
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            return [{"text": str(content) or "(empty)"}]
    # Converse takes a JSON object here, not a bare list or scalar.
    return [{"json": value if isinstance(value, dict) else {"result": value}}]


class BedrockConverseLLM(BaseChatModel):
    """AWS Bedrock LLM using the Converse API.

    This wrapper uses Bedrock's Converse API which provides a unified interface
    across different model providers (Anthropic Claude, Meta Llama, etc.).
    """

    model_id: str
    """Bedrock model ID (e.g., 'anthropic.claude-3-sonnet-20240229-v1:0')"""

    temperature: Optional[float] = None
    """Sampling temperature (0-1). When None, it is omitted from the Converse
    request entirely -- required for models like Claude Sonnet 5 that reject the
    deprecated `temperature` inference parameter."""

    max_tokens: int = 2000
    """Maximum tokens to generate"""

    region: Optional[str] = None
    """AWS region for Bedrock"""

    profile_name: Optional[str] = None
    """AWS profile name"""

    aws_sso_manager: Any = None
    """AWS SSO manager instance"""

    _bedrock_client: Any = None
    """Cached Bedrock client to avoid creating new connections on every call"""

    def __init__(self, **kwargs):
        """Initialize Bedrock Converse LLM."""
        super().__init__(**kwargs)
        # Initialize SSO manager
        self.aws_sso_manager = get_sso_manager(
            profile_name=self.profile_name, region=self.region
        )
        # Create and cache the Bedrock client ONCE
        self._bedrock_client = self.aws_sso_manager.get_bedrock_client("bedrock-runtime")
        print(f"✅ Bedrock client cached for model {self.model_id}")

    @property
    def _llm_type(self) -> str:
        """Return type of LLM."""
        return "bedrock-converse"

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get identifying parameters."""
        return {
            "model_id": self.model_id,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "region": self.region,
        }

    def _convert_messages_to_converse_format(
        self, messages: List[BaseMessage]
    ) -> tuple[List[Dict], Optional[List[Dict]]]:
        """Convert LangChain messages to Bedrock Converse format.

        Args:
            messages: List of LangChain messages

        Returns:
            Tuple of (messages, system_messages) in Bedrock format
        """
        converse_messages: List[Dict[str, Any]] = []
        system_messages = []

        def add(role: str, blocks: List[Dict[str, Any]]) -> None:
            # Converse needs roles to alternate, so a tool result and the next user turn
            # (or several tool results) share one user message.
            if converse_messages and converse_messages[-1]["role"] == role:
                converse_messages[-1]["content"].extend(blocks)
            else:
                converse_messages.append({"role": role, "content": blocks})

        for message in messages:
            if isinstance(message, SystemMessage):
                system_messages.append({"text": message.content})
            elif isinstance(message, HumanMessage):
                add("user", [{"text": message.content}])
            elif isinstance(message, AIMessage):
                blocks: List[Dict[str, Any]] = []
                text = message.content if isinstance(message.content, str) else ""
                if text or not message.tool_calls:
                    blocks.append({"text": text})
                for call in message.tool_calls:
                    blocks.append(
                        {"toolUse": {"toolUseId": call["id"], "name": call["name"], "input": call.get("args") or {}}}
                    )
                add("assistant", blocks)
            elif isinstance(message, ToolMessage):
                result: Dict[str, Any] = {
                    "toolUseId": message.tool_call_id,
                    "content": _tool_result_content(message.content),
                }
                if getattr(message, "status", "success") == "error":
                    result["status"] = "error"
                add("user", [{"toolResult": result}])
            else:
                # Default to user message
                add("user", [{"text": str(message.content)}])

        return converse_messages, system_messages if system_messages else None

    def bind_tools(
        self,
        tools: Sequence[Union[Dict[str, Any], type, Callable, Any]],
        **kwargs: Any,
    ):
        """Bind tools: each request then sends them as the Converse ``toolConfig``.

        Accepts what LangChain's ``bind_tools`` does, plus LaunchDarkly Library tools as
        ``{"name", "description", "parameters"}``. The response's ``toolUse`` blocks come back as
        ``AIMessage.tool_calls``. Tool calls work through ``invoke``; ``stream`` returns text only.
        """
        return self.bind(tools=[converse_tool_spec(t) for t in tools], **kwargs)

    def _request(self, messages: List[BaseMessage], stop: Optional[List[str]], **kwargs: Any) -> Dict[str, Any]:
        """The Converse request for these messages and options."""
        converse_messages, system_messages = self._convert_messages_to_converse_format(messages)
        inference_config: Dict[str, Any] = {
            "maxTokens": kwargs.get("max_tokens", self.max_tokens),
        }
        # Omit temperature when unset: some models (e.g. Claude Sonnet 5)
        # reject the deprecated `temperature` parameter outright.
        temperature = kwargs.get("temperature", self.temperature)
        if temperature is not None:
            inference_config["temperature"] = temperature
        if stop:
            inference_config["stopSequences"] = stop
        api_params: Dict[str, Any] = {
            "modelId": self.model_id,
            "messages": converse_messages,
            "inferenceConfig": inference_config,
        }
        if system_messages:
            api_params["system"] = system_messages
        if kwargs.get("tools"):
            api_params["toolConfig"] = {"tools": kwargs["tools"]}
        return api_params

    def _converse_result(self, response: Dict[str, Any]) -> ChatResult:
        """Build a ChatResult from a Converse response.

        ``model_name`` is the model that served the call (what OpenLLMetry records as ``gen_ai.response.model``).
        ``model_id`` stays the requested ID.
        """
        output_message = response.get("output", {}).get("message", {})
        content_blocks = output_message.get("content", [])
        response_text = "".join(
            block.get("text", "") for block in content_blocks if "text" in block
        )
        tool_calls = [
            {
                "name": block["toolUse"]["name"],
                "args": block["toolUse"].get("input") or {},
                "id": block["toolUse"]["toolUseId"],
                "type": "tool_call",
            }
            for block in content_blocks
            if "toolUse" in block
        ]

        usage = response.get("usage", {})
        token_usage = {
            "input_tokens": usage.get("inputTokens", 0),
            "output_tokens": usage.get("outputTokens", 0),
            "total_tokens": usage.get("totalTokens", 0),
        }
        stop_reason = response.get("stopReason")
        served, source = bedrock_served_model(response, self.model_id)

        message = AIMessage(
            content=response_text,
            tool_calls=tool_calls,
            response_metadata={
                "model_id": self.model_id,
                "model_name": served,
                "served_model_source": source,
                "stop_reason": stop_reason,
                "token_usage": token_usage,
            },
        )
        message.usage_metadata = token_usage

        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={
                "token_usage": token_usage,
                "model_id": self.model_id,
                "model_name": served,
                "served_model_source": source,
                "stop_reason": stop_reason,
            },
        )

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Generate a response from Bedrock Converse API (non-streaming).

        Uses the synchronous converse() API so that the full request duration is
        captured by the auto-instrumented LangChain OTEL span for this call.
        For streaming with TTFT tracking, callers should use stream() instead.

        Args:
            messages: List of messages
            stop: Stop sequences
            run_manager: Callback manager
            **kwargs: Additional arguments

        Returns:
            ChatResult with the response
        """
        bedrock_client = self._bedrock_client
        api_params = self._request(messages, stop, **kwargs)

        try:
            response = bedrock_client.converse(**api_params)
            return self._converse_result(response)

        except Exception as e:
            if "credentials" in str(e).lower() or "expired" in str(e).lower():
                print("🔄 Credentials may be expired, attempting refresh...")
                if self.aws_sso_manager.force_refresh():
                    self._bedrock_client = self.aws_sso_manager.get_bedrock_client(
                        "bedrock-runtime"
                    )

                    response = self._bedrock_client.converse(**api_params)
                    return self._converse_result(response)

            raise

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGeneration]:
        """Stream responses from Bedrock using converseStream API.
        
        Yields chunks as they arrive from Bedrock.
        """
        bedrock_client = self._bedrock_client
        converse_messages, system_messages = self._convert_messages_to_converse_format(
            messages
        )
        inference_config = {
            "maxTokens": kwargs.get("max_tokens", self.max_tokens),
        }
        # Omit temperature when unset: some models (e.g. Claude Sonnet 5)
        # reject the deprecated `temperature` parameter outright.
        temperature = kwargs.get("temperature", self.temperature)
        if temperature is not None:
            inference_config["temperature"] = temperature
        if stop:
            inference_config["stopSequences"] = stop
        api_params = {
            "modelId": self.model_id,
            "messages": converse_messages,
            "inferenceConfig": inference_config,
        }
        if system_messages:
            api_params["system"] = system_messages

        try:
            start_time = time.time()
            ttft_ms = None
            
            response_stream = bedrock_client.converse_stream(**api_params)
            
            accumulated_text = ""
            token_usage = {}
            stop_reason = None
            served, source = self.model_id, SOURCE_REQUESTED_FALLBACK
            
            for event in response_stream["stream"]:
                # Capture TTFT on first content delta
                if ttft_ms is None and "contentBlockDelta" in event:
                    ttft_ms = int((time.time() - start_time) * 1000)
                
                # Yield each chunk as it arrives
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})
                    if "text" in delta:
                        chunk_text = delta["text"]
                        accumulated_text += chunk_text
                        
                        # Create a chunk with the incremental text
                        message = AIMessage(
                            content=chunk_text,
                            response_metadata={
                                "model_id": self.model_id,
                                "ttft_ms": ttft_ms,
                                "is_chunk": True,
                            },
                        )
                        yield ChatGeneration(message=message)
                
                # Capture final metadata
                if "metadata" in event:
                    metadata = event["metadata"]
                    usage = metadata.get("usage", {})
                    token_usage = {
                        "input_tokens": usage.get("inputTokens", 0),
                        "output_tokens": usage.get("outputTokens", 0),
                        "total_tokens": usage.get("totalTokens", 0),
                    }
                    stop_reason = metadata.get("stopReason")
                    served, source = bedrock_served_model(metadata, self.model_id)
            
            # Yield final metadata chunk
            if ttft_ms is None:
                ttft_ms = int((time.time() - start_time) * 1000)
            
            final_message = AIMessage(
                content="",  # Empty content for metadata-only message
                response_metadata={
                    "model_id": self.model_id,
                    "model_name": served,
                    "served_model_source": source,
                    "stop_reason": stop_reason,
                    "token_usage": token_usage,
                    "ttft_ms": ttft_ms,
                    "is_final": True,
                },
            )
            final_message.usage_metadata = token_usage
            yield ChatGeneration(message=final_message)

        except Exception as e:
            if "credentials" in str(e).lower() or "expired" in str(e).lower():
                print("🔄 Credentials may be expired, attempting refresh...")
                if self.aws_sso_manager.force_refresh():
                    self._bedrock_client = self.aws_sso_manager.get_bedrock_client(
                        "bedrock-runtime"
                    )
                    
                    # Retry streaming after refresh
                    start_time = time.time()
                    ttft_ms = None
                    
                    response_stream = self._bedrock_client.converse_stream(**api_params)
                    
                    accumulated_text = ""
                    token_usage = {}
                    stop_reason = None
                    served, source = self.model_id, SOURCE_REQUESTED_FALLBACK
                    
                    for event in response_stream["stream"]:
                        if ttft_ms is None and "contentBlockDelta" in event:
                            ttft_ms = int((time.time() - start_time) * 1000)
                        
                        if "contentBlockDelta" in event:
                            delta = event["contentBlockDelta"].get("delta", {})
                            if "text" in delta:
                                chunk_text = delta["text"]
                                accumulated_text += chunk_text
                                
                                message = AIMessage(
                                    content=chunk_text,
                                    response_metadata={
                                        "model_id": self.model_id,
                                        "ttft_ms": ttft_ms,
                                        "is_chunk": True,
                                    },
                                )
                                yield ChatGeneration(message=message)
                        
                        if "metadata" in event:
                            metadata = event["metadata"]
                            usage = metadata.get("usage", {})
                            token_usage = {
                                "input_tokens": usage.get("inputTokens", 0),
                                "output_tokens": usage.get("outputTokens", 0),
                                "total_tokens": usage.get("totalTokens", 0),
                            }
                            stop_reason = metadata.get("stopReason")
                            served, source = bedrock_served_model(metadata, self.model_id)
                    
                    if ttft_ms is None:
                        ttft_ms = int((time.time() - start_time) * 1000)
                    
                    final_message = AIMessage(
                        content="",
                        response_metadata={
                            "model_id": self.model_id,
                            "model_name": served,
                            "served_model_source": source,
                            "stop_reason": stop_reason,
                            "token_usage": token_usage,
                            "ttft_ms": ttft_ms,
                            "is_final": True,
                        },
                    )
                    final_message.usage_metadata = token_usage
                    yield ChatGeneration(message=final_message)

            raise


# Common Bedrock model IDs
BEDROCK_MODELS = {
    # Anthropic Claude models
    "claude-3-sonnet": "anthropic.claude-3-sonnet-20240229-v1:0",
    "claude-3-haiku": "anthropic.claude-3-haiku-20240307-v1:0",
    "claude-3-opus": "anthropic.claude-3-opus-20240229-v1:0",
    "claude-3-5-sonnet": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    # Meta Llama models
    "llama-3-8b": "meta.llama3-8b-instruct-v1:0",
    "llama-3-70b": "meta.llama3-70b-instruct-v1:0",
    "llama-3-1-8b": "meta.llama3-1-8b-instruct-v1:0",
    "llama-3-1-70b": "meta.llama3-1-70b-instruct-v1:0",
    "llama-3-1-405b": "meta.llama3-1-405b-instruct-v1:0",
    # Amazon Titan
    "titan-text-express": "amazon.titan-text-express-v1",
    "titan-text-lite": "amazon.titan-text-lite-v1",
}


def get_bedrock_model_id(model_name: str) -> str:
    """Get full Bedrock model ID from short name.

    Args:
        model_name: Short model name or full model ID

    Returns:
        Full Bedrock model ID
    """
    # If it's already a full model ID, return it
    if "." in model_name and ":" in model_name:
        return model_name

    # Otherwise, look up the short name
    return BEDROCK_MODELS.get(model_name, model_name)
