"""Configurable useful-disagreement baseline, not a learned controller."""
from dataclasses import dataclass
import networkx as nx
from ..features import EdgeFeatures
from .base import select_edges


@dataclass
class HeuristicController:
    threshold: float = 0.35
    top_k: int | None = None
    disagreement_weight: float = 0.6
    consensus_weight: float = 0.2
    novelty_weight: float = 0.2

    def select(self, features: EdgeFeatures, count: int) -> nx.DiGraph:
        x = features.values
        scores = (self.disagreement_weight * (1 - x[:, 0]) * x[:, 1]
                  + self.consensus_weight * x[:, 2]
                  + self.novelty_weight * (1 - x[:, 11]))
        return select_edges(features, scores, count, self.threshold, self.top_k)
