"""Backend boundary: only prompts and generation settings reach inference."""
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

DEMO_QUESTION = ('Janet has 3 apples. She buys 5 more apples and gives 2 to her friend. '
                 'How many apples does she have left?')


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    latency: float


class LLMBackend(Protocol):
    model: str
    simulated: bool

    def generate(self, prompt: str) -> Completion: ...


class MockLLM:
    """Routing fixture, deliberately limited to the supplied apple example."""
    model = 'offline-mock-apples-v1'
    simulated = True

    def generate(self, prompt: str) -> Completion:
        start = perf_counter()
        if DEMO_QUESTION not in prompt:
            raise ValueError('Mock backend supports only the apple demo; use a real backend for GSM8K.')
        revised = 'Peer messages (previous round):' in prompt
        text = ('I checked the supplied peer responses. ' if revised else '')
        text += 'Janet has 3 + 5 - 2 = 6 apples.\n#### 6'
        return Completion(text, len(prompt.split()), len(text.split()), perf_counter() - start)


class OpenAILLM:
    simulated = False

    def __init__(self, model: str, max_output_tokens: int = 512,
                 temperature: float | None = None, timeout: float = 60,
                 max_retries: int = 2):
        import os
        from openai import OpenAI
        if not model or not os.getenv('OPENAI_API_KEY'):
            raise ValueError('Set OPENAI_API_KEY and OPENAI_MODEL (or config model).')
        if max_output_tokens <= 0 or timeout <= 0 or max_retries < 0:
            raise ValueError('Invalid API request limits.')
        self.model = model
        self.client = OpenAI(timeout=timeout, max_retries=max_retries)
        self.options = {'max_output_tokens': max_output_tokens}
        if temperature is not None:
            self.options['temperature'] = temperature

    def generate(self, prompt: str) -> Completion:
        from openai import AuthenticationError
        start = perf_counter()
        try:
            result = self.client.responses.create(model=self.model, input=prompt,
                                                  store=False, **self.options)
        except AuthenticationError as exc:
            raise RuntimeError('OpenAI authentication failed; check your API credential.') from exc
        if result.status != 'completed' or not result.output_text or result.usage is None:
            raise RuntimeError('Incomplete/empty model response; inspect output limit or model settings.')
        return Completion(result.output_text, result.usage.input_tokens,
                          result.usage.output_tokens, perf_counter() - start)
