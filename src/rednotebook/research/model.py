"""Pydantic AI local backend, explicit egress boundary and conservative request budget."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from rednotebook.errors import DomainError


@dataclass
class Ledger:
    calls: int = 0
    input_reserved: int = 0
    output_reserved: int = 0
    reported_input: int = 0
    reported_output: int = 0

    def payload(self):
        return {
            **asdict(self),
            "reservation_method": "UTF-8 serialized message bytes + 1024 overhead; output cap per attempt",
            "reported_usage_is_provider_supplied": True,
        }


class BudgetedModel(WrapperModel):
    def __init__(self, wrapped, config, budget, ledger, permission_check):
        super().__init__(wrapped)
        self.config = config
        self.budget = budget
        self.ledger = ledger
        self.permission_check = permission_check

    async def request(self, messages, model_settings, model_request_parameters):
        self.permission_check()
        # Reserve conservatively before every request, including invalid output retries.
        # Never call a remote tokenizer or a pricing service.
        serialized = json.dumps(
            [repr(messages), repr(model_request_parameters)], ensure_ascii=False
        )
        input_size = len(serialized.encode()) + 1024
        cap = min(
            self.config.max_tokens_per_call,
            self.budget.max_output_tokens - self.ledger.output_reserved,
        )
        if (
            self.ledger.calls >= self.budget.max_model_calls
            or cap < 64
            or self.ledger.input_reserved + input_size > self.budget.max_input_tokens
        ):
            raise DomainError("research_budget_exhausted")
        self.ledger.calls += 1
        self.ledger.input_reserved += input_size
        self.ledger.output_reserved += cap
        settings = {**(model_settings or {}), "max_tokens": cap}
        response = await asyncio.wait_for(
            super().request(messages, settings, model_request_parameters),
            timeout=self.config.timeout_seconds,
        )
        self.ledger.reported_input += response.usage.input_tokens
        self.ledger.reported_output += response.usage.output_tokens
        if (
            self.ledger.reported_input > self.budget.max_input_tokens
            or self.ledger.reported_output > self.budget.max_output_tokens
        ):
            raise DomainError("provider_reported_usage_exceeded_budget")
        return response


def request_guard(base_url):
    expected = urlsplit(base_url)
    expected_port = expected.port or (443 if expected.scheme == "https" else 80)

    async def guard(request):
        if (
            request.url.scheme != expected.scheme
            or request.url.host != expected.hostname
            or request.url.port != expected_port
            or request.url.path not in {"/v1/models", "/v1/chat/completions"}
            or request.url.query
        ):
            raise DomainError("model_egress_not_allowed")

    return guard


@asynccontextmanager
async def local_backend(config):
    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(config.timeout_seconds, connect=5),
        event_hooks={"request": [request_guard(config.base_url)]},
    ) as http:
        client = AsyncOpenAI(
            base_url=config.base_url.rstrip("/"),
            api_key=config.api_key.get_secret_value(),
            http_client=http,
            max_retries=0,
        )
        try:
            yield OpenAIChatModel(
                config.model,
                provider=OpenAIProvider(openai_client=client),
                profile=OpenAIModelProfile(openai_supports_strict_tool_definition=False),
            )
        finally:
            await client.close()
