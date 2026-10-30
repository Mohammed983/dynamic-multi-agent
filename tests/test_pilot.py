"""Pilot safety checks; SDK fixtures never become experiment results or make HTTP calls."""
import json
from types import SimpleNamespace

import pytest

from experiments import real_pilot
from src.agents import Agent
from src.pilot import Budget, MeteredOpenAILLM, PilotStopped, input_reservation, usd


def response(status='completed', usage=True):
    return SimpleNamespace(id='unit-test-response', model='unit-test-model', status=status,
                           output_text='#### 6', service_tier='default', incomplete_details=None,
                           usage=SimpleNamespace(input_tokens=20, output_tokens=30, total_tokens=50,
                               input_tokens_details=SimpleNamespace(cached_tokens=10),
                               output_tokens_details=SimpleNamespace(reasoning_tokens=20)) if usage else None)


def client(result):
    def create(**kwargs):
        assert kwargs['model'] == 'gpt-5-mini'
        assert kwargs['reasoning'] == {'effort': 'low'}
        assert kwargs['service_tier'] == 'default'
        assert kwargs['max_output_tokens'] == 2048
        assert kwargs['store'] is False
        return result
    return SimpleNamespace(responses=SimpleNamespace(create=create))


def test_pilot_total_capped_cost_and_pre_request_limits():
    assert 240 * usd(12000, 2048) == pytest.approx(1.70304)
    with pytest.raises(PilotStopped, match='spending budget'):
        Budget(limit_usd=.000001).reserve('question')
    with pytest.raises(PilotStopped, match='Input safety'):
        Budget().reserve('q' * 12000)
    assert input_reservation('é') == 258


def test_provider_usage_and_prompt_journal_are_preserved(tmp_path):
    budget = Budget()
    backend = MeteredOpenAILLM(client(response()), budget, tmp_path / 'calls.jsonl', 'unit-test-key')
    backend.start_question('test:q', 'ring')
    result = backend.generate('test question with no reference')
    assert result.input_tokens == 20 and result.output_tokens == 30
    events = [json.loads(line) for line in backend.journal.read_text().splitlines()]
    assert [e['event'] for e in events] == ['request_started', 'response_received']
    assert events[0]['prompt'] == 'test question with no reference'
    assert events[1]['reasoning_tokens'] == 20
    assert events[1]['cached_input_tokens'] == 10
    assert budget.committed_usd == pytest.approx(usd(20, 30))
    assert events[1]['estimated_charge_usd'] == pytest.approx(usd(20, 30, 10))
    assert 'unit-test-key' not in backend.journal.read_text()


@pytest.mark.parametrize('result', [response(status='incomplete'), response(usage=False)])
def test_incomplete_or_unmetered_response_is_logged_and_halts(tmp_path, result):
    backend = MeteredOpenAILLM(client(result), Budget(), tmp_path / 'calls.jsonl', '')
    backend.start_question('test:q', 'fully_connected')
    with pytest.raises(PilotStopped):
        backend.generate('question')
    assert backend.events[-1]['output_text'] == '#### 6'
    assert backend.journal.exists()


def test_raw_sdk_error_never_reveals_credential_or_refunds_unknown_attempt(tmp_path):
    def create(**kwargs):
        raise RuntimeError('sensitive unit-test-key must not appear')
    backend = MeteredOpenAILLM(SimpleNamespace(responses=SimpleNamespace(create=create)),
                               Budget(), tmp_path / 'calls.jsonl', 'unit-test-key')
    backend.start_question('test:q', 'ring')
    with pytest.raises(PilotStopped) as caught:
        backend.generate('question')
    assert 'unit-test-key' not in str(caught.value)
    assert 'unit-test-key' not in backend.journal.read_text()
    assert backend.budget.committed_usd == pytest.approx(usd(input_reservation('question'), 2048))
    assert backend.events[0]['usage_unknown']


def test_credential_in_prompt_is_blocked_before_logging_or_network(tmp_path):
    backend = MeteredOpenAILLM(None, Budget(), tmp_path / 'calls.jsonl', 'unit-test-key')
    with pytest.raises(PilotStopped, match='Credential detected'):
        backend.generate('unit-test-key')
    assert not backend.journal.exists() and backend.budget.issued_calls == 0


def test_shared_prompt_builder_preserves_original_template():
    prompt = Agent(0, None).build_prompt('Question?', ())
    assert prompt == ('You are agent 0. Solve the question carefully. '
                      'Give a concise solution explanation and end with #### <numeric answer>.\n'
                      'Question: Question?\n')


def test_unapproved_pilot_never_constructs_api_client(tmp_path, monkeypatch):
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'status': 'awaiting_budget_approval'}))
    receipt = tmp_path / 'approval.json'
    receipt.write_text(json.dumps({'approved': False}))
    monkeypatch.setattr(real_pilot, 'PLAN', plan)
    monkeypatch.setattr(real_pilot, 'configure_caches', lambda: None)
    monkeypatch.setattr(real_pilot, 'api_client', lambda: pytest.fail('Unapproved run constructed an API client'))
    with pytest.raises(PilotStopped, match='Explicit human approval'):
        real_pilot.run(receipt)


def test_rejected_first_request_is_not_reported_as_zero_accuracy_or_zero_usage(tmp_path, monkeypatch):
    report = tmp_path / 'report.md'
    monkeypatch.setattr(real_pilot, 'REPORT', report)
    records = [dict(item_id='test:q', topology='fully_connected', status='failed_or_incomplete',
                    correct=False, question_wall_seconds=5.),
               dict(item_id='test:q', topology='ring', status='not_run', correct=False)]
    events = [dict(item_id='test:q', topology='fully_connected', event='request_failed',
                   error_type='RateLimitError', http_status=429, usage_unknown=True,
                   latency=4.9, round=1, agent_id=0)]
    real_pilot.write_report({'status': 'halted_incomplete'}, records, events)
    content = report.read_text(encoding='utf-8')
    assert '| fully_connected | 0/10 | N/A | N/A | Unknown | Unknown | 4.900 | N/A |' in content
    assert '| ring | 0/10 | N/A | N/A | Not run | Not run | Not run | N/A |' in content
    assert 'no token-metered response received' in content
    assert '`RateLimitError`, HTTP `429`' in content
