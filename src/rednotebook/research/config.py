import ipaddress
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator

from rednotebook.domain.models import Contract, Text
from rednotebook.errors import DomainError


class LocalModelConfig(Contract):
    base_url: Text
    model: Text
    api_key: SecretStr
    timeout_seconds: float = Field(default=180, gt=0, le=900)
    max_tokens_per_call: int = Field(default=8192, ge=64, le=65536)
    enable_thinking: bool = False

    @model_validator(mode="after")
    def local_only(self):
        url = urlsplit(self.base_url)
        try:
            address = ipaddress.ip_address(url.hostname or "")
        except ValueError:
            raise ValueError("local model requires a literal local IP address") from None
        networks = (
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "127.0.0.0/8",
            "::1/128",
            "fc00::/7",
        )
        if not any(address in ipaddress.ip_network(n) for n in networks):
            raise ValueError("local model address must be loopback or private LAN")
        if (
            url.scheme not in {"http", "https"}
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path.rstrip("/") != "/v1"
        ):
            raise ValueError("expected local HTTP(S) base URL ending /v1 without credentials")
        if not self.api_key.get_secret_value():
            raise ValueError("api_key must be present")
        return self

    def public_metadata(self):
        return {
            "model": self.model,
            "provider": "configured-local-endpoint",
            "timeout_seconds": self.timeout_seconds,
            "max_tokens_per_call": self.max_tokens_per_call,
            "enable_thinking": self.enable_thinking,
        }


def load_config(path: Path):
    if not path.is_file():
        raise DomainError("local_model_config_missing")
    return LocalModelConfig.model_validate(tomllib.loads(path.read_text()))


class ResearchBudget(Contract):
    max_notes: int = Field(default=200, ge=1, le=200)
    max_comments: int = Field(default=500, ge=0, le=500)
    max_model_calls: int = Field(default=60, ge=1, le=200)
    max_input_tokens: int = Field(default=4000000, ge=1, le=50000000)
    max_output_tokens: int = Field(default=524288, ge=1, le=4000000)
    max_retries: int = Field(default=2, ge=0, le=2)
    batch_notes: int = Field(default=10, ge=1, le=10)
    max_chars_per_field: int = Field(default=50000, ge=100, le=50000)


def research_budget(mode="deep", overrides=None):
    """Modes change sample depth, not evidence fidelity; caller overrides stay bounded."""
    if mode not in {"quick", "deep"}:
        raise DomainError("research_mode_invalid")
    values = ResearchBudget().model_dump()
    if mode == "quick":
        values.update(max_notes=20, max_comments=100)
    values.update(overrides or {})
    return ResearchBudget.model_validate(values)
