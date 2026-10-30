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
