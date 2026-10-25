"""Optional PydanticAI transport for the same budgeted DeepSeekModel boundary."""

import asyncio
from importlib import import_module
from typing import Any


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
        client = openai.AsyncOpenAI(
            api_key=key,
            base_url="https://api.deepseek.com",
            max_retries=0,
            timeout=timeout,
            http_client=httpx.AsyncClient(trust_env=False, follow_redirects=False),
        )
        try:
            messages = payload["messages"]
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
                payload["model"], provider=providers.DeepSeekProvider(openai_client=client)
            )
            agent = sdk.Agent(model, system_prompt=system_prompt, output_type=str, retries=0)
            result = await agent.run(
                user_prompt,
                usage_limits=usage_types.UsageLimits(request_limit=1),
                model_settings={
                    "max_tokens": payload["max_tokens"],
                    "temperature": payload["temperature"],
                    "timeout": timeout,
                    "max_retries": 0,
                    "extra_body": extra_body,
                },
            )
            usage = result.usage
            if getattr(usage, "unmeasured_requests", 1) != 0 or usage.requests != 1:
                raise ValueError("Missing usage or hidden additional SDK requests")
            cached = usage.details.get("prompt_cache_hit_tokens")
            missed = usage.details.get("prompt_cache_miss_tokens")
            if type(cached) is not int or type(missed) is not int:
                raise ValueError("Missing original DeepSeek cache usage fields")
            return {
                "choices": [
                    {
                        "finish_reason": result.response.finish_reason,
                        "message": {"content": result.output},
                    }
                ],
                "usage": {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "prompt_cache_hit_tokens": cached,
                    "prompt_cache_miss_tokens": missed,
                    "total_tokens": usage.input_tokens + usage.output_tokens,
                },
            }
        finally:
            await client.close()

    return asyncio.run(request())
