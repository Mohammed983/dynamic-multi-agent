"""Controllers see candidate features and agent count, never references."""
from typing import Protocol
import networkx as nx
import numpy as np
from ..features import EdgeFeatures


class Controller(Protocol):
    def select(self, features: EdgeFeatures, count: int) -> nx.DiGraph: ...


def select_edges(features: EdgeFeatures, scores: np.ndarray, count: int,
                 threshold: float = 0.5, top_k: int | None = None) -> nx.DiGraph:
    """Global edge budget, deterministic lexicographic tie break."""
    scores = np.asarray(scores).reshape(-1)
    if len(scores) != len(features.edges) or not np.isfinite(scores).all():
        raise ValueError('Expected one finite score per candidate edge.')
    if not np.isfinite(threshold):
        raise ValueError('Selection threshold must be finite.')
    if top_k is not None and (type(top_k) is not int or top_k < 0):
        raise ValueError('top_k must be a nonnegative integer.')
    ranked = sorted(zip(features.edges, scores), key=lambda item: (-float(item[1]), item[0]))
    chosen = [edge for edge, score in ranked if score >= threshold]
    if top_k is not None:
        chosen = chosen[:top_k]
    graph = nx.DiGraph()
    graph.add_nodes_from(range(count))
    graph.add_edges_from(chosen)
    return graph
