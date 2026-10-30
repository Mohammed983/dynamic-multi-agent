"""Versioned question-text-only complexity proxy; never consumes labels or outcomes."""
import re
from typing import Mapping, Sequence

VERSION = 1
RELATIONS = ('percent', 'ratio', 'average', 'remaining', 'remainder', 'twice', 'thrice',
             'probability', 'fraction', 'per', 'each', 'difference', 'total', 'more', 'less')


def question_characteristics(question: str) -> dict[str, int]:
    words = re.findall(r'\b\w+\b', question.lower())
    numbers = re.findall(r'(?<!\w)[-+]?\d[\d,]*(?:\.\d+)?(?:/\d+)?', question)
    relation_count = sum(words.count(term) for term in RELATIONS)
    return {'word_count': len(words), 'numeric_quantity_count': len(numbers),
            'relation_term_count': relation_count,
            'score': len(words) + 8 * len(numbers) + 6 * relation_count}


def select_indices(texts: Sequence[str], candidates: Sequence[int], limit: int,
                   policy: Mapping | None = None) -> list[int]:
    """Candidates already have seeded order/partition membership; labels cannot enter."""
    if not policy or policy.get('mode', 'none') == 'none':
        return list(candidates[:limit])
    if (policy.get('mode') != 'question_complexity' or policy.get('version', VERSION) != VERSION
            or any(key not in {'mode', 'version', 'min_words', 'min_numbers', 'min_relations'} for key in policy)):
        raise ValueError('Unsupported question-only difficulty policy/schema.')
    bounds = {key: policy.get(key, 0) for key in ('min_words', 'min_numbers', 'min_relations')}
    if any(type(value) is not int or value < 0 for value in bounds.values()):
        raise ValueError('Difficulty bounds must be nonnegative integers.')
    eligible = []
    for index in candidates:
        values = question_characteristics(texts[index])
        if (values['word_count'] >= bounds['min_words']
                and values['numeric_quantity_count'] >= bounds['min_numbers']
                and values['relation_term_count'] >= bounds['min_relations']):
            eligible.append((index, values['score']))
    # Stable sort retains seeded candidate order for ties; never use a reference or model result.
    eligible.sort(key=lambda pair: -pair[1])
    if len(eligible) < limit:
        raise ValueError(f'Only {len(eligible)} eligible questions for a requested {limit}; do not silently replace them.')
    return [index for index, _ in eligible[:limit]]
