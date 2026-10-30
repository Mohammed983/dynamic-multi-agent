"""Official SDK response fixtures; never send HTTP requests or enable billing."""
from copy import deepcopy
import json
from types import SimpleNamespace

from google import genai
from google.genai import types
import pytest
import yaml

from experiments import run_baseline
from src.gemini_client import GeminiError, GeminiLLM
from src.runner import run_question


LIMITS = {'rpm': 1000, 'tpm': 1000000, 'rpd': 1000}  # Unit-test values, NOT published free quotas.


def response(thoughts=7, finish=types.FinishReason.STOP):
    return types.GenerateContentResponse(model_version='gemini-unit-test-version',
        candidates=[types.Candidate(finish_reason=finish,
            content=types.Content(parts=[types.Part(text='Hidden summary', thought=True),
                                         types.Part(text='Solution explanation. #### 6')]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=10,
            candidates_token_count=5, thoughts_token_count=thoughts,
            cached_content_token_count=4, total_token_count=15 + thoughts))


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'unit-test-gemini-key')
    monkeypatch.setenv('GOOGLE_API_KEY', 'unrelated-key-must-not-be-used')
    state = SimpleNamespace(result=response(), error=None, metadata_error=None,
                            create_options=[], calls=[], metadata_calls=[], closed=False)
    def generate_content(**kwargs):
        state.calls.append(kwargs)
        if state.error:
            raise state.error
        return deepcopy(state.result)
    def get(**kwargs):
        state.metadata_calls.append(kwargs)
        if state.metadata_error:
            raise state.metadata_error
        return types.Model(name='models/' + kwargs['model'].removeprefix('models/'), supported_actions=['generateContent'],
                           input_token_limit=1048576, output_token_limit=65536)
    def close():
        state.closed = True
    def client(**kwargs):
        state.create_options.append(kwargs)
        return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content, get=get), close=close)
    monkeypatch.setattr(genai, 'Client', client)
    return state


def backend(**kwargs):
    return GeminiLLM(free_tier_confirmed=True, rate_limits=LIMITS, **kwargs)


def test_gemini_default_and_explicit_developer_endpoint(sdk):
    llm = backend()
    assert llm.model == 'gemini-2.5-flash'
    options = sdk.create_options[0]
    assert options['api_key'] == 'unit-test-gemini-key'
    assert options['vertexai'] is False and options['enterprise'] is False
    assert options['http_options'].base_url == 'https://generativelanguage.googleapis.com'
    assert options['http_options'].retry_options.attempts == 1
    assert options['http_options'].timeout == 60000
    assert llm.config.thinking_config.thinking_budget == 0
    assert llm.config.automatic_function_calling.disable
    llm.close()
    assert sdk.closed


def test_gemini_3_5_uses_minimal_level_and_no_numeric_budget(sdk):
    llm = backend(model='gemini-3.5-flash-lite', thinking_budget=None, thinking_level='minimal')
    thinking = llm.config.thinking_config
    assert thinking.thinking_level == types.ThinkingLevel.MINIMAL
    assert thinking.thinking_budget is None
    completion = llm.generate('A math question')
    assert sdk.calls[0]['model'] == 'gemini-3.5-flash-lite'
    assert sdk.metadata_calls[0]['model'] == 'gemini-3.5-flash-lite'
    assert completion.output_tokens == 12


def test_gemini_3_5_model_override_works_with_old_zero_budget_default(sdk):
    llm = backend(model='gemini-3.5-flash-lite')
    assert llm.config.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
    assert llm.config.thinking_config.thinking_budget is None


@pytest.mark.parametrize('kwargs', [
    {'model': 'gemini-3.5-flash-lite', 'thinking_budget': 512},
    {'model': 'gemini-3.5-flash-lite', 'thinking_level': 'invalid'},
    {'model': 'gemini-2.5-flash', 'thinking_level': 'minimal'},
])
def test_model_specific_thinking_controls_fail_before_client_creation(sdk, kwargs):
    with pytest.raises(ValueError):
        backend(**kwargs)
    assert not sdk.create_options


