"""Reference-bearing offline evaluation, isolated from agent/controller payloads."""
from typing import Callable

from .evaluation import vote_for_format


def question_diagnostics(answers: list[str | None], reference: str, answer_format: str,
                         evaluate: Callable[[str | None, str], bool],
                         returned: list[bool] | None = None) -> dict:
    """All four planned slots remain present, including missing/unparseable outputs."""
    if len(answers) != 4 or (returned is not None and len(returned) != 4):
        raise ValueError('The independent diagnostic requires exactly four agent slots.')
    returned = returned if returned is not None else [True] * 4
    if any(answer is not None and not ok for answer, ok in zip(answers, returned)):
        raise ValueError('An unavailable response cannot have a parsed answer.')
    correctness = [evaluate(answer, reference) if ok else None for answer, ok in zip(answers, returned)]
    valid = [answer for answer in answers if answer is not None]
    all_parsed = len(valid) == 4
    unanimous = all_parsed and len(set(valid)) == 1
    disagreement = all_parsed and len(set(valid)) > 1
    prediction = vote_for_format(answers, answer_format) if all(returned) else None
    pairs = [{'source': i, 'target': j,
              'agree': answers[i] == answers[j] if answers[i] is not None and answers[j] is not None else None}
             for i in range(4) for j in range(i + 1, 4)]
    return {'answers': answers, 'returned': returned, 'agent_correctness': correctness,
            'majority_prediction': prediction, 'majority_correct': evaluate(prediction, reference) if all(returned) else None,
            'all_answers_parsed': all_parsed, 'unanimous': unanimous, 'disagreement': disagreement,
            'missing_or_unparseable': not all_parsed, 'observed_disagreement': len(set(valid)) > 1,
            'pairwise_agreement': pairs, 'incorrect_agent_ids': [i for i, c in enumerate(correctness) if c is False],
            'invalid_answer_agent_ids': [i for i, (a, ok) in enumerate(zip(answers, returned)) if ok and a is None],
            'unavailable_agent_ids': [i for i, ok in enumerate(returned) if not ok],
            'at_least_one_incorrect': any(c is False for c in correctness)}


def summarize_diagnostics(records: list[dict], planned_questions: int = 20) -> dict:
    if planned_questions < 1 or len(records) > planned_questions:
        raise ValueError('Invalid diagnostic denominator.')
    evaluated = [r['diagnostics'] for r in records if r.get('diagnostics') is not None]
    agents = []
    for i in range(4):
        correct = sum(d['agent_correctness'][i] is True for d in evaluated)
        returned = sum(d['returned'][i] for d in evaluated)
        agents.append({'agent_id': i, 'correct': correct, 'planned': planned_questions,
                       'returned': returned, 'planned_accuracy': correct / planned_questions if returned else None,
                       'returned_accuracy': correct / returned if returned else None})
    unanimous = sum(d['unanimous'] for d in evaluated)
    disagreement = sum(d['disagreement'] for d in evaluated)
    majority_correct = sum(d['majority_correct'] is True for d in evaluated)
    complete = sum(all(d['returned']) for d in evaluated)
    return {'planned_questions': planned_questions, 'recorded_questions': len(records),
            'complete_four_response_questions': complete,
            'four_parsed_answer_questions': sum(d['all_answers_parsed'] for d in evaluated),
            'individual_agents': agents, 'majority_correct': majority_correct,
            'majority_accuracy_planned': majority_correct / planned_questions if complete else None,
            'unanimous_count': unanimous, 'unanimous_rate_planned': unanimous / planned_questions,
            'disagreement_count': disagreement, 'disagreement_rate_planned': disagreement / planned_questions,
            'missing_or_unparseable_count': planned_questions - unanimous - disagreement,
            'items_with_observed_incorrect_agent': [r['item_id'] for r in records
                if r.get('diagnostics') and r['diagnostics']['at_least_one_incorrect']],
            'notes': 'Planned denominators retain failures/unavailable slots; returned accuracy is also exposed. '
                     'Unanimity/disagreement require four parsed answers; missing answers form a third category.'}
