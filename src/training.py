"""Offline paired-intervention labels and supervised fitting, never runtime references."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Callable
import numpy as np
import torch
from .features import FEATURE_NAMES, FEATURE_VERSION
from .controllers.mlp import EdgeMLP, MLPController


@dataclass(frozen=True)
class EdgeTrainingExample:
    item_id: str
    split: str
    edge: tuple[int, int]
    features: tuple[float, ...]
    utility: float
    context_edges: tuple[tuple[int, int], ...]
    replicate: int
    # Identify the frozen reasoning snapshot, paired rollouts and experiment config.
    experiment_id: str
    snapshot_id: str
    without_run_id: str
    with_run_id: str
    feature_version: int = FEATURE_VERSION


def _validate_example(example: EdgeTrainingExample) -> None:
    """Check offline records before any rollout, persistence, or optimization."""
    if example.split not in {'train', 'validation'}:
        raise ValueError('Test examples cannot enter offline label collection or training.')
    values = np.asarray(example.features, dtype=np.float64)
    if (values.shape != (len(FEATURE_NAMES),) or not np.isfinite(values).all()
            or np.any(np.abs(values) > np.finfo(np.float32).max)
            or example.feature_version != FEATURE_VERSION or example.utility not in {-1, 0, 1}):
        raise ValueError('Invalid feature schema or paired utility.')
    identifiers = (example.item_id, example.experiment_id, example.snapshot_id,
                   example.without_run_id, example.with_run_id)
    if not all(isinstance(value, str) and value.strip() for value in identifiers):
        raise ValueError('Nonempty item, experiment, snapshot and rollout IDs are required.')
    if example.without_run_id == example.with_run_id:
        raise ValueError('Paired interventions must have distinct rollout IDs.')
    if type(example.replicate) is not int or example.replicate < 0:
        raise ValueError('Replicate must be a nonnegative integer.')
    pairs = (example.edge, *example.context_edges)
    if any(len(pair) != 2 or any(type(node) is not int or node < 0 for node in pair) for pair in pairs):
        raise ValueError('Edges must contain two nonnegative integer agent IDs.')
    edge = tuple(example.edge)
    context = tuple(tuple(pair) for pair in example.context_edges)
    if len(set(context)) != len(context) or edge in context:
        raise ValueError('Context edges must be unique and exclude the intervention edge.')


def collect_paired_example(*, item_id: str, split: str, edge: tuple[int, int],
                           features: tuple[float, ...], context_edges: tuple[tuple[int, int], ...],
                           rollout: Callable[[tuple[tuple[int, int], ...]], bool],
                           replicate: int, experiment_id: str, snapshot_id: str,
                           without_run_id: str, with_run_id: str) -> EdgeTrainingExample:
    """Rollout closure restores one frozen snapshot and evaluates against offline truth.

    Callback runs the agreed horizon and returns correctness. The caller must save
    rollout traces under the provided run IDs. Never connect this to test examples.
    """
    draft = EdgeTrainingExample(item_id, split, edge, features, 0., context_edges, replicate,
                                experiment_id, snapshot_id, without_run_id, with_run_id)
    _validate_example(draft)
    context = tuple(tuple(pair) for pair in context_edges)
    candidate = tuple(edge)

    def checked_rollout(edges: tuple[tuple[int, int], ...]) -> bool:
        outcome = rollout(edges)
        if not isinstance(outcome, (bool, np.bool_)):
            raise ValueError('Rollout must return boolean correctness, not a truthy object.')
        return bool(outcome)

    without = checked_rollout(context)
    with_edge = checked_rollout(tuple(sorted((*context, candidate))))
    return EdgeTrainingExample(item_id, split, candidate, tuple(features), float(with_edge) - float(without),
                               context, replicate, experiment_id, snapshot_id,
                               without_run_id, with_run_id)


def append_example(path: str | Path, example: EdgeTrainingExample) -> None:
    _validate_example(example)
    with Path(path).open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(asdict(example)) + '\n')


def train_controller(examples: list[EdgeTrainingExample], hidden_dims: tuple[int, ...] = (32, 16),
                     epochs: int = 100, learning_rate: float = 0.001,
                     seed: int = 42) -> tuple[MLPController, dict]:
    """Binary target: did adding this edge improve correctness? Neutral/harmful=0.

    Each paired replicate is a sample; validation is grouped by question, and
    selects the lowest validation BCE epoch. Validation alone is not evidence
    of benchmark improvement; assess frozen checkpoints on held-out test later.
    """
    if type(epochs) is not int or epochs < 1 or not np.isfinite(learning_rate) or learning_rate <= 0:
        raise ValueError('Positive epochs and learning rate required.')
    for e in examples:
        _validate_example(e)
    train = [e for e in examples if e.split == 'train']
    validation = [e for e in examples if e.split == 'validation']
    if not train or not validation:
        raise ValueError('Real train and validation examples are both required.')
    if {e.item_id for e in train} & {e.item_id for e in validation}:
        raise ValueError('Question leakage between train and validation.')
    if len({e.utility > 0 for e in train}) < 2:
        raise ValueError('Training requires positive and nonpositive utility samples.')
    torch.manual_seed(seed)
    model = EdgeMLP(hidden_dims)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    loss_fn = torch.nn.BCEWithLogitsLoss()

    def tensors(rows):
        return (torch.tensor([e.features for e in rows], dtype=torch.float32),
                torch.tensor([float(e.utility > 0) for e in rows]))

    x, y = tensors(train)
    vx, vy = tensors(validation)
    best_loss, best_state, best_epoch = float('inf'), None, 0
    for epoch in range(epochs):
        model.train()
        optimizer.zero_grad()
        loss = loss_fn(model(x), y)
        if not torch.isfinite(loss):
            raise RuntimeError('Non-finite training loss; no validated checkpoint produced.')
        loss.backward()
        optimizer.step()
        model.eval()
        with torch.no_grad():
            val_loss = float(loss_fn(model(vx), vy))
        if not np.isfinite(val_loss):
            raise RuntimeError('Non-finite validation loss; no validated checkpoint produced.')
        if val_loss < best_loss:
            best_loss, best_epoch = val_loss, epoch + 1
            best_state = {key: value.clone() for key, value in model.state_dict().items()}
    model.load_state_dict(best_state)
    report = {'validated': True, 'seed': seed, 'epochs': epochs, 'best_epoch': best_epoch,
              'validation_bce': best_loss, 'train_samples': len(train),
              'validation_samples': len(validation), 'target': 'positive_paired_utility',
              'train_item_ids': sorted({e.item_id for e in train}),
              'validation_item_ids': sorted({e.item_id for e in validation})}
    return MLPController(model, provenance=report), report