def test_provider_counts_include_thinking_without_double_counting_cache(sdk, monkeypatch):
    import src.gemini_client as module
    times = iter([12., 12.25])
    monkeypatch.setattr(module, 'perf_counter', lambda: next(times))
    llm = backend()
    completion = llm.generate('Question without reference')
    assert completion.input_tokens == 10
    assert completion.output_tokens == 12  # Five candidate + seven thinking tokens.
    assert completion.input_tokens + completion.output_tokens == 22
    assert completion.latency == .25
    assert 'Hidden summary' not in completion.text
    assert llm.usage_records[0]['total_token_count'] == 22
    assert llm.usage_records[0]['cached_content_token_count'] == 4
    assert sdk.metadata_calls == [{'model': 'gemini-2.5-flash'}]
    assert sdk.calls[0]['contents'] == 'Question without reference'


def test_no_thinking_usage_can_be_omitted(sdk):
    sdk.result = response(thoughts=0)
    sdk.result.usage_metadata.thoughts_token_count = None
    assert backend().generate('question').output_tokens == 5


@pytest.mark.parametrize('finish', [types.FinishReason.MAX_TOKENS, types.FinishReason.SAFETY])
def test_incomplete_or_blocked_candidate_is_not_an_agent_answer(sdk, finish):
    sdk.result = response(finish=finish)
    llm = backend()
    with pytest.raises(GeminiError, match='blocked, truncated or incomplete') as caught:
        llm.generate('question')
    assert f'finish_reason={finish.value}' in str(caught.value)
    assert llm.usage_records[0]['total_token_count'] == 22


def test_v2_truncation_rejected_even_with_an_apparent_final_line(sdk):
    sdk.result = response(finish=types.FinishReason.MAX_TOKENS)
    sdk.result.candidates[0].content.parts = [types.Part(text='Short fixture.\nFinal answer: A')]
    llm = backend(model='gemini-3.5-flash-lite',max_output_tokens=2048)
    with pytest.raises(GeminiError,match='MAX_TOKENS'):
        llm.generate('Independent fixture question')
    assert llm.last_response['usage']['total_token_count'] == 22
    assert llm.last_response['text'].endswith('Final answer: A')


@pytest.mark.parametrize('problem', ['missing', 'inconsistent', 'tool_tokens'])
def test_missing_or_unsupported_usage_is_never_estimated(sdk, problem):
    if problem == 'missing':
        sdk.result.usage_metadata = None
    elif problem == 'inconsistent':
        sdk.result.usage_metadata.total_token_count = 999
    else:
        sdk.result.usage_metadata.tool_use_prompt_token_count = 2
        sdk.result.usage_metadata.total_token_count = 24
    with pytest.raises(GeminiError):
        backend().generate('question')


def test_empty_visible_text_is_not_accepted(sdk):
    sdk.result.candidates[0].content.parts = [types.Part(text='Only a thought', thought=True)]
    llm = backend()
    with pytest.raises(GeminiError, match='no visible solution'):
        llm.generate('question')
    assert llm.usage_records


