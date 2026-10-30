"""Opt-in strategies, benchmark isolation and diagnostic safety; no network inference."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

from experiments import run_diagnostic
from src.agents import Agent
from src.benchmarks import AQUARAT, GSM8K, BenchmarkItem
from src.diagnostics import question_diagnostics, summarize_diagnostics
from src.difficulty import question_characteristics, select_indices
from src.evaluation import extract_option_answer, majority_vote_options, option_exact_match
from src.features import FeatureExtractor
from src.llm_client import Completion
from src.roles import load_roles, parse_roles
from src.runner import run_question

ROOT = Path(__file__).resolve().parents[1]


class RecordingBackend:
    model, simulated = 'offline-unit-test', True

    def __init__(self):
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        agent_id = int(prompt.split('You are agent ', 1)[1].split('.', 1)[0])
        return Completion(f'Independent fixture solution {len(self.prompts)}. #### {"ABCD"[agent_id]}', 10, 4, .01)


def roles():
    return load_roles(ROOT / 'configs/diverse_roles.yaml')


def test_default_prompt_remains_byte_identical():
    prompt = Agent(0, RecordingBackend()).build_prompt('Question?', ())
    assert prompt == ('You are agent 0. Solve the question carefully. '
                      'Give a concise solution explanation and end with #### <numeric answer>.\n'
                      'Question: Question?\n')
    assert 'Reasoning strategy' not in prompt


def test_roles_fixed_across_topologies_and_independent_first_round():
    left, right = RecordingBackend(), RecordingBackend()
    full = run_question('Same public question', left, rounds=3, topology='fully_connected', roles=roles(), answer_format='option')
    ring = run_question('Same public question', right, rounds=3, topology='ring', roles=roles(), answer_format='option')
    assert left.prompts[:4] == right.prompts[:4]
    assert full['agent_roles'] == ring['agent_roles']
    assert len(set(left.prompts[:4])) == 4
    for i, prompt in enumerate(left.prompts):
        assert roles()[i % 4].instruction in prompt
        if i < 4:
            assert 'Peer messages' not in prompt and 'Your previous response' not in prompt
        else:
            assert 'Peer messages (previous round):' in prompt
    assert all(len(m) == 3 for m in full['trace'][1]['messages'].values())
    assert all(len(m) == 1 for m in ring['trace'][1]['messages'].values())
    assert full['prediction'] == ring['prediction'] == 'A'


def test_role_configuration_validation_and_pre_call_count_check():
    for rows in ([], [{'name': 'a', 'instruction': ''}] * 4,
                 [{'name': 'same', 'instruction': 'valid'}] * 4):
        with pytest.raises(ValueError):
            parse_roles(rows, 4)
    backend = RecordingBackend()
    with pytest.raises(ValueError):
        run_question('question', backend, roles=roles()[:3])
    assert backend.prompts == []


@pytest.mark.parametrize('text,expected', [
    ('Reasoning names A and D.\n#### C', 'C'), ('#### (b)', 'B'), ('Final answer: option E', 'E'),
    ('Answer is A.', 'A'), ('D', 'D'), ('(C)', 'C'), ('#### A or B', None),
    ('Question includes A and B, result is 42.', None), ('#### 42', None), ('#### F', None),
    ('#### A\nActually unsure', None), ('#### invalid\nFinal answer: B', None),
])
def test_option_extraction(text, expected):
    assert extract_option_answer(text) == expected


def test_option_evaluation_and_ties_do_not_use_gold():
    assert majority_vote_options(['D', 'C', 'C', 'D']) == 'C'
    assert majority_vote_options([None, None]) is None
    assert option_exact_match('C', 'C') and not option_exact_match('C', 'D')
    assert not option_exact_match('42', 'B')


def aqua_rows():
    return [{'question': f'Public problem {i}: a ratio of {i+2} to 3 with 4 quantities remaining.',
             'options': ['A)10', 'B)20', 'C)30', 'D)40', 'E)50'],
             'rationale': 'SECRET_RATIONALE_SENTINEL', 'correct': 'ABCDE'[i % 5]} for i in range(30)]


def fake_dataset(monkeypatch, data):
    calls = []
    def loader(name, config, **kwargs):
        calls.append((name, config, kwargs))
        return deepcopy(data)
    monkeypatch.setitem(sys.modules, 'datasets', SimpleNamespace(load_dataset=loader))
    return calls


def test_aqua_native_splits_provenance_and_no_rationale_in_runtime(monkeypatch):
    calls = fake_dataset(monkeypatch, aqua_rows())
    benchmark = AQUARAT()
    selected = {split: benchmark.load(split, 10, 42) for split in ('train', 'validation', 'test')}
    assert all(c[0:2] == ('deepmind/aqua_rat', 'raw') for c in calls)
    assert [c[2]['split'] for c in calls] == ['train', 'validation', 'test']
    assert all(c[2]['revision'] == AQUARAT.DEFAULT_REVISION for c in calls)
    assert not {q.item_id for q in selected['train']} & {q.item_id for q in selected['validation']}
    item = selected['validation'][0]
    assert 'Options:\nA)10' in item.question and 'SECRET_RATIONALE_SENTINEL' not in item.question
    assert 'correct' not in item.provenance and 'rationale' not in item.provenance
    assert benchmark.evaluate(item.reference, item.reference)
    assert [q.item_id for q in benchmark.load('validation', 10, 42)] == [q.item_id for q in selected['validation']]
    with pytest.raises(ValueError, match='commit'):
        AQUARAT('main')


def test_label_and_rationale_changes_cannot_change_selection_or_prompts(monkeypatch):
    data = aqua_rows()
    policy = {'mode': 'question_complexity', 'version': 1, 'min_numbers': 3}
    fake_dataset(monkeypatch, data)
    first = AQUARAT(difficulty=policy).load('validation', 10, 42)
    changed = [{**row, 'rationale': 'DIFFERENT_PRIVATE_RATIONALE', 'correct': 'E'} for row in data]
    fake_dataset(monkeypatch, changed)
    second = AQUARAT(difficulty=policy).load('validation', 10, 42)
    assert [q.item_id for q in first] == [q.item_id for q in second]
    assert [q.question for q in first] == [q.question for q in second]
    assert [q.provenance for q in first] == [q.provenance for q in second]
    backend = RecordingBackend()
    run_question(first[0].question, backend, rounds=1, topology='empty', roles=roles(), answer_format='option')
    assert all('SECRET_RATIONALE_SENTINEL' not in prompt and 'DIFFERENT_PRIVATE_RATIONALE' not in prompt
               for prompt in backend.prompts)


def test_option_consensus_features_and_dynamic_controller_remain_usable():
    from src.controllers.heuristic import HeuristicController
    result = run_question('Public multiple-choice question', RecordingBackend(), rounds=3,
                          topology='dynamic', controller=HeuristicController(), roles=roles(), answer_format='option')
    assert result['prediction'] == 'A'
    assert len(result['trace'][1]['candidate_features']['edges']) == 12


def test_question_only_difficulty_ranking_and_failure_on_insufficient_items():
    texts = ['Short 1.', 'A longer ratio of 1 to 2 per 3 each with remaining 4.', 'Other 2.']
    assert question_characteristics(texts[1])['score'] > question_characteristics(texts[0])['score']
    policy = {'mode': 'question_complexity', 'version': 1}
    assert select_indices(texts, [2, 0, 1], 1, policy) == [1]
    assert select_indices(texts, [2, 0, 1], 2, None) == [2, 0]
    with pytest.raises(ValueError, match='eligible'):
        select_indices(texts, [0, 1, 2], 3, {**policy, 'min_words': 100})
    with pytest.raises(ValueError):
        select_indices(texts, [0, 1, 2], 1, {'mode': 'model_error_score'})


def test_gsm8k_difficulty_retains_partition_before_ranking(monkeypatch):
    data = [{'question': f'A long ratio problem {i} with 2 and 3 remaining each total 4.', 'answer': '#### 4'}
            for i in range(100)]
    fake_dataset(monkeypatch, data)
    source = GSM8K(difficulty={'mode': 'question_complexity', 'version': 1, 'min_numbers': 3})
    train = source.load('train', 10, 42)
    validation = source.load('validation', 10, 42)
    assert not {q.item_id for q in train} & {q.item_id for q in validation}
    assert all(q.provenance['difficulty_policy']['mode'] == 'question_complexity' for q in train)


def test_unanimous_wrong_answers_are_flagged_not_just_disagreement():
    d = question_diagnostics(['B'] * 4, 'A', 'option', option_exact_match)
    assert d['unanimous'] and not d['disagreement']
    assert d['incorrect_agent_ids'] == [0, 1, 2, 3] and d['at_least_one_incorrect']
    assert d['majority_correct'] is False


def test_missing_answers_are_never_unanimous_or_fabricated_wrong_responses():
    d = question_diagnostics([None] * 4, 'A', 'option', option_exact_match, [False] * 4)
    assert not d['unanimous'] and not d['disagreement']
    assert d['agent_correctness'] == [None] * 4
    assert d['incorrect_agent_ids'] == [] and d['majority_prediction'] is None
    assert all(pair['agree'] is None for pair in d['pairwise_agreement'])
    summary = summarize_diagnostics([{'item_id': 'missing', 'diagnostics': d}])
    assert summary['majority_accuracy_planned'] is None
    assert all(agent['planned_accuracy'] is None for agent in summary['individual_agents'])


def test_diagnostic_accuracy_agreement_and_disagreement_denominators():
    same = question_diagnostics(['A'] * 4, 'A', 'option', option_exact_match)
    mixed = question_diagnostics(['A', 'B', 'B', 'C'], 'B', 'option', option_exact_match)
    summary = summarize_diagnostics([{'item_id': 'same', 'diagnostics': same},
                                      {'item_id': 'mixed', 'diagnostics': mixed}], planned_questions=2)
    assert summary['majority_accuracy_planned'] == 1
    assert summary['unanimous_rate_planned'] == summary['disagreement_rate_planned'] == .5
    assert summary['items_with_observed_incorrect_agent'] == ['mixed']
    assert [a['planned_accuracy'] for a in summary['individual_agents']] == [.5, 1., 1., .5]


def test_diagnostic_rejects_communication_rounds_and_test_tuning(tmp_path):
    cfg = yaml.safe_load((ROOT / 'configs/diagnostic_aqua.yaml').read_text())
    for changes in ({'rounds': 3}, {'topology': 'ring'}, {'agents': 5}, {'split': 'test'}):
        path = tmp_path / 'invalid.yaml'
        path.write_text(yaml.safe_dump({**cfg, **changes}), encoding='utf-8')
        with pytest.raises(ValueError):
            run_diagnostic.settings(path)


def test_unapproved_diagnostic_cannot_construct_client_or_make_metadata_call(tmp_path, monkeypatch):
    monkeypatch.setattr(run_diagnostic, 'GeminiLLM', lambda *a, **k: pytest.fail('Unapproved client construction'))
    cfg, paths = run_diagnostic.settings(ROOT / 'configs/diagnostic_aqua.yaml')
    with pytest.raises(ValueError, match='locked'):
        run_diagnostic.execute(cfg, paths, ROOT / 'configs/diagnostic_aqua.yaml', None)
    plan = tmp_path / 'plan.json'
    plan.write_text('{}')
    with pytest.raises(ValueError, match='Explicit human approval'):
        run_diagnostic.validate_approval({'approved': False}, plan, cfg)
    receipt = {'approved': True, 'plan_sha256': run_diagnostic.digest(plan), 'free_tier': True,
               'billing_disabled': True, 'remaining_daily_requests': 99,
               'checked_at': datetime.now(timezone.utc).isoformat()}
    with pytest.raises(ValueError, match='remaining-quota'):
        run_diagnostic.validate_approval(receipt, plan, cfg)


def test_prepare_freezes_twenty_questions_without_provider_or_rationales(tmp_path, monkeypatch):
    cfg_path = ROOT / 'configs/diagnostic_aqua.yaml'
    cfg, _ = run_diagnostic.settings(cfg_path)
    paths = {name: tmp_path / name for name in ('plan', 'questions', 'references', 'calls', 'records', 'report', 'summary', 'state')}
    class Dataset:
        answer_format = 'option'
        def load(self, split, limit, seed):
            return [BenchmarkItem(f'fixture:{i}', f'Public question {i}\nOptions: A)... E)', 'A', split,
                                  {'selection': 'fixture'}) for i in range(20)]
    monkeypatch.setattr(run_diagnostic, 'benchmark', lambda cfg: Dataset())
    monkeypatch.setattr(run_diagnostic, 'GeminiLLM', lambda *a, **k: pytest.fail('Prepare constructed provider client'))
    for name in ('HF_HOME', 'HF_DATASETS_CACHE', 'HF_HUB_DISABLE_IMPLICIT_TOKEN'):
        monkeypatch.setenv(name, 'unit-test-original')
    run_diagnostic.prepare(cfg, paths, cfg_path)
    plan = json.loads(paths['plan'].read_text())
    assert plan['request_estimate'] == 80 and plan['generation_calls_issued'] == 0
    assert len(paths['questions'].read_text().splitlines()) == 20
    assert 'reference' not in paths['questions'].read_text()
    assert 'rationale' not in paths['questions'].read_text()
    assert not paths['calls'].exists() and not paths['records'].exists()
    assert 'No Gemini inference has run' in paths['report'].read_text()


@pytest.mark.parametrize('fail_at', [None, 3])
def test_diagnostic_execution_fixture_retains_answers_usage_and_stops_on_failure(tmp_path, monkeypatch, fail_at):
    """Exercise the live-shaped workflow entirely with a local fake, never a provider."""
    from experiments.gemini_pilot import JournalBackend
    from src.gemini_client import GeminiError

    cfg_path = ROOT / 'configs/diagnostic_aqua.yaml'
    cfg, _ = run_diagnostic.settings(cfg_path)
    paths = {name: tmp_path / name for name in ('plan', 'questions', 'references', 'calls', 'records', 'report', 'summary', 'state')}
    class Dataset:
        answer_format = 'option'
        evaluate = staticmethod(option_exact_match)
        def load(self, split, limit, seed):
            return [BenchmarkItem(f'fixture:{i}', f'Public question {i}\nA) first B) second C) third D) fourth E) fifth',
                                  'E', split) for i in range(20)]
    monkeypatch.setattr(run_diagnostic, 'benchmark', lambda cfg: Dataset())
    for name in ('HF_HOME', 'HF_DATASETS_CACHE', 'HF_HUB_DISABLE_IMPLICIT_TOKEN', 'GEMINI_API_KEY'):
        monkeypatch.setenv(name, 'offline-test')
    run_diagnostic.prepare(cfg, paths, cfg_path)
    receipt = tmp_path / 'approval.json'
    receipt.write_text(json.dumps({'approved': True, 'plan_sha256': run_diagnostic.digest(paths['plan']),
        'free_tier': True, 'billing_disabled': True, 'remaining_daily_requests': 100,
        'checked_at': datetime.now(timezone.utc).isoformat()}))
    instances = []
    class FakeGemini:
        model, _key = 'offline-test-only', 'offline-test-key'
        def __init__(self, *args, **kwargs):
            self.generation_calls_issued, self.usage_records, self.last_response = 0, [], None
            self.prompts, self.closed = [], False
            instances.append(self)
        def verify_model(self):
            return {'name': self.model}
        def generate(self, prompt):
            self.prompts.append(prompt)
            self.generation_calls_issued += 1
            if self.generation_calls_issued == fail_at:
                self.last_response = {'text': None, 'latency': .02, 'usage': None}
                raise GeminiError('Offline quota failure fixture')
            self.usage_records.append({'prompt_token_count': 10, 'candidates_token_count': 4,
                                      'thoughts_token_count': 0, 'total_token_count': 14, 'finish_reason': 'STOP'})
            return Completion('Local fixture explanation. #### E', 10, 4, .01)
        def close(self):
            self.closed = True
    monkeypatch.setattr(run_diagnostic, 'GeminiLLM', FakeGemini)
    monkeypatch.setattr(run_diagnostic, 'dotenv_values', lambda path: {'GEMINI_API_KEY': 'offline-test-key'})
    monkeypatch.setattr(run_diagnostic, 'JournalBackend', lambda raw, path, run_id, interval, **kw:
                        JournalBackend(raw, path, run_id, interval=0, **kw))
    run_diagnostic.execute(cfg, paths, cfg_path, receipt)
    rows = [json.loads(line) for line in paths['records'].read_text().splitlines()]
    events = [json.loads(line) for line in paths['calls'].read_text().splitlines()]
    state = json.loads(paths['state'].read_text())
    raw = instances[0]
    assert raw.closed and len(rows) == 20
    assert raw.generation_calls_issued == (fail_at or 80)
    assert all('#### E' not in prompt and 'reference' not in prompt and 'Peer messages' not in prompt for prompt in raw.prompts)
    assert all(event['round'] == 1 and event['active_edges'] == 0 and event['messages_received'] == [] for event in events)
    completed = [e for e in events if e['event'] == 'call_completed']
    assert all(e['answer'] == 'E' and e['usage']['total_token_count'] == 14 for e in completed)
    if fail_at:
        assert state['status'] == 'halted_incomplete'
        assert rows[0]['status'] == 'failed_or_incomplete'
        assert all(row['status'] == 'not_run' for row in rows[1:])
        assert rows[0]['diagnostics']['unavailable_agent_ids'] == [2, 3]
        assert events[-1]['usage_unknown']
    else:
        assert state['status'] == 'completed'
        assert all(row['diagnostics']['majority_correct'] for row in rows)
        assert all(row['trace'][0]['active_edges'] == 0 for row in rows)
