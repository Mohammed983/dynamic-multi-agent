"""GSM8K numerical extraction and deterministic voting (not code evaluation)."""
from collections import Counter
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable

NUMBER = r"[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:/\d+)?"


def normalize_number(value: str) -> str | None:
    """Canonicalize decimals and exact terminating fractions without floats."""
    try:
        parts = value.replace(',', '').strip().split('/')
        number = Decimal(parts[0])
        if len(parts) == 2:
            number /= Decimal(parts[1])
        if not number.is_finite():
            return None
        # Decimal.normalize() applies the active precision (usually 28 digits)
        # and can silently merge distinct, large exact answers.
        rendered = format(number, 'f')
        if '.' in rendered:
            rendered = rendered.rstrip('0').rstrip('.')
        return '0' if number == 0 else rendered
    except (InvalidOperation, ZeroDivisionError):
        return None


def extract_answer(text: str) -> str | None:
    """Prefer final #### marker, then Final answer:, else last numerical token."""
    if '####' in text:
        segment = text.rsplit('####', 1)[1]
        numbers = re.findall(NUMBER, segment)
        return normalize_number(numbers[0]) if numbers else None
    marker = re.search(r'final answer\s*:', text, re.I)
    segment = text[marker.end():] if marker else text
    numbers = re.findall(NUMBER, segment)
    if not numbers:
        return None
    return normalize_number(numbers[0] if marker else numbers[-1])


def majority_vote(answers: Iterable[str | None]) -> str | None:
    """Abstain on missing answers; tied answers use numerical ascending order."""
    counts = Counter(a for a in answers if a is not None)
    return min(counts, key=lambda a: (-counts[a], Decimal(a))) if counts else None


def exact_match(prediction: str | None, reference: str) -> bool:
    expected = extract_answer(reference)
    return prediction is not None and expected is not None and prediction == expected


def extract_option_answer(text: str) -> str | None:
    """Parse an explicit final A-E choice; never guess from numbers or prose letters."""
    if '####' in text:
        segment = text.rsplit('####', 1)[1]
    else:
        markers = list(re.finditer(r'(?:final\s+answer\s*:|answer\s*(?:is|:))\s*', text, re.I))
        segment = text[markers[-1].end():] if markers else text
    match = re.fullmatch(r'\s*(?:option\s+)?\(?([A-E])\)?[.)]?\s*', segment, re.I)
    return match.group(1).upper() if match else None


def majority_vote_options(answers: Iterable[str | None]) -> str | None:
    """Abstain on missing/invalid options; break ties alphabetically A before B."""
    counts = Counter(answer for answer in answers if answer in {'A', 'B', 'C', 'D', 'E'})
    return min(counts, key=lambda answer: (-counts[answer], answer)) if counts else None


def option_exact_match(prediction: str | None, reference: str) -> bool:
    expected = extract_option_answer(reference)
    return prediction in {'A', 'B', 'C', 'D', 'E'} and expected is not None and prediction == expected


def extract_for_format(text: str, answer_format: str = 'numeric', *, prompt_style: str = 'default') -> str | None:
    if prompt_style == 'concise_option_v2':
        if answer_format != 'option':
            raise ValueError('Concise option prompts require option evaluation.')
        # Require the explicit final line, not letters or quantities inside reasoning.
        match = re.fullmatch(r'Final answer:\s*([A-E])', text.strip().splitlines()[-1] if text.strip() else '')
        return match.group(1) if match else None
    if prompt_style != 'default':
        raise ValueError('Unknown prompt style.')
    if answer_format == 'numeric':
        return extract_answer(text)
    if answer_format == 'option':
        return extract_option_answer(text)
    raise ValueError('Answer format must be numeric or option.')


def vote_for_format(answers: Iterable[str | None], answer_format: str = 'numeric') -> str | None:
    if answer_format == 'numeric':
        return majority_vote(answers)
    if answer_format == 'option':
        return majority_vote_options(answers)
    raise ValueError('Answer format must be numeric or option.')
