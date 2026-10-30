"""Ground-truth-free candidate edge features, versioned for saved models."""
from dataclasses import dataclass
import re
import numpy as np
from .agents import Response
from .evaluation import vote_for_format

FEATURE_NAMES = ('pair_agreement', 'pair_answers_available', 'source_consensus',
                 'target_consensus', 'consensus_available', 'source_support_delta',
                 'target_support_delta', 'support_delta_available', 'source_temporal_similarity',
                 'target_temporal_similarity', 'history_available', 'pair_similarity')
FEATURE_VERSION = 1


def similarity(left: str, right: str) -> float:
    """Token-set Jaccard, a cheap lexical redundancy signal."""
    a, b = set(re.findall(r'\w+', left.lower())), set(re.findall(r'\w+', right.lower()))
    return len(a & b) / len(a | b) if a | b else 0.0


@dataclass(frozen=True)
class EdgeFeatures:
    edges: tuple[tuple[int, int], ...]
    values: np.ndarray


class FeatureExtractor:
    names = FEATURE_NAMES

    def extract(self, history: tuple[tuple[Response, ...], ...],
                allow_self_loops: bool = False, *, answer_format: str = 'numeric') -> EdgeFeatures:
        if not history or not history[-1]:
            raise ValueError('Completed response history is required.')
        for index, row in enumerate(history):
            ids = [response.agent_id for response in row]
            if (not row or len(ids) != len(set(ids)) or set(ids) != set(range(len(row)))
                    or any(response.round != index + 1 for response in row)
                    or len(row) != len(history[-1])):
                raise ValueError('History must contain complete, consecutive rounds with unique agent IDs.')
        current = {r.agent_id: r for r in history[-1]}
        old = {r.agent_id: r for r in history[-2]} if len(history) > 1 else {}
        consensus = vote_for_format((r.answer for r in current.values()), answer_format)

        def support(rows: dict[int, Response], answer: str | None) -> float:
            return sum(r.answer == answer for r in rows.values()) / len(rows) if answer is not None else 0.0

        edges, values = [], []
        for i in sorted(current):
            for j in sorted(current):
                if i == j and not allow_self_loops:
                    continue
                a, b = current[i], current[j]
                pair_known = a.answer is not None and b.answer is not None
                prior = i in old and j in old
                delta_known = prior and all(r.answer is not None for r in (a, b, old[i], old[j]))
                deltas = [support(current, current[k].answer) - support(old, old[k].answer)
                          if delta_known else 0.0 for k in (i, j)]
                temporal = [similarity(current[k].text, old[k].text) if prior else 0.0 for k in (i, j)]
                edges.append((i, j))
                values.append([float(pair_known and a.answer == b.answer), float(pair_known),
                               float(a.answer is not None and a.answer == consensus),
                               float(b.answer is not None and b.answer == consensus),
                               float(consensus is not None), *deltas, float(delta_known),
                               *temporal, float(prior), similarity(a.text, b.text)])
        return EdgeFeatures(tuple(edges), np.asarray(values, dtype=np.float32).reshape(-1, len(self.names)))
