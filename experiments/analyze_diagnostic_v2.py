"""Offline, append-only publication of AQUA v2 metrics. No inference SDK imports.

Recompute answers from recorded text, check stored evaluations, reconcile journal
events and compare with the canonical GSM8K pilot. Existing outputs are refused.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from src.evaluation import extract_for_format, majority_vote_options, option_exact_match, majority_vote, exact_match

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def usage_metrics(events: list[dict]) -> dict:
    usage = [e['usage'] for e in events if e.get('usage') is not None]
    inputs = sum(u['prompt_token_count'] for u in usage)
    visible = sum(u['candidates_token_count'] for u in usage)
    thinking = sum(u.get('thoughts_token_count', 0) for u in usage)
    totals = sum(u['total_token_count'] for u in usage)
    assert inputs + visible + thinking == totals, 'Unexpected provider usage breakdown.'
    return {'attempts': len(events), 'metered_attempts': len(usage), 'input_tokens': inputs,
            'visible_output_tokens': visible, 'thinking_tokens': thinking,
            'output_tokens_including_thinking': visible + thinking, 'total_tokens': totals,
            'unknown_usage_attempts': sum(bool(e.get('usage_unknown')) for e in events),
            'known_request_latency_seconds': sum(e['inference_latency'] for e in events if e.get('inference_latency') is not None),
            'missing_latency_attempts': sum(e.get('inference_latency') is None for e in events)}


def analyze() -> tuple[dict, dict[str, str]]:
    prefix = ROOT / 'results/independent_diagnostic_aqua_v2'
    paths = {key: prefix.with_name(prefix.name + suffix) for key, suffix in {
        'records':'_results.jsonl', 'calls':'_calls.jsonl', 'plan':'_plan.json', 'state':'_state.json',
        'questions':'_questions.jsonl', 'references':'_references.jsonl', 'summary':'_summary.json'}.items()}
    paths.update(gsm_summary=ROOT/'results/gemini_10_question_pilot_summary.json',
                 gsm_records=ROOT/'results/gemini_10_question_pilot_final.jsonl')
    source_hashes = {str(p.relative_to(ROOT)): digest(p) for p in paths.values()}
    plan = json.loads(paths['plan'].read_text(encoding='utf-8'))
    state = json.loads(paths['state'].read_text(encoding='utf-8'))
    summary = json.loads(paths['summary'].read_text(encoding='utf-8'))
    assert digest(paths['questions']) == plan['question_file_sha256']
    assert digest(paths['references']) == plan['reference_file_sha256']
    rows = read_rows(paths['records'])
    refs = {r['item_id']: r['reference'] for r in read_rows(paths['references'])}
    assert len(rows) == len({r['item_id'] for r in rows}) == 20
    events = read_rows(paths['calls'])
    starts = {e['call_id']: e for e in events if e['event'] == 'request_started'}
    terminals = [e for e in events if e['event'] != 'request_started']
    assert len(starts) == len(terminals) == len({e['call_id'] for e in terminals})
    assert len(events) == 2 * len(terminals) and set(starts) == {e['call_id'] for e in terminals}
    assert sum(bool(e['issued']) for e in terminals) == state['generation_calls_issued']
    by_slot = {}
    for e in terminals:
        assert e['run_id'] == plan['run_id'] and e['round'] == 1 and e['active_edges'] == 0
        assert e['messages_received'] == [] and e['graph_edges'] == []
        slot = (e['item_id'], e['agent_id'])
        assert slot not in by_slot, 'Duplicate attempt would double-count usage.'
        by_slot[slot] = e
    complete = [r for r in rows if r['status'] == 'completed']
    details = []
    for r in complete:
        assert not r['simulated'] and r['rounds'] == 1 and len(r['trace']) == 1
        responses = sorted(r['trace'][0]['responses'], key=lambda a: a['agent_id'])
        assert [a['agent_id'] for a in responses] == list(range(4))
        answers = [extract_for_format(a['text'], 'option', prompt_style='concise_option_v2') for a in responses]
        assert answers == r['final_answers'] == r['diagnostics']['answers']
        correctness = [option_exact_match(a, refs[r['item_id']]) for a in answers]
        assert correctness == r['diagnostics']['agent_correctness']
        prediction = majority_vote_options(answers)
        assert prediction == r['prediction'] == r['diagnostics']['majority_prediction']
        counts = Counter(a for a in answers if a is not None)
        tied = sorted(a for a, n in counts.items() if n == max(counts.values())) if counts else []
        all_parsed = all(a is not None for a in answers)
        for a, answer in zip(responses, answers):
            e = by_slot[(r['item_id'], a['agent_id'])]
            assert e['event'] == 'call_completed' and e['finish_reason'] == 'STOP'
            assert e['output_text'] == a['text'] and e['answer'] == answer
        details.append({'item_id':r['item_id'], 'short_id':r['item_id'].rsplit(':', 1)[1],
            'answers':answers, 'reference':refs[r['item_id']], 'agent_correctness':correctness,
            'majority':prediction, 'majority_correct':option_exact_match(prediction, refs[r['item_id']]),
            'tied_winners':tied if len(tied) > 1 else [], 'all_parsed':all_parsed,
            'disagreement':all_parsed and len(set(answers)) > 1,
            'mixed_correct_incorrect':any(correctness) and not all(correctness),
            'unanimous_correct':all_parsed and len(set(answers)) == 1 and all(correctness),
            'unanimous_incorrect':all_parsed and len(set(answers)) == 1 and not any(correctness)})
    n = len(complete)
    assert n > 0 and summary['complete_four_response_questions'] == n
    # This report describes the explicitly requested halted snapshot. Refuse to
    # silently mix a later continuation into its fixed narrative/case examples.
    assert n == 16 and len(terminals) == 66 and dict(Counter(r['status'] for r in rows)) == {
        'completed':16, 'failed_or_incomplete':1, 'not_run':3}, 'Input snapshot changed; review this analysis.'
    assert all(r['all_parsed'] for r in details)
    completed_ids = {r['item_id'] for r in complete}
    completed_events = [e for e in terminals if e['item_id'] in completed_ids]
    counts = {key:sum(row[key] for row in details) for key in
              ('majority_correct','disagreement','mixed_correct_incorrect','unanimous_correct','unanimous_incorrect')}
    agents = [{'human_agent':i+1, 'node_id':i, 'role':plan['roles'][i]['name'],
               'correct':sum(row['agent_correctness'][i] for row in details), 'denominator':n,
               'accuracy':sum(row['agent_correctness'][i] for row in details)/n} for i in range(4)]
    assert counts['majority_correct'] == summary['majority_correct']
    assert counts['disagreement'] == summary['disagreement_count']
    assert counts['unanimous_correct'] + counts['unanimous_incorrect'] == summary['unanimous_count']
    failures = [{k:e.get(k) for k in ('item_id','agent_id','call_id','event','error','finish_reason',
        'http_status','usage','usage_unknown','inference_latency')} | {'text_tail':(e.get('output_text') or '')[-300:]}
        for e in terminals if e['event'] != 'call_completed']
    gsm_rows = read_rows(paths['gsm_records'])
    gsm_summary = json.loads(paths['gsm_summary'].read_text(encoding='utf-8'))
    gsm = {}
    for topology in ('fully_connected','ring'):
        group = [r for r in gsm_rows if r['topology'] == topology]
        assert len(group) == 10 and all(r['status'] == 'completed' for r in group)
        initial = [majority_vote(a['answer'] for a in r['trace'][0]['responses']) for r in group]
        initial_correct = sum(exact_match(a, r['ground_truth']) for a,r in zip(initial,group))
        final_correct = sum(exact_match(r['prediction'],r['ground_truth']) for r in group)
        assert initial_correct == gsm_summary['topologies'][topology]['initial_majority_correct_questions']
        assert final_correct == gsm_summary['topologies'][topology]['correct_questions']
        assert sum(r['input_tokens'] for r in group) == gsm_summary['topologies'][topology]['successful_input_tokens']
        assert sum(r['output_tokens'] for r in group) == gsm_summary['topologies'][topology]['successful_output_tokens']
        initial_disagreements = sum(len({a['answer'] for a in r['trace'][0]['responses']}) > 1 for r in group)
        gsm[topology] = {**gsm_summary['topologies'][topology], 'initial_disagreements':initial_disagreements,
            'max_output_tokens':group[0]['config']['max_output_tokens'],
            'active_edges_by_round':[row['active_edges'] for row in group[0]['trace']]}
    numeric_changes = sum(old['answer'] != new['answer'] for r in gsm_rows
        for before,after in zip(r['trace'],r['trace'][1:]) for old,new in zip(before['responses'],after['responses']))
    assert numeric_changes == len(gsm_summary['agent_answer_changes'])
    output = {'generated_at_utc':datetime.now(timezone.utc).isoformat(),
        'source_sha256':source_hashes, 'analysis_script_sha256':digest(Path(__file__)),
        'plan_sha256':digest(paths['plan']), 'config':plan['config'], 'coverage':dict(Counter(r['status'] for r in rows)),
        'completed_denominator':n, 'completed_items':details, 'agents':agents, 'counts':counts,
        'majority_accuracy_completed':counts['majority_correct']/n,
        'disagreement_rate_completed':counts['disagreement']/n,
        'ties':sum(bool(r['tied_winners']) for r in details),
        'all_attempts_usage':usage_metrics(terminals), 'completed_questions_usage':usage_metrics(completed_events),
        'partial_question_usage':usage_metrics([e for e in terminals if e['item_id'] not in completed_ids]),
        'summed_attempted_question_wall_seconds':sum(r.get('wall_seconds',0.) for r in rows),
        'mean_completed_question_wall_seconds':sum(r['wall_seconds'] for r in complete)/n,
        'failures':failures, 'not_run_ids':[r['item_id'] for r in rows if r['status'] == 'not_run'],
        'gsm8k':gsm, 'gsm8k_unknown_usage_attempts':gsm_summary['unknown_usage_attempts'],
        'gsm8k_numeric_answer_changes':numeric_changes,
        'limitations':['Completed-prefix analysis; no imputation for incomplete/not-run items.',
            'Benchmark-label accuracy, not independently adjudicated mathematical correctness.',
            'Independent AQUA v2 has no communication and cannot establish communication gains.',
            'Datasets, prompts, token caps, round counts and sample sizes differ from GSM8K.']}
    return output, source_hashes


def render(data: dict) -> str:
    n, c = data['completed_denominator'], data['counts']
    lines = ['# AQUA-RAT v2: independent-agent diagnostic — midterm results', '',
        '**Preliminary, incomplete run; offline analysis only.** Gemini 3.5 Flash-Lite, minimal thinking; '
        'four distinct reasoning roles, one independent round, zero edges, 2,048-token output cap. '
        'All primary accuracies below use the **16 completed questions**, not 20 planned questions. '
        'Correctness means agreement with the frozen benchmark option label.', '',
        '## Coverage and accuracy', '',
        '| Coverage | Count |', '|---|---:|',
        f"| Complete questions | {n}/20 (80%) |", '| Partial question | 1/20 |', '| Not attempted | 3/20 |',
        '| Generation attempts | 66/80 |', '| Complete responses / truncated responses | 65 / 1 |', '',
        '| Agent / strategy | Correct / completed | Accuracy |', '|---|---:|---:|']
    for agent in data['agents']:
        lines.append(f"| {agent['human_agent']} — {agent['role']} | {agent['correct']}/{n} | {agent['accuracy']:.2%} |")
    lines += [f"| **System plurality vote** | **{c['majority_correct']}/{n}** | **{data['majority_accuracy_completed']:.2%}** |", '',
        'The project calls this majority voting, but it selects the most frequent parsed option '
        '(plurality); ties use alphabetical order A < B < C < D < E. Invalid choices abstain; no '
        'reference is consulted. All 64 responses in completed questions parsed successfully. '
        'There were two 2–2 ties: validation:126, A vs D → A (label match), and validation:68, '
        'C vs D → C (label mismatch). One tie was correct and one incorrect; tie behavior affects the score.', '',
        'The partial item and three unattempted items are excluded from completed-question accuracy. '
        'The planned-denominator majority count is 14/20 (70%) with 4 missing outcomes; this is a '
        'coverage-sensitive count, not accuracy on 20 answered questions. No missing response is imputed.', '',
        '## Agreement and error diversity', '',
        '| Metric on 16 complete questions | Count | Percentage |', '|---|---:|---:|']
    for label,key in [('Disagreement (at least two distinct choices)','disagreement'),
        ('At least one label-correct and one label-incorrect agent','mixed_correct_incorrect'),
        ('Unanimous, label-correct','unanimous_correct'),('Unanimous, label-incorrect','unanimous_incorrect')]:
        lines.append(f"| {label} | {c[key]}/{n} | {c[key]/n:.2%} |")
    lines += ['', '| Item | Agents 1 / 2 / 3 / 4 | Reference | System vote | Result |', '|---|---|---|---|---|']
    for r in data['completed_items']:
        if r['disagreement']:
            lines.append(f"| validation:{r['short_id']} | {' / '.join(r['answers'])} | {r['reference']} | {r['majority']} | {'Correct' if r['majority_correct'] else 'Incorrect'} |")
    lines += ['', 'These three items expose differing final choices, not demonstrated communication benefits. '
        'At validation:126, Agents 1 and 4 calculate 24 days but select D (28), suggesting answer-to-option '
        'mapping inconsistencies rather than solely failed arithmetic. At validation:71 the correct '
        'choice exists in one agent but the plurality selects B. The strongest individual agent scored '
        '15/16, exceeding the system vote in this small sample. No peers exchanged messages.', '',
        '## Exact interruption and data-quality caveat', '',
        'At validation:146, Agent 1/node 0 returned STOP and chose B (label A; excluded from the 16-item '
        'analysis). Agent 2/node 1 returned **MAX_TOKENS**, not STOP, before a final answer. '
        'Error: `Gemini response was blocked, truncated or incomplete; recorded usage is retained. '
        'finish_reason=MAX_TOKENS; candidates=1.`', '',
        f"The failed call ID is `{data['failures'][0]['call_id']}`. It reports 266 input, 2,044 output, "
        'zero thinking tokens and 5.930364 seconds. HTTP status is null; no quota, authentication, '
        'model-access or timeout failure is recorded. The backend rejects truncation before answer '
        'parsing, and the diagnostic halts on every incomplete response. Nodes 2/3 of this item were '
        'not called; validation:66, :215 and :14 were not attempted. The 150-word prompt instruction '
        'is soft; it did not prevent lengthy reconsideration and output-cap exhaustion.', '',
        '**Data-quality flags (not rescored or excluded):** validation:146 gives '
        '`0.05×3 + 0.20x = 0.10(3+x)`, hence x = 1.5 quarts. No displayed choice contains 1.5; '
        'the reference A is 2 quarts. Both returned texts derive 1.5 and then reconsider the options. '
        'This mismatch plausibly contributed to verbosity, but is not a measured causal explanation. '
        'For validation:68, the stated equations yield cost 12.5 and original sale price 13.75, '
        'also absent from its options (reference D is 13.5). These are local arithmetic consistency '
        'flags requiring dataset review; official-label metrics above remain unchanged.', '',
        '## Actual tokens and latency', '',
        '| Scope | Attempts | Input tokens | Output tokens | Total tokens | API request latency (s) |',
        '|---|---:|---:|---:|---:|---:|']
    for label,key in [('16 complete questions','completed_questions_usage'),('Partial validation:146','partial_question_usage'),
                      ('All recorded attempts','all_attempts_usage')]:
        u = data[key]
        lines.append(f"| {label} | {u['attempts']} | {u['input_tokens']:,} | {u['output_tokens_including_thinking']:,} | {u['total_tokens']:,} | {u['known_request_latency_seconds']:.3f} |")
    u = data['completed_questions_usage']
    lines += ['', f"All 66 attempts have provider usage and measured latency; no unknown usage. Thinking tokens: 0. "
        f"Mean summed API latency per complete question: {u['known_request_latency_seconds']/n:.3f} s. "
        f"Mean complete-question wall time: {data['mean_completed_question_wall_seconds']:.3f} s; "
        f"summed attempted-question wall time: {data['summed_attempted_question_wall_seconds']:.3f} s.", '',
        'API latency is client-observed request time, not isolated server compute. Wall time also '
        'includes pacing and logging. Partial-call usage is included in totals; cached input is not '
        'double-counted. These totals exclude the original v1 diagnostic, GSM8K and metadata reads.', '',
        '## Comparison with the earlier GSM8K pilot', '',
        '| Setting / result | AQUA-RAT v2 independent | GSM8K fully connected | GSM8K ring |',
        '|---|---|---|---|', '| Questions | 16 complete / 20 frozen validation | 10 validation | Same 10 validation |',
        '| Format | Five-choice A–E | Numerical exact match | Numerical exact match |',
        '| Model / thinking | Gemini 3.5 Flash-Lite / minimal | Same | Same |',
        '| Agents / rounds | 4 roles / 1 independent | 4 generic agents / 3 | 4 generic agents / 3 |',
        '| Output cap per call | 2,048 | 512 | 512 |',
        '| Active edges by round | 0 | 0 → 12 → 12 | 0 → 4 → 4 |',
        '| Initial plurality accuracy | 14/16 (87.5%) | 10/10 (100%) | 10/10 (100%) |',
        '| Final system accuracy | Same as initial (no later rounds) | 10/10 (100%) | 10/10 (100%) |',
        '| Initial disagreement | 3/16 (18.75%) | 0/10 | 0/10 |',
        '| Known input tokens | 18,802 (all 66 attempts) | 75,200 (successful calls) | 44,002 (successful calls) |',
        '| Known output tokens | 33,321 (all 66 attempts) | 22,498 (successful calls) | 22,175 (successful calls) |',
        '| Successful-call API latency (s) | 119.040 (65 responses) | 179.118 | 214.984 |', '',
        'GSM8K had 240 successful responses and one additional HTTP 504 attempt: 241 attempts overall. '
        'The full-graph timeout took 59.378 s with unknown token usage; its true all-attempt token total '
        'is unavailable. AQUA totals include its known truncated call; GSM8K token columns include '
        'successful calls only. These consumption/latency figures are not matched-budget comparisons.', '',
        'Both GSM8K initial and final votes were already correct, with zero numeric answer changes '
        'across 160 agent transitions. AQUA shows more observed disagreement and lower label accuracy '
        'in its completed subset, but dataset, roles, prompts, output cap, round count, sample size '
        'and completion differ. Their individual effects cannot be isolated. Multiple-choice labeling '
        'and suspected option errors further limit interpretation. This small incomplete diagnostic '
        'does not prove communication superiority, causal role benefits, or successful error correction.', '',
        '## Reproduce without API access', '',
        '```powershell',
        '.\\.venv\\Scripts\\python.exe -m experiments.analyze_diagnostic_v2 --output-prefix results/aqua_v2_midterm_repeat',
        '```', '',
        'The script imports no inference SDK, recomputes choices/votes from raw returned text, '
        'checks stored diagnostics, reconciles all request starts/terminal events and rejects duplicate '
        'attempts. It writes new Markdown/JSON only, refuses existing output files, and verifies all '
        'pre-existing results remain unchanged. JSON contains per-question outcomes, source SHA-256 '
        'hashes and analysis-script hash. No training or experiment is started.', '']
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-prefix', type=Path, default=Path('results/aqua_v2_midterm'))
    args = parser.parse_args()
    base = ROOT / args.output_prefix
    if base.resolve().parent != (ROOT/'results').resolve():
        raise ValueError('Use a new output prefix directly under results/.')
    outputs = [base.with_suffix('.json'), base.with_suffix('.md')]
    if any(p.exists() for p in outputs):
        raise ValueError('Analysis outputs exist; choose a different prefix. No files overwritten.')
    preserved = {p:digest(p) for p in (ROOT/'results').iterdir() if p.is_file()}
    data, hashes = analyze()
    for path, content in zip(outputs, [json.dumps(data, indent=2), render(data)]):
        with path.open('x', encoding='utf-8') as handle:
            handle.write(content + '\n')
    assert all(digest(p) == checksum for p,checksum in preserved.items())
    print(json.dumps({'outputs':[str(p) for p in outputs], 'completed_questions':data['completed_denominator'],
        'agent_correct':[r['correct'] for r in data['agents']], 'counts':data['counts'],
        'usage':data['all_attempts_usage'], 'prior_result_files_preserved':len(preserved), 'api_calls_made':0}, indent=2))


if __name__ == '__main__':
    main()