def test_key_missing_and_google_key_not_used(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.setenv('GOOGLE_API_KEY', 'other-key')
    with pytest.raises(ValueError, match='GEMINI_API_KEY'):
        GeminiLLM()


@pytest.mark.parametrize('free,limits', [(False, LIMITS), (True, {}),
                                      (True, {'rpm': 1, 'tpm': 1, 'rpd': None})])
def test_unconfirmed_free_tier_or_limits_prevents_all_requests(sdk, free, limits):
    llm = GeminiLLM(free_tier_confirmed=free, rate_limits=limits)
    with pytest.raises(GeminiError):
        llm.generate('question')
    assert not sdk.calls and not sdk.metadata_calls


@pytest.mark.parametrize('code', [401, 403, 404, 429, 503])
def test_provider_error_is_sanitized_and_not_retried(sdk, code):
    class ProviderError(Exception):
        pass
    error = ProviderError('https://example.invalid?key=unit-test-gemini-key')
    error.code = code
    sdk.error = error
    with pytest.raises(GeminiError) as caught:
        backend().generate('question')
    assert 'unit-test-gemini-key' not in str(caught.value)
    assert 'example.invalid' not in str(caught.value)
    assert len(sdk.calls) == 1


def test_metadata_failure_does_not_fallback_or_generate(sdk):
    sdk.metadata_error = RuntimeError('sensitive unit-test-gemini-key')
    with pytest.raises(GeminiError):
        backend().generate('question')
    assert not sdk.calls


def test_session_daily_limit_and_conservative_input_limit(sdk):
    llm = GeminiLLM(free_tier_confirmed=True, rate_limits={**LIMITS, 'rpd': 1})
    llm.generate('first question')
    with pytest.raises(GeminiError, match='RPD'):
        llm.generate('second question')
    assert len(sdk.calls) == 1
    tiny = GeminiLLM(free_tier_confirmed=True, rate_limits={**LIMITS, 'tpm': 1})
    with pytest.raises(GeminiError, match='TPM'):
        tiny.generate('too large')
    assert tiny.generation_calls_issued == 0


def test_minute_window_pacing_is_local_and_does_not_sleep_in_tests(sdk, monkeypatch):
    import src.gemini_client as module
    clock, waits = [0.], []
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(module, 'sleep', sleep)
    llm = GeminiLLM(free_tier_confirmed=True, rate_limits={**LIMITS, 'rpm': 1})
    llm.generate('first')
    llm.generate('second')
    assert waits == [60.]
    assert len(sdk.calls) == 2


@pytest.mark.parametrize('topology,peers', [('fully_connected', 3), ('ring', 1)])
def test_existing_multiagent_engine_uses_gemini_without_routing_changes(sdk, topology, peers):
    result = run_question('Mathematical question without a reference.', backend(), rounds=3, topology=topology)
    assert len(sdk.calls) == 12
    assert result['simulated'] is False and result['model'] == 'gemini-2.5-flash'
    assert result['input_tokens'] == 120 and result['output_tokens'] == 144
    assert [row['active_edges'] for row in result['trace']] == [0, 4 * peers, 4 * peers]
    assert all(len(messages) == peers for messages in result['trace'][1]['messages'].values())
    assert all('Peer messages' not in call['contents'] for call in sdk.calls[:4])
    assert all('Peer messages (previous round):' in call['contents'] for call in sdk.calls[4:])


@pytest.mark.parametrize('config_name,expected_model', [
    ('gemini.yaml', 'gemini-2.5-flash'),
    ('gemini_3_5_flash_lite.yaml', 'gemini-3.5-flash-lite'),
])
def test_cli_selects_gemini_defaults_and_logs_raw_usage(sdk, tmp_path, monkeypatch, capsys,
                                                       config_name, expected_model):
    import sys
    config = yaml.safe_load((run_baseline.ROOT / 'configs' / config_name).read_text())
    config.update(gemini_free_tier_confirmed=True, gemini_rate_limits=LIMITS)
    path = tmp_path / 'gemini.yaml'
    path.write_text(yaml.safe_dump(config), encoding='utf-8')
    output = tmp_path / 'run.jsonl'
    monkeypatch.setattr(run_baseline, 'load_dotenv', lambda *args: None)
    monkeypatch.setattr(run_baseline, 'dotenv_values', lambda *args: {})
    monkeypatch.setenv('OPENAI_MODEL', 'must-not-leak-into-gemini-selection')
    monkeypatch.delenv('GEMINI_MODEL', raising=False)
    monkeypatch.setattr(sys, 'argv', ['test', '--config', str(path), '--output', str(output)])
    run_baseline.main()
    capsys.readouterr()
    record = json.loads(output.read_text())
    assert record['model'] == expected_model
    assert all(call['model'] == expected_model for call in sdk.calls)
    if expected_model == 'gemini-3.5-flash-lite':
        assert sdk.calls[0]['config'].thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
        assert sdk.calls[0]['config'].thinking_config.thinking_budget is None
    assert len(record['provider_usage']) == 8
    assert record['provider_usage'][4]['round'] == 2 and record['provider_usage'][4]['agent_id'] == 0
    assert record['total_tokens'] == sum(row['total_token_count'] for row in record['provider_usage'])
    assert 'unit-test-gemini-key' not in output.read_text()
    assert sdk.closed
