"""Opt-in Claude provider adapter (WP6).  The only module that imports a network SDK;
it is imported lazily by ``llm.build_client`` when ``llm.provider: anthropic`` is configured."""

from __future__ import annotations

from .llm import LLMClientError, LLMPreflightError, LLMResponse


class AnthropicClient:
    """Claude through the official ``anthropic`` SDK.

    Credentials resolve the SDK's usual way (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an
    ``ant auth login`` profile) and are never logged.  Extraction is a bounded task, so
    effort defaults to ``low``.  A refusal is a failed call (counted, never "no signal").
    Server-side refusal fallback is off by default; enable it with llm.fallbacks: true."""
    provider = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low", fallbacks: bool = False,
                 timeout_s: float = 120.0):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise LLMPreflightError("provider 'anthropic' needs the anthropic package (pip install anthropic)") from e
        self._sdk = anthropic
        self._client = anthropic.Anthropic(timeout=timeout_s)
        self.model, self.effort, self.fallbacks = model, effort, fallbacks

    def complete(self, system: str, user: str, max_tokens: int) -> LLMResponse:  # pragma: no cover - network
        kw = dict(model=self.model, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}], output_config={"effort": self.effort})
        try:
            if self.fallbacks:
                resp = self._client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                         fallbacks="default", **kw)
            else:
                resp = self._client.messages.create(**kw)
        except self._sdk.APIStatusError as e:
            raise LLMClientError(f"provider error {e.status_code}") from e
        except self._sdk.APIConnectionError as e:
            raise LLMClientError("provider connection error") from e
        if resp.stop_reason == "refusal":
            raise LLMClientError("provider refused the request")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return LLMResponse(text, resp.usage.input_tokens, resp.usage.output_tokens, resp.model, resp.stop_reason)
