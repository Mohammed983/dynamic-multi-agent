"""Audit regressions: no external services or optimization steps are used."""
from dataclasses import replace
import json
import sys

import numpy as np
import pytest
import torch
import yaml

from experiments import run_baseline
from src.agents import Response
from src.benchmarks import BenchmarkItem
from src.controllers.base import select_edges
from src.controllers.mlp import EdgeMLP, MLPController
from src.evaluation import extract_answer, normalize_number
from src.features import FEATURE_NAMES, FeatureExtractor
from src.llm_client import Completion, DEMO_QUESTION
from src.runner import run_question
from src.training import EdgeTrainingExample, append_example, collect_paired_example, train_controller


def snapshot(round_number=1):
    return tuple(Response(i, round_number, f'Agent {i} solution. #### 6', '6', 10, 3, 0.) for i in range(4))


def record(split='train', item_id='question:a', utility=0):
    return EdgeTrainingExample(item_id, split, (0, 1), (0.,) * 12, utility, (), 0,
                               'audit-fixture', 'snapshot:a', 'run:without', 'run:with')


@pytest.mark.parametrize('value', [
    '123456789012345678901234567890123456789',
    '0.123456789012345678901234567890123456789',
])
def test_decimal_exact_match_does_not_round_distinct_answers(value):
    assert normalize_number(value) == value
    assert extract_answer(f'#### {value}') == value
    assert normalize_number(value + '1') != normalize_number(value)


@pytest.mark.parametrize('bad_history', [
    (snapshot()[:3] + (snapshot()[0],),),
    (snapshot(), snapshot(3)),
    (snapshot(), snapshot(2)[:3]),
    (tuple(replace(r, round=2) for r in snapshot()),),
])
def test_malformed_feature_histories_are_rejected(bad_history):
    with pytest.raises(ValueError, match='complete, consecutive'):
        FeatureExtractor().extract(bad_history)


@pytest.mark.parametrize('threshold,top_k', [(float('nan'), None), (float('inf'), None),
                                          (.5, 1.5), (.5, True)])
def test_invalid_selection_settings_fail_clearly(threshold, top_k):
    features = FeatureExtractor().extract((snapshot(),))
    with pytest.raises(ValueError):
        select_edges(features, np.ones(12), 4, threshold, top_k)


@pytest.mark.parametrize('bad_result', ['False', {'correct': False}, None, 0, 1])
def test_collector_rejects_non_boolean_outcomes(bad_result):
    with pytest.raises(ValueError, match='boolean correctness'):
        collect_paired_example(item_id='q', split='train', edge=(0, 1), features=(0.,) * 12,
                               context_edges=(), rollout=lambda edges: bad_result, replicate=0,
                               experiment_id='audit', snapshot_id='s', without_run_id='a', with_run_id='b')


def test_collection_validates_json_edge_shapes_before_any_rollout():
    def forbidden_rollout(edges):
        pytest.fail('Invalid record reached a potentially paid rollout')
    with pytest.raises(ValueError, match='exclude the intervention'):
        collect_paired_example(item_id='q', split='train', edge=(0, 1), features=(0.,) * 12,
                               context_edges=([0, 1],), rollout=forbidden_rollout, replicate=0,
                               experiment_id='audit', snapshot_id='s', without_run_id='a', with_run_id='b')


@pytest.mark.parametrize('changes', [
    {'split': 'test'}, {'features': ((0., 0.),) * 12}, {'features': (float('nan'),) * 12},
    {'features': (1e100,) * 12}, {'utility': 2}, {'feature_version': -1},
    {'context_edges': ([0, 1],)}, {'context_edges': ((2, 3), (2, 3))},
    {'edge': (-1, 2)}, {'snapshot_id': ''}, {'with_run_id': 'run:without'}, {'replicate': -1},
])
def test_training_and_persistence_reject_invalid_records_without_training(changes, tmp_path, monkeypatch):
    def forbidden_optimizer(*args, **kwargs):
        pytest.fail('Input validation must happen before any optimizer is created')
    monkeypatch.setattr(torch.optim, 'Adam', forbidden_optimizer)
    malformed = replace(record(), **changes)
    rows = [malformed, record(item_id='question:b', utility=1),
            record(split='validation', item_id='question:c')]
    with pytest.raises(ValueError):
        train_controller(rows)
    with pytest.raises(ValueError):
        append_example(tmp_path / 'invalid.jsonl', malformed)
    assert not (tmp_path / 'invalid.jsonl').exists()


