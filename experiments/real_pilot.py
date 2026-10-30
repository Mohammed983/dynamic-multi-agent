"""Prepare an unpaid, frozen pilot; run only after a human-approved budget receipt."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
from time import perf_counter

from dotenv import dotenv_values

from src.agents import Agent
from src.benchmarks import GSM8K
from src.evaluation import exact_match, extract_answer
from src.pilot import Budget, MeteredOpenAILLM, PilotStopped, append_jsonl, usd
from src.runner import run_question

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'results'
PLAN = RESULTS / 'real_pilot_plan.json'
QUESTIONS = ROOT / 'data/pilot_questions.jsonl'
REFERENCES = RESULTS / 'real_pilot_references.jsonl'
RECORDS = RESULTS / 'real_pilot.jsonl'
CALLS = RESULTS / 'real_pilot_calls.jsonl'
REPORT = RESULTS / 'real_llm_pilot.md'
MODEL_DOC = 'https://developers.openai.com/api/docs/models/gpt-5-mini'
REASONING_DOC = 'https://developers.openai.com/api/docs/guides/reasoning'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hash() -> str:
    paths = sorted((ROOT / 'src').rglob('*.py')) + [Path(__file__)]
    combined = b''.join(str(p.relative_to(ROOT)).encode() + b'\0' + p.read_bytes() for p in paths)
    return hashlib.sha256(combined).hexdigest()


def configure_caches() -> None:
    for name, relative in [('HF_HOME', 'data/hf_home'), ('HF_DATASETS_CACHE', 'data/hf_datasets'),
                           ('TIKTOKEN_CACHE_DIR', 'data/tiktoken_cache')]:
        os.environ[name] = str(ROOT / relative)
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    logging.disable(logging.CRITICAL)


def api_client():
    """Only .env supplies the key; ignore alternate credential/base-URL environment settings."""
    from openai import OpenAI
    key = (dotenv_values(ROOT / '.env').get('OPENAI_API_KEY') or '').strip()
    if not key:
        raise PilotStopped('No API credential is configured in the local .env file.')
    return OpenAI(api_key=key, base_url='https://api.openai.com/v1', timeout=90, max_retries=0), key


def estimate(questions: list[dict]) -> dict:
    import tiktoken
    encoding = tiktoken.encoding_for_model('gpt-5-mini')
    # Same builder as real inference. No model is invoked or simulated here.
    base = sum(len(encoding.encode(Agent(i, None).build_prompt(q['question'], ())))
               for q in questions for i in range(4))
    # Two topologies * three rounds; 160 own-history and 320 peer-text copies.
    expected_input = base * 6 + 240 * 32 + 480 * 200
    return {'assumed_visible_tokens_per_response': 200,
            'assumed_total_output_tokens_per_call_range': [400, 1000],
            'estimated_input_tokens': expected_input,
            'estimated_cost_usd_range': [usd(expected_input, 240 * n) for n in (400, 1000)],
            'max_calls': 240, 'max_output_tokens_per_call': 2048,
            'max_input_token_reservation_per_call': 12000,
            'capped_scenario_usd': 240 * usd(12000, 2048),
            'proposed_budget_usd': 2.0, 'input_usd_per_million': .25,
            'cached_input_usd_per_million': .025, 'output_usd_per_million': 2.0,
            'pricing_source': MODEL_DOC, 'tokenizer': encoding.name,
            'notes': 'Forecast, not observed usage. No cache discount assumed. Reasoning tokens are output.'}


def prepare() -> None:
    if RECORDS.exists() or CALLS.exists():
        raise PilotStopped('Existing paid-run artifacts found; do not overwrite or regenerate this plan.')
    configure_caches()
    RESULTS.mkdir(exist_ok=True)
    QUESTIONS.parent.mkdir(exist_ok=True)
    state = {'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'preparing',
             'model': 'gpt-5-mini', 'agents': 4, 'rounds': 3,
             'topologies': ['fully_connected', 'ring'], 'split': 'validation',
             'partition_seed': 42, 'validation_fraction': .2,
             'settings': {'reasoning': {'effort': 'low'}, 'max_output_tokens': 2048,
                          'temperature': None, 'store': False, 'max_retries': 0,
                          'timeout_seconds': 90, 'service_tier': 'default'},
             'generation_calls_made': 0, 'source_sha256': source_hash()}
    try:
        from huggingface_hub import HfApi
        revision = HfApi().dataset_info('openai/gsm8k', token=False).sha
        benchmark = GSM8K(revision=revision)
        items = benchmark.load('validation', limit=10, seed=42)
        if (len(items) != 10 or len({i.item_id for i in items}) != 10
                or len({i.question for i in items}) != 10
                or any(not i.question.strip() or extract_answer(i.reference) is None for i in items)):
            raise PilotStopped('Dataset validation failed: require 10 unique questions with parseable references.')
        questions = [{'item_id': i.item_id, 'question': i.question, 'split': i.split} for i in items]
        QUESTIONS.write_text(''.join(json.dumps(q) + '\n' for q in questions), encoding='utf-8')
        REFERENCES.write_text(''.join(json.dumps({'item_id': i.item_id, 'ground_truth': i.reference}) + '\n'
                                      for i in items), encoding='utf-8')
        state.update(dataset={'name': 'openai/gsm8k', 'config': 'main', 'revision': revision,
                              'question_count': len(items), 'verified_unique': True},
                     question_file_sha256=digest(QUESTIONS), reference_file_sha256=digest(REFERENCES),
                     cost=estimate(questions))
        print('Dataset verified: 10 unique GSM8K validation questions frozen for both topologies.', flush=True)
        # Authenticated metadata GET only: no inference/generation endpoint is invoked.
        client, _ = api_client()
        try:
            model = client.models.retrieve('gpt-5-mini')
            state['api_preflight'] = {'status': 'passed', 'method': 'GET /v1/models/gpt-5-mini',
                                      'returned_model_id': model.id, 'generation_performed': False}
        except Exception as exc:
            state['api_preflight'] = {'status': 'failed', 'error_type': type(exc).__name__,
                                      'http_status': getattr(exc, 'status_code', None),
                                      'generation_performed': False}
        finally:
            client.close()
        state['status'] = ('awaiting_budget_approval' if state['api_preflight']['status'] == 'passed'
                           else 'blocked_api_preflight')
    except Exception as exc:
        state.update(status='blocked_preparation', error_type=type(exc).__name__)
        # Never print exception text: auth failures may contain credential fragments.
    state['versions'] = {p: importlib.metadata.version(p) for p in ('openai', 'datasets', 'tiktoken', 'networkx', 'numpy')}
    PLAN.write_text(json.dumps(state, indent=2), encoding='utf-8')
    write_report(state, [], [])
    print(json.dumps(state, indent=2), flush=True)


def write_report(plan: dict, records: list[dict], events: list[dict]) -> None:
    rows = ['# Real LLM pilot: GSM8K, full versus ring', '',
            f"Status: **{plan['status']}**. No mock responses are used in this pilot.", '',
            '## Protocol', '',
            'The same 10 frozen validation questions are assigned to both topologies, with 4 agents '
            'and 3 rounds. Model: `gpt-5-mini`; reasoning effort: low; output cap: 2,048 tokens; '
            'temperature omitted; default service tier; no tools, retained API history or retries. '
            'The existing prompt builder is shared. Initial prompt strings match across topologies; '
            'revision prompts necessarily differ in delivered peer content. Each topology has its own '
            'independent initial calls; these are not a shared-snapshot causal intervention.', '',
            'Pair order alternates by question: full then ring, ring then full. Execution is sequential. '
            'Configured full graph edge counts are [0,12,12]; ring counts are [0,4,4]. These correspond to '
            '24 versus 8 message deliveries per completed question across the communication rounds.', '']
    if 'dataset' in plan:
        rows.extend([f"Pinned dataset revision: `{plan['dataset']['revision']}`.",
                     f"Frozen question-file SHA-256: `{plan['question_file_sha256']}`.", ''])
    if 'cost' in plan:
        cost = plan['cost']
        low, high = cost['estimated_cost_usd_range']
        rows.extend(['## Pre-run cost and approval', '',
                     f'Estimated generation cost: **${low:.3f}–${high:.3f} USD**, assuming 200 visible '
                     'tokens per solution and 400–1,000 total output tokens per call. These are planning '
                     'assumptions, not observed model behavior.', '',
                     'At 240 calls, the configured input reservation/output caps give a conservative '
                     f"capped scenario of **${cost['capped_scenario_usd']:.5f}**; proposed spending limit "
                     'is **$2.00 USD**, with a pre-request reservation check. No cache discount is assumed '
                     'for budgeting. Prompts exceeding the input safety cap halt rather than being '
                     'silently truncated. This is a client-side guard for this pilot, not an account-wide '
                     'provider billing limit.', '',
                     'The published rates used are $0.25/M input, $0.025/M cached input and $2.00/M '
                     f'output. [Official model/pricing reference]({MODEL_DOC}). '
                     f'[Reasoning-token limits]({REASONING_DOC}) include non-visible reasoning in '
                     'the output budget. No Batch API pricing is used.', ''])
    rows.extend(['## Results and completeness', ''])
    if not records:
        rows.extend(['**No generation calls have been made.** Accuracy, actual tokens, inference '
                     'latency, agent responses and communication effects are not yet available. '
                     'The paid run is pending approval or blocked as specified above.', '',
                     f"API metadata preflight: `{json.dumps(plan.get('api_preflight', {'status': 'not_reached'}))}`.", ''])
    else:
        if not any(r['status'] == 'completed' for r in records):
            rows.extend(['**No question completed in either topology. Accuracy and a topology '
                         'comparison are unavailable, not 0% measured model accuracy. No agent '
                         'solution or communication round completed.**', ''])
        failures = [e for e in events if e['event'] == 'request_failed']
        if failures:
            first = failures[0]
            rows.extend([f"First request failure: `{first.get('error_type')}`, HTTP "
                         f"`{first.get('http_status')}`; client-observed duration "
                         f"{first.get('latency', 0.):.3f} seconds. No automatic retry was made.", '',
                         'The error subtype/body was deliberately not retained by this run, so a '
                         '429 cannot be attributed specifically to rate pressure, credits or a spend '
                         'limit. Successful model-metadata authentication did not establish inference '
                         'quota. Check API account/project billing, credits and request/token limits '
                         'before another attempt. '
                         '[Official error-code guidance](https://developers.openai.com/api/docs/guides/error-codes).', ''])
        rows.extend(['| Topology | Complete / planned | Correct / planned | Accuracy (failures included) | '
                     'Input tokens | Output tokens | Request seconds (incl. failures) | Mean completed question wall seconds |',
                     '|---|---:|---:|---:|---:|---:|---:|---:|'])
        for topology in ('fully_connected', 'ring'):
            group = [r for r in records if r['topology'] == topology]
            calls = [e for e in events if e['topology'] == topology]
            complete = sum(r['status'] == 'completed' for r in group)
            correct = sum(bool(r.get('correct', False)) for r in group)
            wall = (f"{sum(r.get('question_wall_seconds', 0.) for r in group if r['status'] == 'completed') / complete:.3f}"
                    if complete else 'N/A')
            unknown = any(e.get('usage_unknown') for e in calls)
            def token_display(name: str) -> str:
                known = sum(e.get(name, 0) for e in calls)
                if unknown:
                    return f'{known} known + unknown' if known else 'Unknown'
                return str(known) if calls else 'Not run'
            correct_display = f'{correct}/10' if complete else 'N/A'
            accuracy_display = f'{correct/10:.0%}' if complete else 'N/A'
            request_seconds = f"{sum(e.get('latency', 0.) for e in calls):.3f}" if calls else 'Not run'
            rows.append(f'| {topology} | {complete}/10 | {correct_display} | {accuracy_display} | '
                        f"{token_display('input_tokens')} | {token_display('output_tokens')} | "
                        f'{request_seconds} | {wall} |')
        rows.extend(['', 'Token sums include all received usage, including incomplete responses; '
                     'missing usage is **unknown**, not zero-cost. Call latency includes failed requests '
                     'when known; a rejected-request duration is not measured LLM compute latency. '
                     'Question wall time includes routing and logging; averages use completed questions '
                     'only. When any questions complete, planned-denominator accuracy counts other '
                     'failed/not-run cases as incorrect; without any completed questions accuracy is N/A.', '',
                     ('Known usage-based cost estimate: unavailable; no token-metered response received. '
                      if not any('estimated_charge_usd' in e for e in events) else
                      f"Known usage-based cost subtotal: ${sum(e.get('estimated_charge_usd', 0.) for e in events):.6f}. ") +
                     f"Calls with unknown usage: {sum(bool(e.get('usage_unknown')) for e in events)}. "
                     'The provider invoice is authoritative.', '', '## Per-question status', '',
                     '| Item | Topology | Status | Prediction | Correct |', '|---|---|---|---|---|'])
        for r in records:
            evaluated = r.get('correct') if r['status'] == 'completed' else 'N/A'
            rows.append(f"| {r['item_id']} | {r['topology']} | {r['status']} | {r.get('prediction')} | {evaluated} |")
        rows.extend(['', '## Observed changes after communication', '',
                     'Changes below are descriptive and do not prove that communication caused '
                     'an improvement: self-revision and stochastic variation remain alternative explanations.', ''])
        changed = 0
        for r in records:
            if r['status'] != 'completed':
                continue
            for row in r['trace'][1:]:
                old = r['trace'][row['round'] - 2]['responses']
                for new in row['responses']:
                    before = old[new['agent_id']]
                    if before['answer'] != new['answer']:
                        changed += 1
                        senders = [m['agent_id'] for m in row['messages'][str(new['agent_id'])]]
                        rows.append(f"- {r['item_id']}, {r['topology']}, agent {new['agent_id']}, "
                                    f"round {row['round']}: {before['answer']} → {new['answer']}; "
                                    f'previous-round senders: {senders}. See full response text below.')
        if not changed:
            rows.append('No extracted agent-answer changes were observed in completed records. '
                        'There is no supported example of an answer improvement to report.')
        rows.extend(['', '### Revision examples with delivered peer text', '',
                     'The following paired excerpts show observed text before and after receiving '
                     'messages. They do not isolate communication from self-revision.', ''])
        for topology in ('fully_connected', 'ring'):
            examples = [r for r in records if r['status'] == 'completed' and r['topology'] == topology]
            if not examples:
                rows.append(f'No completed {topology} example is available.')
                continue
            example = examples[0]
            before, after = example['trace'][0]['responses'][0], example['trace'][1]['responses'][0]
            messages = example['trace'][1]['messages']['0']
            rows.extend([f"**{example['item_id']}, {topology}, agent 0, rounds 1 → 2**", '',
                         f"Initial answer: {before['answer']}; revised answer: {after['answer']}. "
                         'Excerpts below are capped at 600 characters; the appendix contains complete text.', '',
                         'Initial response:', '', '``````````text', before['text'][:600], '``````````', ''])
            for message in messages:
                rows.extend([f"Received from agent {message['agent_id']}:", '',
                             '``````````text', message['text'][:600], '``````````', ''])
            rows.extend(['Revised response:', '', '``````````text', after['text'][:600], '``````````', ''])
        rows.extend(['', '## Individual agent responses', ''])
        for r in records:
            rows.extend([f"### {r['item_id']} / {r['topology']} / {r['status']}", ''])
            for row in r.get('trace', []):
                for response in row['responses']:
                    senders = [m['agent_id'] for m in row['messages'][str(response['agent_id'])]]
                    rows.extend([f"Round {row['round']}, agent {response['agent_id']}; received from {senders}.", '',
                                 '``````````text', response['text'], '``````````', ''])
        partial = [e for e in events if not any(r['item_id'] == e['item_id'] and r['topology'] == e['topology']
                                               and r['status'] == 'completed' for r in records)]
        if partial:
            rows.extend(['### Partial responses from failed questions', ''])
            for e in partial:
                rows.extend([f"{e['item_id']}, {e['topology']}, round {e['round']}, agent {e['agent_id']}: "
                             f"{e.get('status', e['event'])}.", '', '``````````text',
                             e.get('output_text') or '[No returned solution text]', '``````````', ''])
    rows.extend(['## Files and limitations', '',
                 '`real_pilot_plan.json`: frozen protocol/preflight; `data/pilot_questions.jsonl`: '
                 'runtime questions only; `real_pilot_references.jsonl`: offline evaluation references; '
                 '`real_pilot.jsonl`: 20 planned question/topology outcomes after execution; '
                 '`real_pilot_calls.jsonl`: durable request-start and response/failure events, preserving '
                 'partial outputs and usage. No key or raw SDK exception text is written.', '',
                 'Ten questions yield accuracy increments of 10 percentage points. This is a debugging '
                 'pilot, not a statistically powered comparison. No MLP is trained.'])
    REPORT.write_text('\n'.join(rows) + '\n', encoding='utf-8')


def run(approval_path: Path) -> None:
    configure_caches()
    plan = json.loads(PLAN.read_text(encoding='utf-8'))
    approval = json.loads(approval_path.read_text(encoding='utf-8'))
    if (approval.get('approved') is not True or approval.get('plan_sha256') != digest(PLAN)
            or approval.get('budget_usd') != 2.0 or plan['status'] != 'awaiting_budget_approval'):
        raise PilotStopped('Explicit human approval for this exact plan and $2 budget is required.')
    if (digest(QUESTIONS) != plan['question_file_sha256'] or digest(REFERENCES) != plan['reference_file_sha256']
            or source_hash() != plan['source_sha256']):
        raise PilotStopped('Approved questions/references/source changed; prepare and review a new plan.')
    if RECORDS.exists() or CALLS.exists():
        raise PilotStopped('Existing attempts found; automatic resume/replay is refused to avoid duplicate spending.')
    client, key = api_client()
    budget = Budget()
    backend = MeteredOpenAILLM(client, budget, CALLS, key)
    questions = [json.loads(line) for line in QUESTIONS.read_text().splitlines()]
    references = {row['item_id']: row['ground_truth'] for row in
                  (json.loads(line) for line in REFERENCES.read_text().splitlines())}
    records, stopped = [], False
    try:
        for index, item in enumerate(questions):
            order = ('fully_connected', 'ring') if index % 2 == 0 else ('ring', 'fully_connected')
            for topology in order:
                record = {'item_id': item['item_id'], 'topology': topology, 'status': 'not_run',
                          'ground_truth': references[item['item_id']], 'correct': False,
                          'plan_sha256': digest(PLAN), 'simulated': False,
                          'split': plan['split'], 'dataset_revision': plan['dataset']['revision']}
                if not stopped:
                    backend.start_question(item['item_id'], topology)
                    started = perf_counter()
                    try:
                        result = run_question(item['question'], backend, count=4, rounds=3, topology=topology)
                        record.update(result, status='completed', correct=exact_match(result['prediction'],
                                                                                       references[item['item_id']]))
                    except PilotStopped as exc:
                        record.update(status='failed_or_incomplete', reason=str(exc))
                        stopped = True
                    except Exception:
                        record.update(status='failed_or_incomplete', reason='Unexpected execution error; inspect durable call events.')
                        stopped = True
                    record['question_wall_seconds'] = perf_counter() - started
                records.append(record)
                append_jsonl(RECORDS, record)
                print(f"{record['item_id']} {topology}: {record['status']}; "
                      f'committed budget ${budget.committed_usd:.4f}/$2.00', flush=True)
                plan['status'] = 'in_progress'
                write_report(plan, records, backend.events)
    finally:
        client.close()
        plan['status'] = ('completed' if len(records) == 20 and not stopped
                          and all(r['status'] == 'completed' for r in records) else 'halted_incomplete')
        plan['committed_budget_usd'] = budget.committed_usd
        plan['issued_generation_calls'] = budget.issued_calls
        plan['generation_calls_made'] = budget.issued_calls
        (RESULTS / 'real_pilot_execution.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
        write_report(plan, records, backend.events)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run'])
    parser.add_argument('--approval-file', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.approval_file is None:
        parser.error('Paid execution requires --approval-file after explicit human approval.')
    else:
        run(args.approval_file)


if __name__ == '__main__':
    try:
        main()
    except PilotStopped as exc:
        print(str(exc))
        raise SystemExit(1) from None
