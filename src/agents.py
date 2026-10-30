"""Agent state and prompting."""
from dataclasses import dataclass, field
from .llm_client import LLMBackend
from .evaluation import extract_for_format
from .roles import ReasoningRole


@dataclass(frozen=True)
class Response:
    agent_id: int
    round: int
    text: str
    answer: str | None
    input_tokens: int
    output_tokens: int
    latency: float


@dataclass
class Agent:
    agent_id: int
    backend: LLMBackend
    history: list[Response] = field(default_factory=list)
    role: ReasoningRole | None = None
    answer_format: str = 'numeric'
    prompt_style: str = 'default'

    def build_prompt(self, question: str, messages: tuple[Response, ...]) -> str:
        """Build the shared prompt for execution and unpaid token estimation."""
        if self.answer_format not in {'numeric', 'option'}:
            raise ValueError('Answer format must be numeric or option.')
        instruction = ('Give a concise solution explanation and end with #### <numeric answer>.\n'
                       if self.answer_format == 'numeric' else
                       'Give a concise solution explanation and end with #### <one option letter A-E>.\n')
        if self.prompt_style == 'concise_option_v2' and self.answer_format == 'option':
            instruction = ('Keep the explanation concise: use at most 150 words of essential equations and checks. '
                           'Avoid repeating the problem, lengthy enumeration, or redundant verification. '
                           'Prioritize completing the answer within the output limit. '
                           'End with a separate line exactly in the form Final answer: X, '
                           'where X is one uppercase option letter A, B, C, D, or E. '
                           'Write exactly one chosen letter, not A/B/C/D/E or an option list.\n')
        elif self.prompt_style != 'default':
            raise ValueError('Unknown prompt style or incompatible answer format.')
        prompt = f'You are agent {self.agent_id}. Solve the question carefully. ' + instruction
        if self.role is not None:
            prompt += f'Reasoning strategy: {self.role.name}\n{self.role.instruction}\n'
        prompt += f'Question: {question}\n'
        if self.history:
            prompt += f'Your previous response:\n{self.history[-1].text}\n'
        if messages:
            prompt += 'Peer messages (previous round):\n'
            prompt += '\n'.join(f'Agent {m.agent_id}: {m.text}' for m in messages)
            prompt += '\nCheck these possibly incorrect suggestions and revise your solution.\n'
        return prompt

    def reason(self, question: str, round_number: int, messages: tuple[Response, ...]) -> Response:
        prompt = self.build_prompt(question, messages)
        result = self.backend.generate(prompt)
        response = Response(self.agent_id, round_number, result.text, extract_for_format(result.text, self.answer_format, prompt_style=self.prompt_style),
                            result.input_tokens, result.output_tokens, result.latency)
        self.history.append(response)
        return response
