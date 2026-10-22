"""Calls Claude for one agent turn and returns its JSON answer, keeping count of what it cost."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Protocol

import anthropic

# USD per million tokens (input, output). Cache reads cost a tenth of input, cache writes 1.25x.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
WEB_SEARCH_USD = 0.01  # per search
# Models that can hand a declined request to another model server-side, instead of just stopping.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}


class SpendCapReached(RuntimeError):
    pass


class CreditsExhausted(SpendCapReached):
    """The Claude API account itself is out of credits (or its key no longer works)."""


class AgentError(RuntimeError):
    pass


def _out_of_credits(exc: anthropic.APIStatusError) -> bool:
    return exc.status_code in (401, 402, 403) or "credit balance" in str(exc).lower()


@dataclass
class Usage:
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    web_searches: int
    cost_usd: float


class LLM(Protocol):
    def ask(
        self, *, agent: str, model: str, system: str, prompt: str, schema: dict,
        effort: str = "medium", web_searches: int = 0,
    ) -> dict: ...


def cost(model: str, usage) -> float:
    price_in, price_out = PRICES.get(model, PRICES["claude-opus-5-5"])
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
    return (
        usage.input_tokens * price_in + cache_read * price_in * 0.1 + cache_write * price_in * 1.25
        + usage.output_tokens * price_out
    ) / 1_000_000 + searches * WEB_SEARCH_USD


def api_key() -> str | None:
    """Cloud environments reserve ANTHROPIC_API_KEY for Claude Code itself, so the desk's key can live under its own name."""
    return os.environ.get("TRADEBOY_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")


class ClaudeLLM:
    """Thread-safe: analysts run in parallel. Usage piles up in `usage` until the desk journals it."""

    def __init__(self, client: anthropic.Anthropic | None = None, budget_left_usd: float = float("inf")):
        self.client = client or anthropic.Anthropic(api_key=api_key())
        self.budget_left_usd = budget_left_usd
        self.usage: list[Usage] = []
        self.lock = threading.Lock()

    def drain_usage(self) -> list[Usage]:
        with self.lock:
            usage, self.usage = self.usage, []
            return usage

    def ask(
        self, *, agent: str, model: str, system: str, prompt: str, schema: dict,
        effort: str = "medium", web_searches: int = 0,
    ) -> dict:
        with self.lock:
            if self.budget_left_usd <= 0:
                raise SpendCapReached("The monthly Claude API budget is used up.")
        tools = []
        if web_searches:
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": web_searches})
        fallback = (
            {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
        )
        messages = [{"role": "user", "content": prompt}]
        for _ in range(5):  # a server-side web search can pause a long turn; continue it
            try:
                response = self.client.beta.messages.create(
                    model=model,
                    max_tokens=16000,
                    system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                    messages=messages,
                    tools=tools or anthropic.omit,
                    output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                    **fallback,
                )
            except anthropic.APIStatusError as exc:
                if _out_of_credits(exc):
                    raise CreditsExhausted(f"Claude API refused the request ({exc.status_code}): {exc}") from exc
                raise AgentError(f"{agent}: Claude API error {exc.status_code}") from exc
            except anthropic.APIConnectionError as exc:
                raise AgentError(f"{agent}: could not reach the Claude API") from exc
            self._record(agent, model, response.usage)
            if response.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": response.content}]
        if response.stop_reason == "refusal":
            raise AgentError(f"{agent} declined to answer.")
        if response.stop_reason == "max_tokens":
            raise AgentError(f"{agent} ran out of room before finishing its answer.")
        texts = [b.text for b in response.content if b.type == "text"]
        if not texts:
            raise AgentError(f"{agent} returned no answer.")
        try:
            return json.loads(texts[-1])
        except json.JSONDecodeError as exc:
            raise AgentError(f"{agent} returned invalid JSON: {exc}") from exc

    def _record(self, agent: str, model: str, usage) -> None:
        spent = cost(model, usage)
        searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
        with self.lock:
            self.budget_left_usd -= spent
            self.usage.append(Usage(agent, model, usage.input_tokens, usage.output_tokens, searches, spent))
