from typing import Literal

import httpx
from pydantic_ai import Agent, ModelRetry, ToolOutput

from rednotebook.domain.models import Contract
from rednotebook.research.config import ResearchBudget
from rednotebook.research.model import BudgetedModel, Ledger, local_backend, request_guard
from rednotebook.research.runner import failure_code


class ProbeOutput(Contract):
    status: Literal["ok"]
    text: Literal["中文结构化输出验证"]
    value: Literal[7]


async def probe(config):
    summary = {
        "model": config.model,
        "connectivity": False,
        "structured_output": False,
        "tool_calling": False,
        "visual_analysis": "not_tested",
    }
    try:
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(15, connect=5),
            event_hooks={"request": [request_guard(config.base_url)]},
        ) as http:
            response = await http.get(
                config.base_url.rstrip("/") + "/models",
                headers={"Authorization": "Bearer " + config.api_key.get_secret_value()},
            )
            response.raise_for_status()
            summary["connectivity"] = True
            summary["model_listed"] = config.model in [
                m.get("id") for m in response.json().get("data", [])
            ]
        async with local_backend(config) as model:
            ledger = Ledger()
            budget = ResearchBudget(
                max_model_calls=5, max_input_tokens=50000, max_output_tokens=5000
            )
            guarded = BudgetedModel(model, config, budget, ledger, lambda: None)
            agent = Agent(
                guarded,
                output_type=ToolOutput(ProbeOutput, name="submit_probe", strict=False),
                retries=2,
                instructions="先调用 read_probe，然后用返回的数据填充结构化输出。不要猜测数据。",
            )
            called = []

            @agent.tool_plain
            def read_probe(probe_id: Literal["synthetic"]) -> dict:
                """Read the synthetic probe data."""
                called.append(True)
                return {"status": "ok", "text": "中文结构化输出验证", "value": 7}

            @agent.output_validator
            def validate(output: ProbeOutput) -> ProbeOutput:
                if not called:
                    raise ModelRetry("read_probe must be called first")
                return output

            await agent.run(
                "开始探针。",
                model_settings={
                    "temperature": 0,
                    "extra_body": {
                        "chat_template_kwargs": {"enable_thinking": config.enable_thinking}
                    },
                },
            )
            summary.update(
                structured_output=True, tool_calling=bool(called), usage=ledger.payload()
            )
    except Exception as exc:
        summary["error"] = failure_code(exc)
    return summary
