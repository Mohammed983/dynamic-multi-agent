"""Directed graph construction and validation. Edge i -> j delivers i to j."""
from collections.abc import Iterable
import networkx as nx


def validate_graph(graph: nx.DiGraph, count: int, allow_self_loops: bool = False) -> None:
    if not graph.is_directed() or graph.is_multigraph():
        raise ValueError('Expected a simple directed graph.')
    if set(graph.nodes) != set(range(count)):
        raise ValueError('Graph nodes must exactly match agent IDs 0..N-1.')
    if not allow_self_loops and nx.number_of_selfloops(graph):
        raise ValueError('Self communication is disabled.')


def make_topology(kind: str, count: int, edges: Iterable[tuple[int, int]] = (),
                  allow_self_loops: bool = False) -> nx.DiGraph:
    if count < 1:
        raise ValueError('At least one agent is required.')
    graph = nx.DiGraph()
    graph.add_nodes_from(range(count))
    if kind == 'fully_connected':
        graph.add_edges_from((i, j) for i in graph for j in graph if i != j or allow_self_loops)
    elif kind == 'ring':
        graph.add_edges_from((i, (i + 1) % count) for i in range(count)
                             if count > 1 or allow_self_loops)
    elif kind == 'static_sparse':
        graph.add_edges_from(edges)
    elif kind not in {'empty', 'dynamic'}:
        raise ValueError(f'Unknown topology: {kind}')
    validate_graph(graph, count, allow_self_loops)
    return graph
