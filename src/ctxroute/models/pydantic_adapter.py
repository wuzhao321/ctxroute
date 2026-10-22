"""Optional PydanticAI transport for the same budgeted DeepSeekModel boundary."""

import asyncio
from importlib import import_module
from typing import Any


def pydantic_transport(payload: dict[str, Any], key: str, timeout: int) -> dict[str, Any]:
    """SDK is loaded only on explicit use; no installation or inference at import time."""

    async def request() -> dict[str, Any]:
        sdk = import_module("pydantic_ai")
        openai = import_module("openai")
        models = import_module("pydantic_ai.models.openai")
        providers = import_module("pydantic_ai.providers.openai")
        client = openai.AsyncOpenAI(
            api_key=key,
            base_url="https://api.deepseek.com",
            max_retries=0,
            timeout=timeout,
        )
        try:
            messages = payload["messages"]
            if (
                len(messages) != 2
                or messages[0]["role"] != "system"
                or messages[1]["role"] != "user"
            ):
                raise ValueError("Pydantic adapter expects the two-message context envelope")
            model = models.OpenAIChatModel(
                payload["model"], provider=providers.OpenAIProvider(openai_client=client)
            )
            agent = sdk.Agent(
                model, system_prompt=messages[0]["content"], output_type=str, retries=0
            )
            result = await agent.run(
                messages[1]["content"],
                model_settings={
                    "max_tokens": payload["max_tokens"],
                    "temperature": payload["temperature"],
                    "timeout": timeout,
                    "max_retries": 0,
                    "extra_body": {"thinking": payload["thinking"]},
                },
            )
            usage = result.usage()
            if getattr(usage, "unmeasured_requests", 1) != 0 or usage.requests != 1:
                raise ValueError("Missing usage or hidden additional SDK requests")
            return {
                "choices": [{"finish_reason": "stop", "message": {"content": result.output}}],
                "usage": {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "prompt_cache_hit_tokens": usage.cache_read_tokens,
                    "prompt_cache_miss_tokens": usage.input_tokens - usage.cache_read_tokens,
                    "total_tokens": usage.input_tokens + usage.output_tokens,
                },
            }
        finally:
            await client.close()

    return asyncio.run(request())
