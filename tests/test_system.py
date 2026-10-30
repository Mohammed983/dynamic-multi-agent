from dataclasses import replace
import inspect
import json
import networkx as nx
import numpy as np
import pytest
import torch
from src.agents import Response
from src.benchmarks import partition_indices
from src.communication import route_messages
from src.controllers.base import select_edges
from src.controllers.heuristic import HeuristicController
from src.controllers.mlp import EdgeMLP, MLPController
from src.evaluation import extract_answer, majority_vote, exact_match
from src.features import FEATURE_NAMES, FeatureExtractor
from src.llm_client import Completion, DEMO_QUESTION, MockLLM, OpenAILLM
from src.runner import run_question
from src.topology import make_topology, validate_graph
from src.training import EdgeTrainingExample, collect_paired_example, train_controller


def responses(round_number=1):
    return tuple(Response(i, round_number, f'Calculation agent {i}. #### {i % 2}', str(i % 2), 10, 5, 0.1)
                 for i in range(4))


@pytest.mark.parametrize('kind,expected', [('fully_connected', 12), ('ring', 4), ('empty', 0), ('dynamic', 0), ('static_sparse', 2)])
def test_topology(kind, expected):
    graph = make_topology(kind, 4, [(0, 1), (2, 3)])
    assert graph.number_of_edges() == expected
    assert not list(nx.selfloop_edges(graph))


def test_invalid_graphs():
    for edges in ([(0, 0)], [(0, 4)], [(-1, 2)]):
        with pytest.raises(ValueError):
            make_topology('static_sparse', 4, edges)
    assert make_topology('ring', 1).number_of_edges() == 0
    assert make_topology('fully_connected', 1, allow_self_loops=True).number_of_edges() == 1
    with pytest.raises(ValueError):
        validate_graph(nx.Graph(), 0)


def test_routing_and_barrier_validation():
    previous = {r.agent_id: r for r in responses()}
    inboxes = route_messages(make_topology('ring', 4), previous)
    assert [[m.agent_id for m in inboxes[i]] for i in range(4)] == [[3], [0], [1], [2]]
    previous[0] = replace(previous[0], round=2)
    with pytest.raises(ValueError, match='same completed round'):
        route_messages(make_topology('ring', 4), previous)


class SpyBackend:
    model, simulated = 'spy', True

    def __init__(self):
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return Completion(f'Unique call {len(self.prompts)}. #### 6', 3, 4, 0.01)


def test_actual_prompts_are_synchronized_and_ground_truth_free():
    backend = SpyBackend()
    result = run_question('Question without a reference sentinel', backend, rounds=3, topology='ring')
    for index, prompt in enumerate(backend.prompts):
        current_round = index // 4 + 1
        assert 'SECRET_GROUND_TRUTH' not in prompt
        if current_round == 1:
            assert 'Peer messages' not in prompt
        else:
            for call in range((current_round - 1) * 4 + 1, current_round * 4 + 1):
                assert f'Unique call {call}.' not in prompt
            source = ((index % 4) - 1) % 4
            assert f'Agent {source}: Unique call {(current_round - 2) * 4 + source + 1}.' in prompt
    assert result['total_tokens'] == 12 * 7
    assert [r['active_edges'] for r in result['trace']] == [0, 4, 4]
    assert 'reference' not in inspect.signature(run_question).parameters
    assert 'ground_truth' not in json.dumps(result)
    assert exact_match(result['prediction'], '#### 6')


class SwitchingController:
    def __init__(self):
        self.calls = 0

    def select(self, features, count):
        assert features.values.shape == (12, len(FEATURE_NAMES))
        self.calls += 1
        return make_topology('static_sparse', count, [(0, 1)] if self.calls == 1 else [(2, 3)])


def test_dynamic_add_remove_and_following_round():
    result = run_question(DEMO_QUESTION, MockLLM(), rounds=3, topology='dynamic', controller=SwitchingController())
    assert result['trace'][1]['edges'] == [(0, 1)]
    assert result['trace'][2]['edges'] == [(2, 3)]
    assert len(result['trace'][1]['messages']['1']) == 1
    assert result['trace'][2]['messages']['1'] == []
    assert result['trace'][2]['messages']['3'][0]['agent_id'] == 2


def test_feature_masks_and_consensus():
    extractor = FeatureExtractor()
    first = extractor.extract((responses(),))
    assert first.values.shape == (12, 12)
    assert not first.values[:, 5:11].any()
    second = extractor.extract((responses(), responses(2)))
    assert np.all(second.values[:, 7] == 1)
    assert np.all(second.values[:, 8:11] == 1)
    assert np.all(second.values[:, 5:7] == 0)
    missing = tuple(replace(r, answer=None) for r in responses())
    unavailable = extractor.extract((missing, tuple(replace(r, round=2) for r in missing)))
    assert not unavailable.values[:, :8].any()
    single = extractor.extract(((responses()[0],),))
    assert single.values.shape == (0, 12)
    assert HeuristicController().select(single, 1).number_of_edges() == 0


def test_heuristic_and_deterministic_selection():
    features = FeatureExtractor().extract((responses(),))
    graph = HeuristicController(top_k=3).select(features, 4)
    assert graph.number_of_edges() == 3
    assert all(i % 2 != j % 2 for i, j in graph.edges())
    graph = select_edges(features, np.ones(12), 4, top_k=2)
    assert sorted(graph.edges()) == [(0, 1), (0, 2)]


@pytest.mark.parametrize('text,expected', [('3 + 5 - 2 = 6. #### 6', '6'), ('#### $1,200.00', '1200'),
    ('Final answer: -2.50 apples', '-2.5'), ('#### 1/2', '0.5'), ('#### .5', '0.5'),
    ('#### -0', '0'), ('No answer', None), ('#### unknown', None)])
