"""Synchronous reasoning engine. Reference answers are deliberately absent from this API."""
from dataclasses import asdict
from time import perf_counter
from .agents import Agent
from .communication import route_messages
from .controllers.base import Controller
from .features import FEATURE_VERSION, FeatureExtractor
from .llm_client import LLMBackend
from .evaluation import vote_for_format
from .roles import ReasoningRole
from .topology import make_topology, validate_graph


def run_question(question: str, backend: LLMBackend, count: int = 4, rounds: int = 2,
                 topology: str = 'fully_connected', sparse_edges: tuple = (),
                 controller: Controller | None = None, allow_self_loops: bool = False, *,
                 roles: tuple[ReasoningRole, ...] | None = None, answer_format: str = 'numeric',
                 prompt_style: str = 'default') -> dict:
    """Round 1 independent; every later round reads only a completed snapshot.

    Inference is sequential, but communication uses a round barrier. Reported
    wall time is actual sequential execution, not hypothetical parallel latency.
    """
    if rounds < 1:
        raise ValueError('At least one reasoning round is required.')
    if answer_format not in {'numeric', 'option'}:
        raise ValueError('Answer format must be numeric or option.')
    if prompt_style not in {'default', 'concise_option_v2'} or (prompt_style != 'default' and answer_format != 'option'):
        raise ValueError('Invalid prompt style or incompatible answer format.')
    if roles is not None and (len(roles) != count or any(not isinstance(r, ReasoningRole) for r in roles)):
        raise ValueError('Provide exactly one valid role per agent.')
    if (topology == 'dynamic') != (controller is not None):
        raise ValueError('Dynamic topology requires a controller; static graphs cannot use one.')
    start = perf_counter()
    agents = [Agent(i, backend, role=roles[i] if roles else None, answer_format=answer_format,
                    prompt_style=prompt_style) for i in range(count)]
    graph = make_topology(topology, count, sparse_edges, allow_self_loops)
    history, records = [], []
    extractor = FeatureExtractor()
    previous = {}
    for round_number in range(1, rounds + 1):
        candidates = None
        if round_number == 1:
            inboxes = {i: () for i in range(count)}
            edges = []
        else:
            if controller is not None:
                candidates = extractor.extract(tuple(history), allow_self_loops, answer_format=answer_format)
                graph = controller.select(candidates, count)
                validate_graph(graph, count, allow_self_loops)
            inboxes = route_messages(graph, previous, allow_self_loops)
            edges = sorted(graph.edges())
        responses = tuple(agent.reason(question, round_number, inboxes[agent.agent_id]) for agent in agents)
        history.append(responses)
        previous = {r.agent_id: r for r in responses}
        records.append({'round': round_number, 'edges': edges, 'active_edges': len(edges),
                        'messages': {str(i): [asdict(m) for m in inboxes[i]] for i in inboxes},
                        'responses': [asdict(r) for r in responses],
                        'candidate_features': None if candidates is None else {
                            'edges': candidates.edges, 'names': list(extractor.names),
                            'feature_version': FEATURE_VERSION,
                            'values': candidates.values.tolist()}})
    all_responses = [r for row in history for r in row]
    input_tokens = sum(r.input_tokens for r in all_responses)
    output_tokens = sum(r.output_tokens for r in all_responses)
    result = {'question': question, 'model': backend.model, 'simulated': backend.simulated,
            'token_usage_kind': 'whitespace_estimate' if backend.simulated else 'provider_reported',
            'agents': count, 'rounds': rounds, 'topology': topology, 'trace': records,
            'final_answers': [r.answer for r in history[-1]],
            'prediction': vote_for_format((r.answer for r in history[-1]), answer_format),
            'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens, 'latency': perf_counter() - start,
            'agent_inference_latency': sum(r.latency for r in all_responses)}
    if roles is not None:
        result['agent_roles'] = [asdict(role) for role in roles]
    if answer_format != 'numeric':
        result['answer_format'] = answer_format
    if prompt_style != 'default':
        result['prompt_style'] = prompt_style
    return result
