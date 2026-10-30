"""Cost-reserving real API backend with durable, secret-free attempt accounting."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter
import uuid

from .llm_client import Completion

INPUT_PER_MILLION = 0.25
CACHED_INPUT_PER_MILLION = 0.025
OUTPUT_PER_MILLION = 2.0


def usd(input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    return ((input_tokens - cached_tokens) * INPUT_PER_MILLION
            + cached_tokens * CACHED_INPUT_PER_MILLION
            + output_tokens * OUTPUT_PER_MILLION) / 1_000_000


def append_jsonl(path: Path, value: dict) -> None:
    """Flush each event so a failed run retains evidence of issued requests."""
    import os
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def input_reservation(prompt: str) -> int:
    """Conservative byte-based token bound plus an envelope safety allowance.

    Not a provider token-count endpoint: actual usage is checked on every response.
    The fixed pilot uses text input only, no tools, attachments or hidden history.
    """
    return len(prompt.encode('utf-8')) + 256


class PilotStopped(RuntimeError):
    """Safe public error message; raw SDK exceptions are never logged."""


@dataclass
class Budget:
    limit_usd: float = 2.0
    max_calls: int = 240
    max_output_tokens: int = 2048
    max_input_reservation: int = 12000
    committed_usd: float = 0.
    issued_calls: int = 0

    def reserve(self, prompt: str) -> tuple[int, float]:
        bound = input_reservation(prompt)
        reserve = usd(bound, self.max_output_tokens)
        if bound > self.max_input_reservation:
            raise PilotStopped('Input safety cap reached; no prompt truncation or request performed.')
        if self.issued_calls >= self.max_calls or self.committed_usd + reserve > self.limit_usd:
            raise PilotStopped('Pilot call/spending budget reached; no further request performed.')
        self.committed_usd += reserve
        self.issued_calls += 1
        return bound, reserve


class MeteredOpenAILLM:
    """One attempt per call, no silent retries, no mock fallback and no references."""
    model = 'gpt-5-mini'
    simulated = False

    def __init__(self, client, budget: Budget, journal: Path, key: str):
        self.client, self.budget, self.journal = client, budget, journal
        self._key = key
        self.context: dict = {}
        self.call_in_question = 0
        self.events: list[dict] = []

    def start_question(self, item_id: str, topology: str) -> None:
        self.context = {'item_id': item_id, 'topology': topology}
        self.call_in_question = 0

    def generate(self, prompt: str) -> Completion:
        if self._key and self._key in prompt:
            raise PilotStopped('Credential detected in prompt; request and logging refused.')
        bound, reserved = self.budget.reserve(prompt)
        event = {'call_id': str(uuid.uuid4()), 'timestamp': datetime.now(timezone.utc).isoformat(),
                 **self.context, 'round': self.call_in_question // 4 + 1,
                 'agent_id': self.call_in_question % 4, 'model_requested': self.model,
                 'input_token_reservation': bound, 'reserved_usd': reserved,
                 'event': 'request_started', 'prompt': prompt}
        self.call_in_question += 1
        # Persist before request. A dangling request_started means outcome/cost unknown.
        append_jsonl(self.journal, event)
        started = perf_counter()
        try:
            response = self.client.responses.create(
                model=self.model, input=prompt, reasoning={'effort': 'low'},
                max_output_tokens=self.budget.max_output_tokens, store=False,
                service_tier='default')
        except Exception as exc:
            failure = {**event, 'event': 'request_failed', 'latency': perf_counter() - started,
                       'error_type': type(exc).__name__,
                       'http_status': getattr(exc, 'status_code', None),
                       'usage_unknown': True, 'prompt': None}
            self.events.append(failure)
            append_jsonl(self.journal, failure)
            # Keep the full reservation and halt; never echo exception text or retry.
            raise PilotStopped('API request failed; reserved cost retained and pilot halted.') from None
        latency = perf_counter() - started
        usage = response.usage
        text = (response.output_text or '').replace(self._key, '[REDACTED]') if self._key else (response.output_text or '')
        result = {**event, 'event': 'response_received', 'prompt': None,
                  'latency': latency, 'response_id': response.id, 'model_returned': response.model,
                  'status': response.status, 'output_text': text, 'usage_unknown': usage is None,
                  'service_tier': getattr(response, 'service_tier', None),
                  'incomplete_reason': getattr(getattr(response, 'incomplete_details', None), 'reason', None)}
        if usage is not None:
            cached = getattr(getattr(usage, 'input_tokens_details', None), 'cached_tokens', 0) or 0
            reasoning = getattr(getattr(usage, 'output_tokens_details', None), 'reasoning_tokens', 0) or 0
            result.update(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                          total_tokens=usage.total_tokens, cached_input_tokens=cached,
                          reasoning_tokens=reasoning,
                          estimated_charge_usd=usd(usage.input_tokens, usage.output_tokens, cached))
            self.budget.committed_usd += usd(usage.input_tokens, usage.output_tokens) - reserved
        self.events.append(result)
        append_jsonl(self.journal, result)
        if usage is None:
            raise PilotStopped('Provider usage missing; full cost reservation retained and pilot halted.')
        if usage.input_tokens > bound or usage.output_tokens > self.budget.max_output_tokens:
            raise PilotStopped('Provider usage exceeded the safety reservation; pilot halted.')
        if response.status != 'completed' or not text.strip():
            raise PilotStopped('Incomplete/empty API response recorded; pilot halted for review.')
        return Completion(text, usage.input_tokens, usage.output_tokens, latency)
