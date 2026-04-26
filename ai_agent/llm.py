"""Provider-agnostic LLM client wrapping litellm for the RTL agent."""
import copy
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

_verbose = os.environ.get("AI_MCP_VERBOSE", "").strip().lower() in ("1", "true", "yes")

if not _verbose:
    os.environ.setdefault("LITELLM_LOG", "ERROR")

import litellm

if not _verbose:
    litellm.suppress_debug_info = True
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
else:
    logging.getLogger("LiteLLM").setLevel(logging.INFO)


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0


@dataclass
class Response:
    text: Optional[str]
    tool_calls: list[ToolCall]
    stop_reason: str  # normalized: "end_turn" | "tool_use" | "max_tokens" | "stop_sequence"
    usage: Usage
    raw: Any = None


_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


class LLMClient:
    def __init__(self, model: str, api_key: Optional[str] = None):
        # model is "provider/model-name" e.g. "anthropic/claude-opus-4-7"
        self.model = model
        self.api_key = api_key

    def call(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 8000,
    ) -> Response:
        # --- Build system message ----------------------------------------
        if self.model.startswith("anthropic/"):
            system_msg = {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        else:
            system_msg = {"role": "system", "content": system}

        full_messages = [system_msg] + list(messages)

        # --- Convert Anthropic-style tool defs to OpenAI function style ----
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]

        # --- Prompt caching for Anthropic models ---------------------------
        if self.model.startswith("anthropic/"):
            # Work on copies so we don't mutate the caller's data structures
            openai_tools = list(openai_tools)
            if openai_tools:
                last_tool = dict(openai_tools[-1])
                last_tool["cache_control"] = {"type": "ephemeral"}
                openai_tools[-1] = last_tool

            full_messages = copy.deepcopy(full_messages)
            # Cache the last user message
            for i in range(len(full_messages) - 1, -1, -1):
                msg = full_messages[i]
                if msg.get("role") == "user":
                    content = msg["content"]
                    if isinstance(content, str):
                        msg["cache_control"] = {"type": "ephemeral"}
                    elif isinstance(content, list) and content:
                        content[-1]["cache_control"] = {"type": "ephemeral"}
                    break

        # --- Build litellm kwargs ------------------------------------------
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": full_messages,
            "tools": openai_tools,
            "max_tokens": max_tokens,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key

        # --- Call litellm (exceptions propagate) ---------------------------
        raw = litellm.completion(**kwargs)

        # --- Parse response ------------------------------------------------
        choice = raw.choices[0]
        message = choice.message

        text: Optional[str] = message.content  # may be None when tool calls present

        # Parse tool calls
        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        input=json.loads(tc.function.arguments),
                    )
                )

        # Normalize finish_reason
        finish_reason = choice.finish_reason or ""
        stop_reason = _FINISH_REASON_MAP.get(finish_reason, finish_reason)

        # Parse usage
        usage_obj = raw.usage
        cache_read = getattr(usage_obj, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage_obj, "cache_creation_input_tokens", 0) or 0
        usage = Usage(
            input_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
            cache_read=cache_read,
            cache_creation=cache_creation,
        )

        return Response(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            raw=raw,
        )
