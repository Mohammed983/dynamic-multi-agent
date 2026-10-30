"""Immutable snapshots enforce round barriers even with sequential inference."""
from collections.abc import Mapping
import networkx as nx
from .agents import Response
from .topology import validate_graph


def route_messages(graph: nx.DiGraph, previous: Mapping[int, Response],
                   allow_self_loops: bool = False) -> dict[int, tuple[Response, ...]]:
    validate_graph(graph, len(previous), allow_self_loops)
    if set(previous) != set(graph):
        raise ValueError('Missing previous-round agent responses.')
    if len({r.round for r in previous.values()}) != 1:
        raise ValueError('Responses must come from the same completed round.')
    if any(key != response.agent_id for key, response in previous.items()):
        raise ValueError('Response identity mismatch.')
    return {j: tuple(previous[i] for i in sorted(graph.predecessors(j))) for j in graph}
