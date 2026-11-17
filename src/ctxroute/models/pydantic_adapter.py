"""Optional PydanticAI transport for the same budgeted DeepSeekModel boundary."""

import asyncio
import json
from collections.abc import AsyncIterator
from importlib import import_module
from typing import Any

from ctxroute.models.deadline import wall_timeout
from ctxroute.models.diagnostics import WireMetadata, active_wire
from ctxroute.models.streaming import IncompleteStreamError, StreamProtocolError, StreamTracker


def pydantic_transport(payload: dict[str, Any], key: str, timeout: int) -> dict[str, Any]:
    """SDK is loaded only on explicit use; no installation or inference at import time."""

    async def request() -> dict[str, Any]:
        extra_body = {"thinking": payload["thinking"]}
        if "response_format" in payload:
            if payload["response_format"] != {"type": "json_object"}:
                raise ValueError("Unsupported response format")
            # Keep raw text for Engine validation and accounting. SDK structured-output
            # validation must not consume a correction outside the budgeted loop.
            extra_body["response_format"] = payload["response_format"]
        sdk = import_module("pydantic_ai")
        openai = import_module("openai")
        models = import_module("pydantic_ai.models.openai")
        providers = import_module("pydantic_ai.providers.deepseek")
        httpx = import_module("httpx")
        usage_types = import_module("pydantic_ai.usage")
        wire = active_wire.get() or WireMetadata()
        streaming = payload.get("stream", False)
        if type(streaming) is not bool:
            raise ValueError("Invalid stream option")
        wire.streaming = streaming
        tracker = StreamTracker(output_audit=wire.output_audit)
        if streaming:
            wire.stream_terminal = tracker.terminal_fields()

        async def observe_request(request: Any) -> None:
            # The SDK sends a buffered JSON body. Never materialize a stream for logging.
            try:
                wire.request_body_bytes = len(request.content)
            except httpx.RequestNotRead:
                pass

        async def observe_response(response: Any) -> None:
            wire.observe_response(response.status_code, response.headers)
            original_bytes = response.aiter_bytes
            body: list[bytes] = []

            def observe(chunk: bytes) -> None:
                wire.observe_chunk(len(chunk))
                if wire.response_bytes_received > 16 * 1024 * 1024:
                    raise ValueError("Response exceeded byte limit")
                if streaming:
                    tracker.feed(chunk)
                    wire.sse_events = tracker.events
                    wire.sse_keepalives = tracker.keepalives
                    wire.sse_done = tracker.done
                    wire.stream_terminal = tracker.terminal_fields()
                else:
                    body.append(chunk)

            def completed() -> None:
                wire.response_completed = True
                if not streaming:
                    try:
                        raw = json.loads(b"".join(body))
                    except (ValueError, UnicodeError):
                        return  # SDK validation still owns malformed-response handling.
                    wire.output_audit.capture_response(raw)

            if response.is_stream_consumed:
                # Mock/cached responses have already been decoded by HTTPX.
                observe(response.content)
                completed()
                return

            async def measured_bytes(chunk_size: int | None = None) -> AsyncIterator[bytes]:
                # Observe decoded bytes, so gzip and split UTF-8/SSE frames also work.
                # Text evidence is bounded and saved separately by the gateway.
                async for chunk in original_bytes(chunk_size):
                    observe(chunk)
                    yield chunk
                completed()

            response.aiter_bytes = measured_bytes

        client = openai.AsyncOpenAI(
            api_key=key,
            base_url="https://api.deepseek.com",
            max_retries=0,
            timeout=timeout,
            http_client=httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                event_hooks={"request": [observe_request], "response": [observe_response]},
            ),
        )
        try:
            messages = payload["messages"]
            if len(messages) > 2 and "response_format" not in payload:
                # Native mini-swe-agent owns its chat history. Pass it unchanged
                # through the installed SDK instead of forcing a two-message envelope.
                if any(
                    item.get("role") not in {"system", "user", "assistant"}
                    or not isinstance(item.get("content"), str)
                    for item in messages
                ):
                    raise ValueError("Invalid native text history")
                async with wall_timeout(timeout):
                    native_response = await client.chat.completions.create(
                        model=payload["model"],
                        messages=messages,
                        max_tokens=payload["max_tokens"],
                        temperature=payload["temperature"],
                        stream=streaming,
                        extra_body=extra_body,
                    )
                    if not streaming:
                        wire.sdk_output_accepted = True
                        return native_response.model_dump()
                    fragments: list[str] = []
                    async for chunk in native_response:
                        for choice in chunk.choices:
                            if choice.delta.content:
                                fragments.append(choice.delta.content)
                wire.sdk_output_accepted = True
                finish_reason, receipt_usage = tracker.validated_receipt()
                return {
                    "choices": [
                        {"finish_reason": finish_reason, "message": {"content": "".join(fragments)}}
                    ],
                    "usage": receipt_usage,
                }
            if len(messages) == 1 and messages[0]["role"] == "user":
                system_prompt, user_prompt = (), messages[0]["content"]
            elif (
                len(messages) == 2
                and messages[0]["role"] == "system"
                and messages[1]["role"] == "user"
            ):
                system_prompt, user_prompt = messages[0]["content"], messages[1]["content"]
            else:
                raise ValueError("Expected fixed probe or two-message context envelope")
            model = models.OpenAIChatModel(
                payload["model"],
                provider=providers.DeepSeekProvider(openai_client=client),
                # DeepSeek documents max_tokens for Chat Completions.
                profile={"openai_chat_supports_max_completion_tokens": False},
            )
            agent = sdk.Agent(model, system_prompt=system_prompt, output_type=str, retries=0)
            settings = {
                "max_tokens": payload["max_tokens"],
                "temperature": payload["temperature"],
                "timeout": timeout,
                "max_retries": 0,
                "extra_body": extra_body,
            }
            finish_reason: str | None = None
            receipt_usage: dict[str, int] | None = None
            # A server sending keep-alives must not extend the run deadline forever.
            async with wall_timeout(timeout):
                if streaming:
                    try:
                        async with agent.run_stream(
                            user_prompt,
                            usage_limits=usage_types.UsageLimits(request_limit=1),
                            model_settings=settings,
                        ) as result:
                            output = await result.get_output()
                            usage, response = result.usage, result.response
                            wire.sdk_output_accepted = True
                    except Exception as exc:
                        wire.sdk_output_accepted = False
                        if wire.http_status == 200 and wire.response_completed and not tracker.done:
                            # SDKs may report "no output" after a clean but incomplete EOF.
                            raise IncompleteStreamError(
                                "Stream ended without completion marker"
                            ) from exc
                        raise
                    finish_reason, receipt_usage = tracker.validated_receipt()
                    if finish_reason == "stop" and response.finish_reason != "stop":
                        raise StreamProtocolError("Invalid stream terminal metadata")
                else:
                    result = await agent.run(
                        user_prompt,
                        usage_limits=usage_types.UsageLimits(request_limit=1),
                        model_settings=settings,
                    )
                    output, usage, response = result.output, result.usage, result.response
                    wire.sdk_output_accepted = True
            if getattr(usage, "unmeasured_requests", 1) != 0 or usage.requests != 1:
                raise ValueError("Missing usage or hidden additional SDK requests")
            cached = usage.details.get("prompt_cache_hit_tokens")
            missed = usage.details.get("prompt_cache_miss_tokens")
            if type(cached) is not int or type(missed) is not int:
                raise ValueError("Missing original DeepSeek cache usage fields")
            sdk_usage = {
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
                "prompt_cache_hit_tokens": cached,
                "prompt_cache_miss_tokens": missed,
                "total_tokens": usage.input_tokens + usage.output_tokens,
            }
            if streaming and sdk_usage != receipt_usage:
                raise ValueError("SDK usage differs from stream receipt")
            return {
                "choices": [
                    {
                        "finish_reason": finish_reason if streaming else response.finish_reason,
                        "message": {"content": output},
                    }
                ],
                "usage": receipt_usage if streaming else sdk_usage,
            }
        finally:
            await client.close()

    return asyncio.run(request())
