"""Official Google Gen AI backend for text-only, confirmed-free-tier use.

This implements the existing LLMBackend protocol without changing OpenAILLM.
No billing APIs, Vertex AI, tools, fallback model or automatic retries are used.
"""
from collections import deque
import math
import os
from time import perf_counter, monotonic, sleep
from typing import Mapping

from .llm_client import Completion


class GeminiError(RuntimeError):
    """Sanitized error: no raw provider body, request URL or credential fragments."""


class GeminiLLM:
    simulated = False

    def __init__(self, model: str = 'gemini-2.5-flash', max_output_tokens: int = 512,
                 temperature: float | None = None, timeout: float = 60,
                 thinking_budget: int | None = 0, free_tier_confirmed: bool = False,
                 rate_limits: Mapping[str, int | None] | None = None,
                 thinking_level: str | None = None):
        key = (os.getenv('GEMINI_API_KEY') or '').strip()
        if not key:
            raise ValueError('Set GEMINI_API_KEY in the local .env file; never paste credentials into logs.')
        if (not model or type(max_output_tokens) is not int or max_output_tokens <= 0
                or not math.isfinite(timeout) or timeout <= 0
                or (thinking_budget is not None and (type(thinking_budget) is not int or thinking_budget < -1))):
            raise ValueError('Invalid Gemini model, output limit, timeout or thinking budget.')
        if temperature is not None and (not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise ValueError('Gemini temperature must be finite and between 0 and 2.')
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise GeminiError('Install the official google-genai package from requirements.txt.') from None
        name = model.removeprefix('models/')
        if name.startswith('gemini-3'):
            if thinking_budget not in (None, 0):
                raise ValueError('Gemini 3 uses thinking_level; do not supply a numeric thinking budget.')
            level = thinking_level or ('minimal' if name == 'gemini-3.5-flash-lite' else None)
            if level is not None:
                if not isinstance(level, str) or level.lower() not in {'minimal', 'low', 'medium', 'high'}:
                    raise ValueError('Invalid Gemini thinking level.')
                thinking = types.ThinkingConfig(thinking_level=level.upper())
            else:
                thinking = None  # Preserve the selected model's default; do not invent its supported levels.
        else:
            if thinking_level is not None:
                raise ValueError('Use the existing numeric thinking_budget setting for Gemini 2.5.')
            thinking = types.ThinkingConfig(thinking_budget=thinking_budget) if thinking_budget is not None else None
        self.model, self._key = model, key
        self.free_tier_confirmed = free_tier_confirmed is True
        self.rate_limits = dict(rate_limits or {})
        self._recent: deque[tuple[float, int]] = deque()
        self._issued = 0
        self.model_metadata: dict | None = None
        self.usage_records: list[dict] = []
        self.last_response: dict | None = None
        # Explicit endpoint and both cloud flags prevent ambient Google/Vertex settings
        # or GOOGLE_API_KEY from choosing a different credential or paid cloud backend.
        self.client = self._safe_request(lambda: genai.Client(api_key=key, enterprise=False, vertexai=False,
            http_options=types.HttpOptions(base_url='https://generativelanguage.googleapis.com',
                api_version='v1beta', timeout=math.ceil(timeout * 1000),
                retry_options=types.HttpRetryOptions(attempts=1))))
        self.config = types.GenerateContentConfig(
            max_output_tokens=max_output_tokens, temperature=temperature, candidate_count=1,
            response_modalities=['TEXT'], thinking_config=thinking,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))

    @staticmethod
    def _safe_request(call):
        try:
            return call()
        except Exception as exc:
            code = getattr(exc, 'code', None)
            code = code if type(code) is int else None
            if code in {401, 403}:
                message = 'Gemini authentication/permission denied; check the local key, region and model access.'
            elif code == 429:
                message = 'Gemini rate/quota limit reached (HTTP 429); no retry or billing change was made.'
            elif code == 404:
                message = 'Requested Gemini model is unavailable to this project; no substitute was selected.'
            else:
                message = 'Gemini request failed; no automatic retry or fallback was performed.'
            error = GeminiError(message)
            error.http_status = code
            raise error from None

    def verify_model(self) -> dict:
        """Read-only model metadata; does not verify billing tier or remaining quota."""
        if self.model_metadata is None:
            info = self._safe_request(lambda: self.client.models.get(model=self.model))
            actions = list(info.supported_actions or [])
            if 'generateContent' not in actions:
                raise GeminiError('Requested model does not advertise generateContent support.')
            if info.output_token_limit is not None and self.config.max_output_tokens > info.output_token_limit:
                raise GeminiError('Configured output cap exceeds the model metadata limit.')
            self.model_metadata = {'name': (info.name or self.model).replace(self._key, '[REDACTED]'),
                                   'supported_actions': actions, 'input_token_limit': info.input_token_limit,
                                   'output_token_limit': info.output_token_limit}
        return dict(self.model_metadata)

    def validate_free_access(self) -> None:
        """Require operator verification of Free tier/billing disabled and active quotas.

        The generation SDK does not attest billing status. This is an explicit local
        safety gate, not a claim that an API key itself proves free-tier eligibility.
        """
        if not self.free_tier_confirmed:
            raise GeminiError('Confirm this project is on the Free tier with billing disabled before generation.')
        if any(type(self.rate_limits.get(name)) is not int or self.rate_limits[name] <= 0
               for name in ('rpm', 'tpm', 'rpd')):
            raise GeminiError('Record current positive RPM, input TPM and RPD limits from AI Studio before generation.')

    def _pace(self, prompt: str) -> None:
        self.validate_free_access()
        bound = len(prompt.encode('utf-8')) + 32
        if bound > self.rate_limits['tpm']:
            raise GeminiError('Prompt exceeds the conservative input TPM allowance; not sent or truncated.')
        if self._issued >= self.rate_limits['rpd']:
            raise GeminiError('Local session RPD allowance reached; no further request sent.')
        while True:
            now = monotonic()
            while self._recent and now - self._recent[0][0] >= 60:
                self._recent.popleft()
            if (len(self._recent) < self.rate_limits['rpm']
                    and sum(size for _, size in self._recent) + bound <= self.rate_limits['tpm']):
                self._recent.append((now, bound))
                self._issued += 1
                return
            sleep(min(60., max(.001, 60. - (now - self._recent[0][0]))))

    @staticmethod
    def _usage(response) -> dict[str, int]:
        metadata = response.usage_metadata
        if metadata is None:
            raise GeminiError('Gemini returned no usage metadata; token estimates will not be substituted.')
        result = {}
        for name in ('prompt_token_count', 'candidates_token_count', 'thoughts_token_count',
                     'cached_content_token_count', 'tool_use_prompt_token_count', 'total_token_count'):
            value = getattr(metadata, name, None)
            if value is None and name in {'thoughts_token_count', 'cached_content_token_count', 'tool_use_prompt_token_count'}:
                value = 0  # Optional counters are omitted when absent; retain the reported total below.
            if type(value) is not int or value < 0:
                raise GeminiError('Gemini usage metadata is missing or invalid; no numerical usage was fabricated.')
            result[name] = value
        expected = sum(result[name] for name in ('prompt_token_count', 'candidates_token_count',
                                                 'thoughts_token_count', 'tool_use_prompt_token_count'))
        if (result['total_token_count'] != expected or result['tool_use_prompt_token_count'] != 0
                or result['cached_content_token_count'] > result['prompt_token_count']):
            raise GeminiError('Unexpected Gemini usage breakdown for this text-only, no-tools backend.')
        return result

    def generate(self, prompt: str) -> Completion:
        self.last_response = None
        if self._key in prompt:
            raise GeminiError('Credential detected in prompt; request refused.')
        self.validate_free_access()
        self.verify_model()
        self._pace(prompt)
        start = perf_counter()
        try:
            response = self._safe_request(lambda: self.client.models.generate_content(
                model=self.model, contents=prompt, config=self.config))
        except GeminiError:
            self.last_response = {'text': None, 'latency': perf_counter() - start, 'usage': None}
            raise
        latency = perf_counter() - start
        candidates = response.candidates or []
        finish = getattr(candidates[0].finish_reason, 'value', candidates[0].finish_reason) if candidates else None
        parts = (getattr(candidates[0].content, 'parts', None) or []) if candidates else []
        text = ''.join(p.text for p in parts if getattr(p, 'text', None) and not getattr(p, 'thought', False))
        text = text.replace(self._key, '[REDACTED]')
        self.last_response = {'text': text, 'latency': latency, 'finish_reason': finish,
                              'model_version': (response.model_version or self.model).replace(self._key, '[REDACTED]'),
                              'usage': None}
        usage = self._usage(response)
        self.last_response['usage'] = dict(usage)
        self.usage_records.append({**usage, 'latency': latency, 'finish_reason': finish,
            'model_version': (response.model_version or self.model).replace(self._key, '[REDACTED]')})
        if len(candidates) != 1 or finish != 'STOP':
            raise GeminiError('Gemini response was blocked, truncated or incomplete; recorded usage is retained. '
                              f'finish_reason={finish}; candidates={len(candidates)}.')
        if not text.strip():
            raise GeminiError('Gemini returned no visible solution text; usage is retained.')
        # Include generated thinking in output totals; cached input is already a subset
        # of prompt tokens and must not be added a second time.
        return Completion(text.replace(self._key, '[REDACTED]'), usage['prompt_token_count'],
                          usage['candidates_token_count'] + usage['thoughts_token_count'], latency)

    def close(self) -> None:
        self.client.close()

    @property
    def generation_calls_issued(self) -> int:
        return self._issued
