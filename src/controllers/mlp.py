"""Edge usefulness model. Training and validation provenance is mandatory for runtime use."""
from pathlib import Path
import networkx as nx
import numpy as np
import torch
from torch import nn
from ..features import EdgeFeatures, FEATURE_NAMES, FEATURE_VERSION
from .base import select_edges


class EdgeMLP(nn.Module):
    def __init__(self, hidden_dims: tuple[int, ...] = (32, 16)):
        super().__init__()
        if any(d <= 0 for d in hidden_dims):
            raise ValueError('Hidden dimensions must be positive.')
        self.hidden_dims = tuple(hidden_dims)
        layers: list[nn.Module] = []
        previous = len(FEATURE_NAMES)
        for dimension in hidden_dims:
            layers.extend([nn.Linear(previous, dimension), nn.ReLU()])
            previous = dimension
        layers.append(nn.Linear(previous, 1))
        self.network = nn.Sequential(*layers)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        return self.network(values).squeeze(-1)


class MLPController:
    def __init__(self, model: EdgeMLP, threshold: float = 0.5, top_k: int | None = None,
                 provenance: dict | None = None):
        self.model, self.threshold, self.top_k = model.cpu().eval(), threshold, top_k
        self.provenance = provenance or {}

    def predict(self, features: EdgeFeatures) -> np.ndarray:
        with torch.inference_mode():
            return torch.sigmoid(self.model(torch.from_numpy(features.values))).numpy()

    def select(self, features: EdgeFeatures, count: int) -> nx.DiGraph:
        if not self.provenance.get('validated'):
            raise RuntimeError('MLP runtime selection requires a trained, validated checkpoint.')
        return select_edges(features, self.predict(features), count, self.threshold, self.top_k)

    def save(self, path: str | Path) -> None:
        torch.save({'state_dict': self.model.state_dict(), 'hidden_dims': self.model.hidden_dims,
                    'feature_names': list(FEATURE_NAMES), 'feature_version': FEATURE_VERSION,
                    'provenance': self.provenance}, path)

    @classmethod
    def load(cls, path: str | Path, threshold: float = 0.5, top_k: int | None = None) -> 'MLPController':
        checkpoint = torch.load(path, map_location='cpu', weights_only=True)
        if checkpoint['feature_names'] != list(FEATURE_NAMES) or checkpoint['feature_version'] != FEATURE_VERSION:
            raise ValueError('Checkpoint feature schema mismatch.')
        model = EdgeMLP(tuple(checkpoint['hidden_dims']))
        model.load_state_dict(checkpoint['state_dict'])
        return cls(model, threshold, top_k, checkpoint['provenance'])
