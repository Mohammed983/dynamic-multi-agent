"""Pilot journal/quota regressions. All responses are local test fixtures only."""
from datetime import datetime, timezone
import json

import pytest

from experiments import gemini_pilot
from src.gemini_client import GeminiError
from src.llm_client import Completion
from src.runner import run_question


def access(remaining=499):
    return {'free_tier': True, 'billing_disabled': True, 'remaining_daily_requests': remaining,
            'model': 'gemini-3.5-flash-lite', 'checked_at': datetime.now(timezone.utc).isoformat()}


@pytest.mark.parametrize('remaining', [240, 259, 501, None, True])
def test_quota_reserve_is_required(remaining):
    with pytest.raises(ValueError):
        gemini_pilot.validate_access(access(remaining))


def test_account_access_and_model_checks():
    gemini_pilot.validate_access(access())
    for changes in ({'billing_disabled': False}, {'free_tier': False}, {'model': 'other'},
                    {'checked_at': '2000-01-01T00:00:00+00:00'}):
        with pytest.raises(ValueError):
            gemini_pilot.validate_access({**access(), **changes})


class FixtureBackend:
    model = 'gemini-3.5-flash-lite'
    _key = 'unit-test-key'

    def __init__(self):
        self.generation_calls_issued = 0
        self.usage_records = []
        self.last_response = None
        self.error = None

    def generate(self, prompt):
        self.generation_calls_issued += 1
        if self.error:
            raise self.error
        usage = {'prompt_token_count': 10, 'candidates_token_count': 5, 'thoughts_token_count': 2,
                 'total_token_count': 17, 'finish_reason': 'STOP'}
        self.usage_records.append(usage)
        return Completion('Fixture solution. #### 6', 10, 7, .1)


@pytest.mark.parametrize('topology,edge_count', [('fully_connected', 12), ('ring', 4)])
def test_journal_records_actual_engine_rounds_and_messages(tmp_path, topology, edge_count):
    raw = FixtureBackend()
    backend = gemini_pilot.JournalBackend(raw, tmp_path / 'events.jsonl', 'unit-test', interval=0)
    backend.start_question('fixture:q', topology)
    result = run_question('Fixture question', backend, rounds=3, topology=topology)
    assert raw.generation_calls_issued == 12 and result['total_tokens'] == 204
    assert [r['active_edges'] for r in result['trace']] == [0, edge_count, edge_count]
    for e in backend.events:
        actual_messages = result['trace'][e['round'] - 1]['messages'][str(e['agent_id'])]
        assert actual_messages == e['messages_received']
    events = [json.loads(line) for line in backend.journal.read_text().splitlines()]
    assert len(events) == 24
    assert 'unit-test-key' not in backend.journal.read_text()


def test_unknown_usage_stops_and_preserves_failure(tmp_path):
    raw = FixtureBackend()
    raw.error = GeminiError('Gemini rate/quota limit reached (HTTP 429); no retry or billing change was made.')
    raw.error.http_status = 429
    backend = gemini_pilot.JournalBackend(raw, tmp_path / 'events.jsonl', 'unit-test', interval=0)
    backend.start_question('fixture:q', 'ring')
    with pytest.raises(gemini_pilot.PilotCallError) as caught:
        backend.generate('fixture question')
    assert caught.value.fatal
    assert backend.events[0]['usage_unknown']
    assert backend.events[0]['http_status'] == 429


def test_known_partial_output_is_preserved_without_a_substitute(tmp_path):
    raw = FixtureBackend()
    raw.error = GeminiError('Gemini response was blocked, truncated or incomplete; recorded usage is retained.')
    raw.last_response = {'text': 'Unfinished solution', 'finish_reason': 'MAX_TOKENS', 'latency': .2,
                         'usage': {'prompt_token_count': 10, 'candidates_token_count': 5,
                                   'thoughts_token_count': 0, 'total_token_count': 15}}
    backend = gemini_pilot.JournalBackend(raw, tmp_path / 'events.jsonl', 'unit-test', interval=0)
    backend.start_question('fixture:q', 'fully_connected')
    with pytest.raises(gemini_pilot.PilotCallError) as caught:
        backend.generate('fixture question')
    assert not caught.value.fatal
    assert backend.events[0]['output_text'] == 'Unfinished solution'
    assert backend.events[0]['event'] == 'call_incomplete'


def test_no_more_than_240_requests_and_no_key_in_journal(tmp_path):
    raw = FixtureBackend()
    backend = gemini_pilot.JournalBackend(raw, tmp_path / 'events.jsonl', 'unit-test', interval=0)
    backend.start_question('fixture:q', 'ring')
    with pytest.raises(gemini_pilot.PilotCallError, match='Credential'):
        backend.generate('unit-test-key')
    assert not backend.journal.exists()
    raw.generation_calls_issued = 240
    with pytest.raises(gemini_pilot.PilotCallError, match='limit'):
        backend.generate('fixture question')
    assert not backend.journal.exists()


def test_reviewed_recovery_reuses_real_prefix_without_new_prefix_calls(tmp_path):
    from experiments.resume_gemini_pilot import RealPrefixReplay, RecoveryJournal
    original = gemini_pilot.JournalBackend(FixtureBackend(), tmp_path / 'original.jsonl', 'unit-test', interval=0)
    original.start_question('fixture:q', 'fully_connected')
    expected = run_question('Fixture question', original, rounds=3, topology='fully_connected')
    fresh = FixtureBackend()
    resumed = RecoveryJournal(fresh, tmp_path / 'recovery.jsonl', 'unit-test', interval=0, previous_attempts=46)
    resumed.start_question('fixture:q', 'fully_connected')
    replay = RealPrefixReplay(resumed, original.events[:9], original.events[9])
    result = run_question('Fixture question', replay, rounds=3, topology='fully_connected')
    assert fresh.generation_calls_issued == 3
    assert result['trace'] == expected['trace']
    assert len(resumed.events) == 3
    assert resumed.events[0]['retry_of_call_id'] == original.events[9]['call_id']
    assert 'retry_of_call_id' not in resumed.events[1]


def test_recovery_refuses_changed_prefix_prompt(tmp_path):
    from experiments.resume_gemini_pilot import RealPrefixReplay, RecoveryJournal
    raw = FixtureBackend()
    resumed = RecoveryJournal(raw, tmp_path / 'recovery.jsonl', 'unit-test', interval=0, previous_attempts=46)
    resumed.start_question('fixture:q', 'ring')
    prefix = [{'event': 'call_completed', 'prompt': 'original', 'round': 1, 'agent_id': 0}]
    with pytest.raises(gemini_pilot.PilotCallError, match='identity/prompt'):
        RealPrefixReplay(resumed, prefix, None).generate('different')
    assert raw.generation_calls_issued == 0
    assert not resumed.journal.exists()


def test_recovery_ceiling_counts_prior_attempts(tmp_path):
    from experiments.resume_gemini_pilot import RecoveryJournal
    raw = FixtureBackend()
    resumed = RecoveryJournal(raw, tmp_path / 'recovery.jsonl', 'unit-test', interval=0, previous_attempts=240)
    resumed.start_question('fixture:q', 'ring')
    resumed.generate('fixture question')
    with pytest.raises(gemini_pilot.PilotCallError, match='241-attempt'):
        resumed.generate('fixture question')
    assert raw.generation_calls_issued == 1
