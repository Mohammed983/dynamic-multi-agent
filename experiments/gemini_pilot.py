"""The explicitly authorized, free-tier ten-question Gemini pilot.

Uses existing frozen questions and synchronous runner. Never resumes/replays calls
automatically, changes billing, changes model, or substitutes a mock response.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
from time import monotonic, perf_counter, sleep
import uuid

from dotenv import dotenv_values
import yaml

from src.evaluation import exact_match, extract_answer, extract_for_format, majority_vote
from src.gemini_client import GeminiError, GeminiLLM
from src.pilot import append_jsonl
from src.runner import run_question
from src.topology import make_topology

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'results'
PREFIX = 'gemini_10_question_pilot'
PLAN = RESULTS / f'{PREFIX}_plan.json'
CALLS = RESULTS / f'{PREFIX}_calls.jsonl'
OUTCOMES = RESULTS / f'{PREFIX}.jsonl'
STATE = RESULTS / f'{PREFIX}_state.json'
REPORT = RESULTS / f'{PREFIX}.md'
ACCESS = RESULTS / f'{PREFIX}_access.json'
CONFIG = ROOT / 'configs/gemini_3_5_flash_lite.yaml'
QUESTIONS = ROOT / 'data/pilot_questions.jsonl'
REFERENCES = RESULTS / 'real_pilot_references.jsonl'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources_digest() -> str:
    files = sorted((ROOT / 'src').rglob('*.py')) + [Path(__file__)]
    return hashlib.sha256(b''.join(str(p.relative_to(ROOT)).encode() + b'\0' + p.read_bytes() for p in files)).hexdigest()


def frozen_items() -> tuple[list[dict], dict[str, str], dict]:
    previous = json.loads((RESULTS / 'real_pilot_plan.json').read_text(encoding='utf-8'))
    if digest(QUESTIONS) != previous['question_file_sha256'] or digest(REFERENCES) != previous['reference_file_sha256']:
        raise ValueError('Previously frozen question/reference hashes changed; refusing to select replacement items.')
    questions = [json.loads(line) for line in QUESTIONS.read_text(encoding='utf-8').splitlines()]
    rows = [json.loads(line) for line in REFERENCES.read_text(encoding='utf-8').splitlines()]
    refs = {r['item_id']: r['ground_truth'] for r in rows}
    if (len(questions) != 10 or len(rows) != 10 or len(refs) != 10
            or len({q['item_id'] for q in questions}) != 10 or len({q['question'] for q in questions}) != 10
            or {q['item_id'] for q in questions} != set(refs)
            or any(q['split'] != 'validation' or not q['question'].strip() for q in questions)
            or any(extract_answer(reference) is None for reference in refs.values())):
        raise ValueError('Frozen data do not contain ten valid distinct validation questions and references.')
    return questions, refs, previous


def validate_access(access: dict) -> None:
    """Fresh operator reading, not a claim that model metadata proves billing/quota."""
    if access.get('free_tier') is not True or access.get('billing_disabled') is not True:
        raise ValueError('Current Free-tier and billing-disabled confirmation is required.')
    remaining = access.get('remaining_daily_requests')
    if type(remaining) is not int or not 260 <= remaining <= 500:
        raise ValueError('Need 240 requests plus at least 20 daily requests reserved.')
    if access.get('model') != 'gemini-3.5-flash-lite':
        raise ValueError('Quota check must refer to the exact requested model.')
    checked = datetime.fromisoformat(access['checked_at'])
    age = (datetime.now(timezone.utc) - checked).total_seconds()
    if not 0 <= age <= 3600:
        raise ValueError('Account check is stale; verify the current project state before execution.')


class PilotCallError(RuntimeError):
    def __init__(self, message: str, fatal: bool = True):
        super().__init__(message)
        self.fatal = fatal


class JournalBackend:
    """Observe the existing backend; keep references outside this object."""
    simulated = False

    def __init__(self, raw: GeminiLLM, journal: Path, run_id: str, interval: float = 5.1,
                 answer_format: str = 'numeric', prompt_style: str = 'default'):
        if answer_format not in {'numeric', 'option'}:
            raise ValueError('Journal answer format must be numeric or option.')
        self.answer_format = answer_format
        self.prompt_style = prompt_style
        self.raw, self.model, self.journal = raw, raw.model, journal
        self.run_id, self.interval = run_id, interval
        self._last_start: float | None = None
        self.context: dict = {}
        self.index = 0
        self.events: list[dict] = []
        self.completed: dict[tuple[int, int], dict] = {}

    def start_question(self, item_id: str, topology: str) -> None:
        self.context = {'item_id': item_id, 'topology': topology}
        self.index, self.completed = 0, {}

    def generate(self, prompt: str):
        if self.raw._key in prompt:
            raise PilotCallError('Credential detected in prompt; refused before logging.')
        if self.raw.generation_calls_issued >= 240:
            raise PilotCallError('Pilot generation-call limit reached; no request sent.')
        if self._last_start is not None:
            while (wait := self.interval - (monotonic() - self._last_start)) > 0:
                sleep(min(wait, 30.))
        round_number, agent_id = self.index // 4 + 1, self.index % 4
        self.index += 1
        graph = make_topology(self.context['topology'], 4)
        edges = sorted(graph.edges()) if round_number > 1 else []
        messages = ([self.completed[(round_number - 1, source)] for source in sorted(graph.predecessors(agent_id))]
                    if round_number > 1 else [])
        event = {'run_id': self.run_id, 'call_id': str(uuid.uuid4()), **self.context,
                 'round': round_number, 'agent_id': agent_id, 'model': self.model,
                 'timestamp': datetime.now(timezone.utc).isoformat(), 'prompt': prompt,
                 'graph_edges': edges, 'active_edges': len(edges), 'messages_received': messages,
                 'event': 'request_started'}
        append_jsonl(self.journal, event)
        before = self.raw.generation_calls_issued
        self._last_start = monotonic()
        try:
            completion = self.raw.generate(prompt)
        except BaseException as exc:
            observed = self.raw.last_response or {}
            usage = observed.get('usage')
            # Known incomplete content aborts only this question. Any unknown usage,
            # quota/auth/billing restriction or network failure stops the entire pilot.
            fatal = not (isinstance(exc, GeminiError) and usage is not None
                         and observed.get('finish_reason') is not None)
            safe_error = str(exc) if isinstance(exc, GeminiError) else type(exc).__name__
            final = {**event, 'event': 'call_failed' if fatal else 'call_incomplete',
                     'issued': self.raw.generation_calls_issued > before, 'usage': usage,
                     'usage_unknown': usage is None and self.raw.generation_calls_issued > before,
                     'inference_latency': observed.get('latency'), 'output_text': observed.get('text'),
                     'finish_reason': observed.get('finish_reason'),
                     'http_status': getattr(exc, 'http_status', None), 'error': safe_error, 'fatal': fatal}
            self.events.append(final)
            append_jsonl(self.journal, final)
            raise PilotCallError(safe_error, fatal) from None
        usage = self.raw.usage_records[-1]
        final = {**event, 'event': 'call_completed', 'issued': True, 'usage': usage,
                 'usage_unknown': False, 'inference_latency': completion.latency,
                 'output_text': completion.text, 'answer': extract_for_format(completion.text, self.answer_format, prompt_style=self.prompt_style),
                 'finish_reason': usage['finish_reason'], 'fatal': False}
        self.completed[(round_number, agent_id)] = {
            'agent_id': agent_id, 'round': round_number, 'text': completion.text,
            'answer': final['answer'], 'input_tokens': completion.input_tokens,
            'output_tokens': completion.output_tokens, 'latency': completion.latency}
        self.events.append(final)
        append_jsonl(self.journal, final)
        return completion


def read_events() -> list[dict]:
    if not CALLS.exists():
        return []
    return [r for r in (json.loads(line) for line in CALLS.read_text(encoding='utf-8').splitlines())
            if r['event'] != 'request_started']


def render_report(plan: dict, records: list[dict], events: list[dict]) -> None:
    lines = ['# Gemini 10-question GSM8K pilot', '', f"Status: **{plan['status']}**.", '',
        '**This is a small preliminary experiment, not proof of either topology\'s superiority.**', '',
        '## Protocol and account checks', '',
        'Model: gemini-3.5-flash-lite, minimal thinking, 512-token output cap, temperature omitted. '
        'Four agents, three synchronous rounds; round 1 is independent. The same ten frozen '
        'validation questions are used for both topologies. Prompts use the existing builder; '
        'references are attached only after runtime inference. Each topology has separate initial '
        'calls. Topology order alternates by question to reduce order effects.', '',
        f"Dataset revision: `{plan['dataset_revision']}`. Question SHA-256: `{plan['question_sha256']}`.", '',
        'The user reconfirmed Free tier and disabled billing and read 1 used of 500 daily requests '
        '(499 remaining) before this run. This is the source of account verification; the generation '
        'API does not independently expose the billing ledger or project-wide remaining quota. '
        'No billing changes, paid routing, mock responses or MLP training are used.', '',
        'Safeguards: 12 RPM, 200,000 conservative input TPM, minimum 5.1 seconds between requests; '
        'at most 240 generation requests with no automatic retries and at least 20 daily requests '
        'reserved. The configured public/project limits were 15 RPM, 250,000 input TPM and 500 RPD. '
        'Unknown usage or quota/auth/billing/network restrictions stop the pilot. A known truncated '
        'or safety-blocked response aborts that question without inventing an answer; other planned '
        'questions may continue. No failed call is silently replayed.', '',
        '## Accuracy, tokens, latency and communication', '']
    if not records:
        lines += ['No question outcomes are available yet. No accuracy or latency results are asserted.', '']
    else:
        lines += ['| Topology | Complete / 10 | Correct / completed | Completed accuracy | Correct / 10 planned | '
                  'Input tokens | Output tokens (incl. thinking) | Known request latency (s) | Mean completed question inference (s) | Mean completed question wall (s) |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
        for topology in ('fully_connected', 'ring'):
            group = [r for r in records if r['topology'] == topology]
            complete = [r for r in group if r['status'] == 'completed']
            calls = [e for e in events if e['topology'] == topology]
            metered = [e['usage'] for e in calls if e.get('usage') is not None]
            unknown = sum(bool(e.get('usage_unknown')) for e in calls)
            correct = sum(bool(r.get('correct')) for r in complete)
            def token_total(*names):
                known = sum(sum(u.get(name, 0) for name in names) for u in metered)
                return f'{known} + unknown' if unknown else str(known)
            accuracy = f'{correct / len(complete):.0%}' if complete else 'N/A'
            avg_infer = f"{sum(r['agent_inference_latency'] for r in complete)/len(complete):.3f}" if complete else 'N/A'
            avg_wall = f"{sum(r['question_wall_seconds'] for r in complete)/len(complete):.3f}" if complete else 'N/A'
            lines.append(f'| {topology} | {len(complete)}/10 | {correct}/{len(complete)} | {accuracy} | '
                         f"{correct}/10 | {token_total('prompt_token_count')} | "
                         f"{token_total('candidates_token_count', 'thoughts_token_count')} | "
                         f"{sum(e.get('inference_latency') or 0. for e in calls):.3f} | {avg_infer} | {avg_wall} |")
        lines += ['', 'Totals include provider usage from failed/incomplete calls when available; unknown usage '
                  'is explicitly marked. Output includes thinking; cached input is not added twice. '
                  'Latency is client-observed API request time, not isolated server compute. Wall time '
                  'includes rate-limit pacing and logging, so it is much longer than request time. '
                  'Completed accuracy excludes incomplete/not-run questions; the planned denominator '
                  'is also shown to expose missingness. Failed requests are not numerical answers.', '']
    lines += ['| Topology | Configured active edges by round | Message deliveries per complete question | '
              'Observed message deliveries in completed questions |', '|---|---|---:|---:|']
    for topology, edges in [('fully_connected', 12), ('ring', 4)]:
        total = sum(row['active_edges'] for r in records if r['topology'] == topology
                    and r['status'] == 'completed' for row in r['trace'])
        lines.append(f'| {topology} | 0, {edges}, {edges} | {2*edges} | {total} |')
    lines += ['', 'Each directed edge delivers one previous-round response. With all ten questions complete, '
              'full uses 240 deliveries and ring 80 (three times fewer); these are graph-message counts, '
              'not token counts. Repeated peer text also contributes to provider input usage.', '',
              '## Per-question outcomes', '',
              '| Item | Topology | Status | Round votes (1 → 2 → 3) | Reference answer | Final correct |',
              '|---|---|---|---|---|---|']
    for r in records:
        votes = [majority_vote(a['answer'] for a in row['responses']) for row in r.get('trace', [])]
        lines.append(f"| {r['item_id']} | {r['topology']} | {r['status']} | {' → '.join(str(v) for v in votes)} | "
                     f"{extract_answer(r['ground_truth'])} | {r.get('correct') if r['status']=='completed' else 'N/A'} |")
    lines += ['', '## Agent answer changes', '',
              'These changes describe observed revisions after neighbor messages. Self-revision and '
              'sampling variation are alternative explanations; this design does not isolate causal '
              'communication effects. Initial responses are independently sampled in each topology.', '']
    changes = []
    for r in records:
        if r['status'] != 'completed':
            continue
        for before, after in zip(r['trace'], r['trace'][1:]):
            for old, new in zip(before['responses'], after['responses']):
                if old['answer'] == new['answer']:
                    continue
                peers = after['messages'][str(new['agent_id'])]
                change = {'item_id': r['item_id'], 'topology': r['topology'], 'round': after['round'],
                          'agent_id': new['agent_id'], 'before': old['answer'], 'after': new['answer'],
                          'peer_answers': [(m['agent_id'], m['answer']) for m in peers],
                          'after_correct': exact_match(new['answer'], r['ground_truth'])}
                changes.append(change)
                lines.append(f"- {r['item_id']}, {r['topology']}, agent {new['agent_id']}, round {after['round']}: "
                             f"**{old['answer']} → {new['answer']}**; received {change['peer_answers']}; "
                             f"revised answer matches reference: {change['after_correct']}.")
    if not changes:
        lines += ['No extracted agent-answer changes were observed in the completed records so far. '
                  'No example of an accuracy improvement is fabricated.']
    lines += ['', '## Individual responses, revisions and received messages', '']
    for r in records:
        lines += [f"### {r['item_id']} / {r['topology']} / {r['status']}", '', r.get('question', ''), '']
        for row in r.get('trace', []):
            lines += [f"Round {row['round']}: {row['active_edges']} edges; {row['edges']}.", '']
            for response in row['responses']:
                peers = row['messages'][str(response['agent_id'])]
                lines += [f"Agent {response['agent_id']}, received from {[m['agent_id'] for m in peers]}; "
                          f"answer {response['answer']}; {response['input_tokens']} input / {response['output_tokens']} "
                          f"output tokens; {response['latency']:.3f}s.", '',
                          '``````````text', response['text'], '``````````', '']
        if r['status'] != 'completed':
            lines += [f"Reason: {r.get('reason', 'Not executed after a prior fatal stop')}.", '']
            for e in events:
                if e['item_id'] == r['item_id'] and e['topology'] == r['topology']:
                    lines += [f"Call round {e['round']}, agent {e['agent_id']}, {e['event']}; "
                              f"usage unknown: {e.get('usage_unknown')}.", '', '``````````text',
                              e.get('output_text') or '[No returned visible text]', '``````````', '']
    lines += ['## Reproducibility, files and limitations', '',
              f"Run ID: `{plan['run_id']}`. Source SHA-256: `{plan['source_sha256']}`.", '',
              'Machine-readable records: gemini_10_question_pilot.jsonl (all 20 planned outcomes); '
              'gemini_10_question_pilot_calls.jsonl (durable call starts, prompts, received messages, '
              'outputs, usage and failures); gemini_10_question_pilot_plan.json (effective config, '
              'dataset/source hashes and preservation manifest); gemini_10_question_pilot_access.json '
              '(current human account/quota check); gemini_10_question_pilot_state.json (live/final state).', '',
              'Ten validation questions give accuracy changes in ten-percentage-point steps. These '
              'questions are a debugging/pilot subset, not untouched test data. Local seed fixes item '
              'selection, not Gemini sampling. No confidence interval or superiority claim is made. '
              'The 512-token cap and permissive numeric extractor can affect outcomes. Any claim of '
              'benefit needs repeated trials, matched budgets and a held-out evaluation. No MLP training '
              'or benchmark expansion occurs in this run. Prior logs/reports are preserved.', '']
    REPORT.write_text('\n'.join(lines), encoding='utf-8')


def prepare() -> None:
    if any(p.exists() for p in (PLAN, CALLS, OUTCOMES, STATE, REPORT)):
        raise ValueError('Pilot artifacts already exist; refusing overwrite or automatic replay.')
    questions, _, previous = frozen_items()
    config = yaml.safe_load(CONFIG.read_text(encoding='utf-8'))
    if config['model'] != 'gemini-3.5-flash-lite' or config.get('gemini_thinking_level') != 'minimal':
        raise ValueError('Unexpected model/thinking profile; use the tested configuration.')
    preserved = {str(p.relative_to(ROOT)): digest(p) for p in RESULTS.iterdir() if p.is_file() and p != ACCESS}
    config.update(agents=4, rounds=3, benchmark='gsm8k', split='validation', limit=10,
                  gemini_rate_limits={'rpm': 12, 'tpm': 200000, 'rpd': 240})
    plan = {'status': 'prepared_awaiting_execution', 'run_id': str(uuid.uuid4()),
            'created_at': datetime.now(timezone.utc).isoformat(), 'config': config,
            'dataset_revision': previous['dataset']['revision'], 'question_sha256': digest(QUESTIONS),
            'reference_sha256': digest(REFERENCES), 'source_sha256': sources_digest(),
            'profile_sha256': digest(CONFIG), 'max_generation_calls': 240,
            'minimum_request_spacing_seconds': 5.1, 'daily_reserve': 20,
            'preserved_artifact_sha256': preserved, 'item_ids': [q['item_id'] for q in questions],
            'versions': {p: importlib.metadata.version(p) for p in ('google-genai', 'networkx', 'numpy', 'datasets')}}
    PLAN.write_text(json.dumps(plan, indent=2), encoding='utf-8')
    render_report(plan, [], [])
    print('Prepared the same frozen ten questions; maximum 240 calls at 12 RPM / 200000 input TPM.', flush=True)


def execute() -> None:
    logging.disable(logging.CRITICAL)
    plan = json.loads(PLAN.read_text(encoding='utf-8'))
    access = json.loads(ACCESS.read_text(encoding='utf-8'))
    validate_access(access)
    if CALLS.exists() or OUTCOMES.exists():
        raise ValueError('Existing issued attempts/outcomes found; automatic replay is refused.')
    if sources_digest() != plan['source_sha256'] or digest(CONFIG) != plan['profile_sha256']:
        raise ValueError('Source/profile changed after preparation; review before any request.')
    questions, refs, _ = frozen_items()
    key = (dotenv_values(ROOT / '.env').get('GEMINI_API_KEY') or '').strip()
    if not key:
        raise ValueError('GEMINI_API_KEY is missing from the local .env file.')
    os.environ['GEMINI_API_KEY'] = key
    cfg = plan['config']
    raw = GeminiLLM(cfg['model'], cfg['max_output_tokens'], cfg['temperature'], cfg['timeout'],
                    cfg['gemini_thinking_budget'], True, cfg['gemini_rate_limits'],
                    thinking_level=cfg['gemini_thinking_level'])
    journal = JournalBackend(raw, CALLS, plan['run_id'], plan['minimum_request_spacing_seconds'])
    records, fatal, reason = [], False, None
    began = perf_counter()
    try:
        plan['model_metadata'] = raw.verify_model()
        plan['access_check'] = access
        for index, item in enumerate(questions):
            order = ('fully_connected', 'ring') if index % 2 == 0 else ('ring', 'fully_connected')
            for topology in order:
                record = {'run_id': plan['run_id'], 'item_id': item['item_id'], 'question': item['question'],
                          'topology': topology, 'split': 'validation', 'model': cfg['model'],
                          'dataset_revision': plan['dataset_revision'], 'seed': 42,
                          'status': 'not_run', 'simulated': False, 'ground_truth': refs[item['item_id']],
                          'correct': None, 'config': cfg, 'plan_sha256': digest(PLAN)}
                if not fatal:
                    journal.start_question(item['item_id'], topology)
                    start = perf_counter()
                    try:
                        result = run_question(item['question'], journal, count=4, rounds=3, topology=topology)
                        record.update(result, status='completed', correct=exact_match(result['prediction'], refs[item['item_id']]))
                    except PilotCallError as exc:
                        record.update(status='failed_or_incomplete', reason=str(exc))
                        fatal, reason = exc.fatal, str(exc)
                    except BaseException as exc:
                        record.update(status='failed_or_incomplete', reason=type(exc).__name__)
                        fatal, reason = True, type(exc).__name__
                    record['question_wall_seconds'] = perf_counter() - start
                else:
                    record['reason'] = reason
                records.append(record)
                append_jsonl(OUTCOMES, record)
                plan['status'] = 'running' if not fatal else 'halted_incomplete'
                plan['issued_generation_calls'] = raw.generation_calls_issued
                plan['elapsed_wall_seconds'] = perf_counter() - began
                STATE.write_text(json.dumps(plan, indent=2), encoding='utf-8')
                render_report(plan, records, journal.events)
                print(f"{item['item_id']} {topology}: {record['status']}; "
                      f"correct={record['correct']}; calls={raw.generation_calls_issued}/240", flush=True)
    finally:
        raw.close()
        plan['status'] = ('completed' if len(records) == 20 and all(r['status'] == 'completed' for r in records)
                          else 'finished_with_incomplete_questions')
        plan['issued_generation_calls'] = raw.generation_calls_issued
        plan['elapsed_wall_seconds'] = perf_counter() - began
        plan['preservation_check'] = {name: (ROOT / name).exists() and digest(ROOT / name) == expected
                                      for name, expected in plan['preserved_artifact_sha256'].items()}
        STATE.write_text(json.dumps(plan, indent=2), encoding='utf-8')
        render_report(plan, records, journal.events)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run', 'report'])
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.action == 'run':
        execute()
    else:
        plan = json.loads((STATE if STATE.exists() else PLAN).read_text(encoding='utf-8'))
        records = [json.loads(line) for line in OUTCOMES.read_text(encoding='utf-8').splitlines()] if OUTCOMES.exists() else []
        render_report(plan, records, read_events())


if __name__ == '__main__':
    try:
        main()
    except (ValueError, GeminiError) as exc:
        print(str(exc))
        raise SystemExit(1) from None
