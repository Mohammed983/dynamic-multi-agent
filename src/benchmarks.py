"""Benchmark-specific formatting/evaluation; references never enter runtime payloads."""
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Protocol
import numpy as np
from .evaluation import exact_match, option_exact_match
from .difficulty import select_indices, question_characteristics


@dataclass(frozen=True)
class BenchmarkItem:
    item_id: str
    question: str
    reference: str
    split: str
    provenance: dict = field(default_factory=dict)


class Benchmark(Protocol):
    answer_format: str
    def load(self, split: str, limit: int, seed: int) -> list[BenchmarkItem]: ...
    def evaluate(self, prediction: str | None, reference: str) -> bool: ...


class GSM8K:
    answer_format = 'numeric'

    def __init__(self, validation_fraction: float = 0.2, revision: str = 'main', difficulty: dict | None = None):
        if not 0 < validation_fraction < 1:
            raise ValueError('Validation fraction must lie between 0 and 1.')
        self.validation_fraction = validation_fraction
        self.revision = revision
        self.difficulty = difficulty

    def load(self, split: str, limit: int = 40, seed: int = 42) -> list[BenchmarkItem]:
        if split not in {'train', 'validation', 'test'} or limit < 1:
            raise ValueError('Use train/validation/test and a positive limit.')
        from datasets import load_dataset
        source = 'test' if split == 'test' else 'train'
        dataset = load_dataset('openai/gsm8k', 'main', split=source, revision=self.revision)
        ids = partition_indices(len(dataset), split, seed, self.validation_fraction)
        if self.difficulty:
            ids = select_indices([row['question'] for row in dataset], ids, limit, self.difficulty)
        return [BenchmarkItem(f'gsm8k:{source}:{i}', dataset[i]['question'], dataset[i]['answer'], split,
                {'dataset': 'openai/gsm8k', 'configuration': 'main', 'revision': self.revision,
                 'source_split': source, 'source_row': i, 'partition_seed': seed,
                 'validation_fraction': self.validation_fraction, 'difficulty_policy': self.difficulty,
                 'question_characteristics': question_characteristics(dataset[i]['question'])})
                for i in ids[:limit]]

    @staticmethod
    def evaluate(prediction: str | None, reference: str) -> bool:
        return exact_match(prediction, reference)


class AQUARAT:
    """Official DeepMind HF raw data; native validation corresponds to original dev.json.

    Use native train/validation/test without merging or resplitting. The generic
    parquet loader needs no remote dataset Python code or trust_remote_code flag.
    """
    answer_format = 'option'
    DEFAULT_REVISION = '33301c6a050c96af81f63cad5562cb5363e88971'

    def __init__(self, revision: str = DEFAULT_REVISION, difficulty: dict | None = None):
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ValueError('Pin AQUA-RAT to a full dataset commit hash.')
        self.revision, self.difficulty = revision, difficulty

    @staticmethod
    def format_question(question: str, options: list[str]) -> str:
        if not isinstance(question, str) or not question.strip() or not isinstance(options, list) or len(options) != 5:
            raise ValueError('AQUA-RAT requires a question and exactly five options.')
        for label, option in zip('ABCDE', options):
            if not isinstance(option, str) or not re.match(rf'^\s*{label}\s*[).:]\s*\S', option):
                raise ValueError('AQUA-RAT choices must be ordered, nonempty A-E options.')
        return question.strip() + '\n\nOptions:\n' + '\n'.join(option.strip() for option in options)

    def load(self, split: str, limit: int = 20, seed: int = 42) -> list[BenchmarkItem]:
        if split not in {'train', 'validation', 'test'} or type(limit) is not int or limit < 1:
            raise ValueError('Use native train/validation/test and a positive limit.')
        from datasets import load_dataset
        dataset = load_dataset('deepmind/aqua_rat', 'raw', split=split, revision=self.revision)
        candidates = np.random.default_rng(seed).permutation(len(dataset)).tolist()
        # Only question text is passed into the selection module, before labels/rationales are mapped.
        ids = select_indices([row['question'] for row in dataset], candidates, limit, self.difficulty)
        if len(ids) < limit:
            raise ValueError('Requested subset exceeds this native AQUA-RAT partition.')
        items = []
        for index in ids:
            row = dataset[index]
            question = self.format_question(row['question'], row['options'])
            if not isinstance(row['correct'], str):
                raise ValueError('Invalid AQUA-RAT reference label.')
            reference = row['correct'].strip().upper()
            if reference not in 'ABCDE' or len(reference) != 1:
                raise ValueError('Invalid AQUA-RAT reference label.')
            public_hash = hashlib.sha256(json.dumps({'question': row['question'], 'options': row['options']},
                                                    sort_keys=True).encode()).hexdigest()
            items.append(BenchmarkItem(f'aqua_rat:{self.revision}:{split}:{index}', question, reference, split,
                {'dataset': 'deepmind/aqua_rat', 'configuration': 'raw', 'revision': self.revision,
                 'source_split': split, 'source_row': index, 'selection_seed': seed,
                 'selection': 'seeded_random' if not self.difficulty else 'question_complexity',
                 'difficulty_policy': self.difficulty, 'public_problem_sha256': public_hash,
                 'question_characteristics': question_characteristics(row['question']),
                 'publisher_source': 'https://github.com/google-deepmind/AQuA'}))
        return items

    @staticmethod
    def evaluate(prediction: str | None, reference: str) -> bool:
        return option_exact_match(prediction, reference)


def partition_indices(size: int, split: str, seed: int, fraction: float) -> list[int]:
    """Stable original row IDs; train/validation partition before any debug subset."""
    ids = np.random.default_rng(seed).permutation(size).tolist()
    boundary = max(1, int(size * fraction))
    if split == 'validation':
        return ids[:boundary]
    if split == 'train':
        return ids[boundary:]
    if split == 'test':
        return ids
    raise ValueError('Unknown split.')
