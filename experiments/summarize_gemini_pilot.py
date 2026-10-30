"""Validate real pilot artifacts and summarize them without issuing any API call."""
from datetime import datetime
import json
from pathlib import Path

from dotenv import dotenv_values

from experiments import gemini_pilot as pilot
from experiments.resume_gemini_pilot import latest_outcomes
from src.evaluation import exact_match, majority_vote


def main() -> None:
    state = json.loads(pilot.STATE.read_text(encoding='utf-8'))
    if state['status'] != 'completed':
        raise ValueError('Final validation requires all planned questions to have completed.')
    rows = [json.loads(line) for line in pilot.OUTCOMES.read_text(encoding='utf-8').splitlines()]
    records = latest_outcomes(rows)
    events = pilot.read_events()
    successes = [e for e in events if e['event'] == 'call_completed']
    failures = [e for e in events if e['event'] != 'call_completed']
    assert len(records) == 20 and len(successes) == 240
    assert len(failures) == 1 and failures[0]['http_status'] == 504
    indexed = {(e['item_id'], e['topology'], e['round'], e['agent_id']): e for e in successes}
    assert len(indexed) == 240
    questions, references, _ = pilot.frozen_items()
    question_map = {q['item_id']: q['question'] for q in questions}
    summary = {'status': 'completed', 'model': 'gemini-3.5-flash-lite', 'topologies': {},
               'attempts': len(events), 'successful_responses': 240, 'failed_attempts': 1,
               'unknown_usage_attempts': 1, 'agent_answer_transitions': 0, 'agent_answer_changes': [],
               'visible_text_changes': 0, 'prior_artifacts_preserved': all(state['preservation_check'].values()),
               'quota_remaining_after_own_calls_estimate': 499 - len(events),
               'quota_estimate_caveat': 'Assumes no other project traffic; not a provider dashboard reading.'}
    for record in records:
        assert record['status'] == 'completed' and record['simulated'] is False
        assert record['question'] == question_map[record['item_id']]
        assert record['ground_truth'] == references[record['item_id']]
        # The runner received the actual topology as a direct argument. Normalize
        # the copied profile's nested metadata in the canonical export only; retain
        # all original raw JSONL snapshots for auditing.
        record['config'] = {**record['config'], 'topology': record['topology']}
        record['metadata_note'] = 'Canonical nested config topology normalized to the actual top-level graph; raw snapshots retained.'
        expected_edges = 12 if record['topology'] == 'fully_connected' else 4
        assert [row['active_edges'] for row in record['trace']] == [0, expected_edges, expected_edges]
        assert len(record['trace']) == 3 and all(len(row['responses']) == 4 for row in record['trace'])
        for row in record['trace']:
            edges = {tuple(edge) for edge in row['edges']}
            assert all(a != b for a, b in edges)
            for response in row['responses']:
                event = indexed[(record['item_id'], record['topology'], row['round'], response['agent_id'])]
                assert response['text'] == event['output_text']
                assert response['input_tokens'] == event['usage']['prompt_token_count']
                assert response['output_tokens'] == (event['usage']['candidates_token_count']
                                                      + event['usage']['thoughts_token_count'])
                assert row['messages'][str(response['agent_id'])] == event['messages_received']
                assert {tuple(edge) for edge in event['graph_edges']} == edges
                assert all(message['round'] == row['round'] - 1 and message['agent_id'] != response['agent_id']
                           for message in row['messages'][str(response['agent_id'])])
        for previous, current in zip(record['trace'], record['trace'][1:]):
            for old, new in zip(previous['responses'], current['responses']):
                summary['agent_answer_transitions'] += 1
                summary['visible_text_changes'] += old['text'] != new['text']
                if old['answer'] != new['answer']:
                    summary['agent_answer_changes'].append({'item_id': record['item_id'],
                        'topology': record['topology'], 'agent_id': new['agent_id'],
                        'round': current['round'], 'before': old['answer'], 'after': new['answer']})
    for topology in ('fully_connected', 'ring'):
        group = [r for r in records if r['topology'] == topology]
        calls = [e for e in successes if e['topology'] == topology]
        failed = [e for e in failures if e['topology'] == topology]
        assert len(group) == 10 and {r['item_id'] for r in group} == set(question_map)
        assert len(calls) == 120
        input_tokens = sum(e['usage']['prompt_token_count'] for e in calls)
        output_tokens = sum(e['usage']['candidates_token_count'] + e['usage']['thoughts_token_count'] for e in calls)
        assert input_tokens + output_tokens == sum(e['usage']['total_token_count'] for e in calls)
        assert input_tokens == sum(r['input_tokens'] for r in group)
        assert output_tokens == sum(r['output_tokens'] for r in group)
        summary['topologies'][topology] = {'completed_questions': 10, 'correct_questions': sum(r['correct'] for r in group),
            'initial_majority_correct_questions': sum(exact_match(
                majority_vote(a['answer'] for a in r['trace'][0]['responses']), r['ground_truth']) for r in group),
            'accuracy': sum(r['correct'] for r in group) / 10, 'successful_input_tokens': input_tokens,
            'successful_output_tokens': output_tokens,
            'thinking_tokens': sum(e['usage']['thoughts_token_count'] for e in calls),
            'successful_inference_seconds': sum(e['inference_latency'] for e in calls),
            'failed_request_seconds': sum(e.get('inference_latency') or 0 for e in failed),
            'all_attempt_usage_complete': not failed,
            'mean_successful_inference_seconds_per_question': sum(r['agent_inference_latency'] for r in group) / 10,
            'mean_active_wall_seconds_per_question': sum(r['question_wall_seconds'] for r in group) / 10,
            'message_deliveries': sum(row['active_edges'] for r in group for row in r['trace'])}
    starts = [datetime.fromisoformat(e['timestamp']).timestamp() for e in events]
    summary['peak_requests_in_rolling_minute'] = max(sum(0 <= b-a < 60 for b in starts) for a in starts)
    summary['peak_known_input_tokens_in_rolling_minute'] = max(sum(
        e['usage']['prompt_token_count'] for e in successes
        if 0 <= datetime.fromisoformat(e['timestamp']).timestamp() - a < 60) for a in starts)
    assert summary['peak_requests_in_rolling_minute'] <= 12
    assert summary['peak_known_input_tokens_in_rolling_minute'] <= 200000
    assert summary['prior_artifacts_preserved']
    final = pilot.RESULTS / 'gemini_10_question_pilot_final.jsonl'
    final.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
    (pilot.RESULTS / 'gemini_10_question_pilot_summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    # Rebuild before inserting the final summary so rerunning this read-only command
    # is idempotent and never averages historical outcome revisions.
    if state.get('recovery'):
        from experiments.resume_gemini_pilot import recovery_report
        recovery_report(state, records, events)
    else:
        pilot.render_report(state, records, events)
    lines = ['## Final validated comparison', '',
        'All 20 question/topology runs completed. The table below isolates the 240 successful '
        'responses so the one recovered gateway timeout is not mistaken for a topology effect.', '',
        '| Topology | Accuracy | Successful input tokens | Successful output tokens | Successful inference (s) | '
        'Mean successful inference / question (s) | Mean active wall / question (s) | Directed message deliveries |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name, metrics in summary['topologies'].items():
        lines.append(f"| {name} | {metrics['correct_questions']}/10 ({metrics['accuracy']:.0%}) | "
            f"{metrics['successful_input_tokens']} | {metrics['successful_output_tokens']} | "
            f"{metrics['successful_inference_seconds']:.3f} | {metrics['mean_successful_inference_seconds_per_question']:.3f} | "
            f"{metrics['mean_active_wall_seconds_per_question']:.3f} | {metrics['message_deliveries']} |")
    lines += ['', f"There were {summary['attempts']} attempts: 240 successful responses and one HTTP 504 "
        f"taking {summary['topologies']['fully_connected']['failed_request_seconds']:.3f}s. Its token usage is unknown. "
        'Thus an exact all-attempt input/output total cannot be stated. Successful-response totals above '
        'are exact provider-reported counts; the fully connected all-attempt totals additionally contain '
        'that unknown usage. Wall times include deliberate rate pacing and the failed timeout, but exclude '
        'the manual recovery pause. The detailed table below includes the failed request latency.', '',
        f"Across {summary['agent_answer_transitions']} agent-answer transitions, "
        f"{len(summary['agent_answer_changes'])} numerical answers changed; visible solution text changed "
        f"in {summary['visible_text_changes']} transitions. No numerical correction example is invented. "
        'The individual actual responses and inboxes are retained below and in JSONL.', '',
        f"Initial majority votes were correct on {summary['topologies']['fully_connected']['initial_majority_correct_questions']}/10 "
        f"fully connected runs and {summary['topologies']['ring']['initial_majority_correct_questions']}/10 ring runs. "
        'Compare these initial votes with the final accuracy above; when both are at the ceiling, '
        'there is no observed accuracy gain from communication. Changes in explanation wording '
        'are not evidence of better reasoning.', '',
        'The canonical final JSONL normalizes the copied profile\'s nested topology field to the actual '
        'top-level topology used by the runner. The raw snapshots are unchanged; this is a metadata '
        'correction, not a graph or inference change.', '',
        f"Observed peak dispatch rate: {summary['peak_requests_in_rolling_minute']} per rolling minute. "
        f"Peak known input usage: {summary['peak_known_input_tokens_in_rolling_minute']} tokens per rolling minute "
        '(one failed request remains unmetered; the conservative byte-based limiter applied to every attempt). '
        'All earlier artifact checksums match.', '',
        'This is a small validation-set pilot, not proof of superiority. Equal accuracy on ten '
        'questions does not establish equivalence; fewer ring edges do not establish a general '
        'accuracy/cost tradeoff. No MLP was trained.', '']
    body = pilot.REPORT.read_text(encoding='utf-8')
    # Report is owned by this experiment; all earlier experiment reports remain untouched.
    location = body.index('\n## Protocol and account checks')
    body = body[:location] + '\n\n' + '\n'.join(lines) + body[location:]
    pilot.REPORT.write_text(body, encoding='utf-8')
    secrets = [v.strip() for k, v in dotenv_values(pilot.ROOT / '.env').items()
               if k.endswith('API_KEY') and v and v.strip()]
    files = [p for p in pilot.RESULTS.glob('gemini_10_question_pilot*') if p.is_file()]
    assert all(secret not in p.read_text(encoding='utf-8') for secret in secrets for p in files)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