def test_extraction(text, expected):
    assert extract_answer(text) == expected


def test_vote_and_evaluation():
    assert majority_vote(['10', '2', None]) == '2'
    assert majority_vote([None]) is None
    assert majority_vote(['6', '7', '6', '7']) == '6'
    assert exact_match('1200', 'Work here. #### 1,200.0')
    assert not exact_match(None, 'no answer')


@pytest.mark.parametrize('topology,edges', [('fully_connected', 12), ('ring', 4), ('empty', 0), ('static_sparse', 1)])
def test_offline_execution(topology, edges, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    result = run_question(DEMO_QUESTION, MockLLM(), topology=topology, sparse_edges=((0, 1),))
    assert result['simulated'] and result['prediction'] == '6'
    assert result['trace'][1]['active_edges'] == edges
    assert result['total_tokens'] > 0
    assert result['latency'] >= 0
    with pytest.raises(ValueError, match='only the apple'):
        MockLLM().generate('unknown problem')


def test_api_configuration(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with pytest.raises(ValueError, match='OPENAI_API_KEY'):
        OpenAILLM('configured-model')


def test_partition_disjoint_and_stable():
    train = partition_indices(100, 'train', 42, .2)
    validation = partition_indices(100, 'validation', 42, .2)
    assert not set(train) & set(validation)
    assert set(train) | set(validation) == set(range(100))
    assert len(validation) == 20
    assert train == partition_indices(100, 'train', 42, .2)


def test_checkpoint_and_untrained_guard(tmp_path):
    torch.manual_seed(4)
    controller = MLPController(EdgeMLP((8,)))
    features = FeatureExtractor().extract((responses(),))
    assert controller.predict(features).shape == (12,)
    with pytest.raises(RuntimeError, match='validated'):
        controller.select(features, 4)
    path = tmp_path / 'model.pt'
    controller.save(path)
    restored = MLPController.load(path)
    np.testing.assert_array_equal(controller.predict(features), restored.predict(features))


def example(item_id, split, utility):
    return EdgeTrainingExample(item_id, split, (0, 1), tuple([utility] * 12), utility,
                               (), 0, 'synthetic-unit-test', 'snapshot', 'without', 'with')


def test_offline_label_interface():
    calls = []
    def rollout(edges):
        calls.append(edges)
        return bool(edges)
    row = collect_paired_example(item_id='q', split='train', edge=(0, 1), features=(0.,) * 12,
          context_edges=(), rollout=rollout, replicate=0, experiment_id='test', snapshot_id='s',
          without_run_id='a', with_run_id='b')
    assert row.utility == 1 and calls == [(), ((0, 1),)]


def test_training_pipeline_and_leakage_guard():
    # Synthetic fixtures verify code only; they are never research training data.
    rows = [example('a', 'train', 1), example('b', 'train', 0),
            example('c', 'validation', 1), example('d', 'validation', 0)]
    controller, report = train_controller(rows, hidden_dims=(4,), epochs=3)
    assert report['validated'] and np.isfinite(report['validation_bce'])
    assert controller.select(FeatureExtractor().extract((responses(),)), 4).number_of_nodes() == 4
    for bad_rows in (rows + [example('z', 'test', 0)], rows + [example('a', 'validation', 0)]):
        with pytest.raises(ValueError):
            train_controller(bad_rows, epochs=1)


def test_gsm8k_loader_with_local_fixture(monkeypatch):
    """Verify mapping/splits without claiming a successful remote dataset download."""
    import sys
    import types
    from src.benchmarks import GSM8K
    calls = []
    def load_dataset(name, config, split, revision):
        calls.append((name, config, split, revision))
        return [{'question': f'question {i}', 'answer': f'#### {i}'} for i in range(100)]
    monkeypatch.setitem(sys.modules, 'datasets', types.SimpleNamespace(load_dataset=load_dataset))
    benchmark = GSM8K(revision='pinned-test-revision')
    train = benchmark.load('train', 10, 42)
    validation = benchmark.load('validation', 10, 42)
    test = benchmark.load('test', 10, 42)
    assert not {x.item_id for x in train} & {x.item_id for x in validation}
    assert all(x.item_id.startswith('gsm8k:test:') for x in test)
    assert all(x[-1] == 'pinned-test-revision' for x in calls)
    assert benchmark.evaluate(extract_answer(train[0].reference), train[0].reference)


def test_openai_adapter_with_sdk_fixture(monkeypatch):
    import types
    import openai
    calls = []
    options = []
    def create(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(status='completed', output_text='#### 6',
                                     usage=types.SimpleNamespace(input_tokens=12, output_tokens=4))
    def client(**kwargs):
        options.append(kwargs)
        return types.SimpleNamespace(responses=types.SimpleNamespace(create=create))
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-not-a-real-key')
    monkeypatch.setattr(openai, 'OpenAI', client)
    backend = OpenAILLM('configured-model', max_output_tokens=64, max_retries=2)
    result = backend.generate('Question only')
    assert result.input_tokens == 12 and result.output_tokens == 4
    assert calls[0] == {'model': 'configured-model', 'input': 'Question only',
                        'store': False, 'max_output_tokens': 64}
    assert options[0]['max_retries'] == 2


def test_invalid_controller_graph_rejected():
    class BadController:
        def select(self, features, count):
            return make_topology('fully_connected', count + 1)
    with pytest.raises(ValueError, match='nodes'):
        run_question(DEMO_QUESTION, MockLLM(), topology='dynamic', controller=BadController())