def test_training_question_leakage_and_class_guards_without_optimization(monkeypatch):
    def forbidden_optimizer(*args, **kwargs):
        pytest.fail('Guard regression attempted optimization')
    monkeypatch.setattr(torch.optim, 'Adam', forbidden_optimizer)
    cases = [
        [],
        [record(), record(split='validation', item_id='question:v')],
        [record(), record(item_id='question:b', utility=1), record(split='validation')],
    ]
    for rows in cases:
        with pytest.raises(ValueError):
            train_controller(rows)
    for bad_rate in (float('nan'), float('inf'), 0.):
        with pytest.raises(ValueError):
            train_controller([], learning_rate=bad_rate)


def test_falsy_controller_is_still_used_and_feature_version_is_logged():
    import networkx as nx
    class Backend:
        model, simulated = 'audit-only', True
        def generate(self, prompt):
            return Completion('#### 6', 1, 2, 0.)
    class FalsyController:
        def __bool__(self):
            return False
        def select(self, features, count):
            graph = nx.DiGraph()
            graph.add_nodes_from(range(count))
            graph.add_edge(0, 1)
            return graph
    result = run_question('question', Backend(), topology='dynamic', controller=FalsyController())
    assert result['trace'][1]['edges'] == [(0, 1)]
    assert result['trace'][1]['candidate_features']['feature_version'] == 1


def test_reference_changes_do_not_change_prompts_or_controller_inputs(tmp_path, monkeypatch, capsys):
    """Exercise the evaluation wrapper with real reference sentinels, not absent strings."""
    from src.topology import make_topology
    prompts, feature_inputs = [], []
    class Backend:
        model, simulated = 'audit-fixture', True
        def generate(self, prompt):
            prompts.append(prompt)
            return Completion('#### 6', 10, 3, 0.)
    class Controller:
        def __init__(self, *args):
            pass
        def select(self, features, count):
            feature_inputs.append(features.values.copy())
            return make_topology('ring', count)
    monkeypatch.setattr(run_baseline, 'load_dotenv', lambda *args: None)
    monkeypatch.setattr(run_baseline, 'OpenAILLM', lambda *args: Backend())
    monkeypatch.setattr(run_baseline, 'HeuristicController', Controller)
    items = [BenchmarkItem('q1', 'A question without the reference', '#### 987654321', 'validation'),
             BenchmarkItem('q2', 'A question without the reference', '#### -123456789', 'validation')]
    monkeypatch.setattr(run_baseline.GSM8K, 'load', lambda *args: items)
    output = tmp_path / 'audit_records.jsonl'
    monkeypatch.setattr(sys, 'argv', ['audit', '--backend', 'openai', '--benchmark', 'gsm8k',
                                   '--topology', 'heuristic', '--output', str(output)])
    run_baseline.main()
    capsys.readouterr()
    assert prompts[:8] == prompts[8:]
    assert all('987654321' not in p and '-123456789' not in p for p in prompts)
    np.testing.assert_array_equal(feature_inputs[0], feature_inputs[1])
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [r['ground_truth'] for r in records] == [item.reference for item in items]
    assert all(r['prediction'] == '6' and not r['correct'] for r in records)


def test_dynamic_entrypoint_respects_explicit_config(tmp_path, monkeypatch, capsys):
    config = yaml.safe_load((run_baseline.ROOT / 'configs/baseline.yaml').read_text())
    config['topology'] = 'ring'
    config_path = tmp_path / 'explicit.yaml'
    config_path.write_text(yaml.safe_dump(config), encoding='utf-8')
    output = tmp_path / 'explicit.jsonl'
    monkeypatch.setattr(run_baseline, 'load_dotenv', lambda *args: None)
    monkeypatch.setattr(sys, 'argv', ['audit', '--config', str(config_path), '--output', str(output)])
    run_baseline.main(default_topology='heuristic')
    capsys.readouterr()
    result = json.loads(output.read_text())
    assert result['topology'] == 'ring'
    assert result['config']['output'] == str(output)


def test_untrained_checkpoint_rejected_before_inference(tmp_path, monkeypatch):
    checkpoint = tmp_path / 'untrained_fixture.pt'
    MLPController(EdgeMLP((2,))).save(checkpoint)
    class Backend:
        def generate(self, prompt):
            pytest.fail('Untrained checkpoint caused an inference call')
    monkeypatch.setattr(run_baseline, 'MockLLM', Backend)
    monkeypatch.setattr(run_baseline, 'load_dotenv', lambda *args: None)
    monkeypatch.setattr(sys, 'argv', ['audit', '--topology', 'mlp', '--checkpoint', str(checkpoint)])
    with pytest.raises(SystemExit):
        run_baseline.main()
