"""Explicit recovery of unattempted independent slots, never retries recorded slots.

prepare is entirely offline. run requires a new approval for the recovery manifest.
Parent logs are immutable. An incomplete recorded attempt stays unavailable.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from time import perf_counter, sleep
import uuid

from dotenv import dotenv_values

from experiments import run_diagnostic as diagnostic
from experiments.gemini_pilot import JournalBackend, PilotCallError
from src.agents import Agent
from src.diagnostics import question_diagnostics
from src.evaluation import extract_for_format
from src.gemini_client import GeminiLLM
from src.pilot import append_jsonl
from src.roles import parse_roles

ROOT = diagnostic.ROOT


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def recovery_source_digest() -> str:
    return hashlib.sha256((diagnostic.source_digest() + diagnostic.digest(Path(__file__))).encode()).hexdigest()


def recovery_paths(parent: dict[str, Path]) -> dict[str, Path]:
    """Use a new sibling prefix; never overwrite or append to parent artifacts."""
    suffixes = {'plan':'_plan.json', 'questions':'_questions.jsonl', 'references':'_references.jsonl',
                'calls':'_calls.jsonl', 'records':'_results.jsonl', 'report':'_report.md',
                'summary':'_summary.json', 'state':'_state.json'}
    return {key: path.with_name(path.name[:-len(suffixes[key])] + '_recovery' + suffixes[key])
            for key, path in parent.items()}


def inspect_parent(cfg: dict, paths: dict[str, Path], config_path: Path) -> tuple[dict, list[dict], list[dict], tuple]:
    """Validate exact prompt/config binding and durable attempt pairing, offline."""
    plan = json.loads(paths['plan'].read_text(encoding='utf-8'))
    if cfg != plan['config'] or diagnostic.digest(config_path) != plan['config_sha256']:
        raise ValueError('Original methodology/configuration changed; do not silently revise it during recovery.')
    for key, expected in [('questions', 'question_file_sha256'), ('references', 'reference_file_sha256')]:
        if diagnostic.digest(paths[key]) != plan[expected]:
            raise ValueError('Frozen dataset changed.')
    if diagnostic.digest(ROOT / cfg['roles_config']) != plan['roles_sha256']:
        raise ValueError('Frozen roles changed.')
    questions = read_rows(paths['questions'])
    if len(questions) != 20 or len({q['item_id'] for q in questions}) != 20:
        raise ValueError('Expected twenty frozen distinct items.')
    by_id = {q['item_id']: q for q in questions}
    roles = parse_roles(plan['roles'], 4)
    events = read_rows(paths['calls'])
    starts, terminals, slots = {}, {}, {}
    for event in events:
        call = event['call_id']
        slot = (event['item_id'], event['agent_id'])
        if (event['item_id'] not in by_id or type(event['agent_id']) is not int or not 0 <= event['agent_id'] < 4
                or event['round'] != 1 or event['topology'] != 'empty' or event['active_edges'] != 0
                or event['graph_edges'] or event['messages_received'] or event['model'] != cfg['model']
                or event['run_id'] != plan['run_id']):
            raise ValueError('Journal context does not match independent diagnostic.')
        expected = Agent(event['agent_id'], None, role=roles[event['agent_id']],
                         answer_format=plan['answer_format']).build_prompt(by_id[event['item_id']]['question'], ())
        if event['prompt'] != expected:
            raise ValueError('Recorded prompt differs from frozen role/question; no replay or replacement.')
        if event['event'] == 'request_started':
            if call in starts or slot in slots:
                raise ValueError('Duplicate recorded attempt; manual review required.')
            starts[call], slots[slot] = event, event
        elif event['event'] in {'call_completed', 'call_failed', 'call_incomplete'}:
            if call not in starts or call in terminals or slot != (starts[call]['item_id'], starts[call]['agent_id']):
                raise ValueError('Unpaired or duplicate terminal event; manual review required.')
            if event['event'] == 'call_completed' and (event.get('finish_reason') != 'STOP'
                    or not event.get('usage') or not event.get('output_text')):
                raise ValueError('Completed event lacks validated response/usage.')
            terminals[call] = event
        else:
            raise ValueError('Unknown journal event.')
    if not starts or set(starts) != set(terminals) or len(starts) > 80:
        raise ValueError('Unresolved request starts or invalid attempt count; manual reconciliation required.')
    state = json.loads(paths['state'].read_text(encoding='utf-8'))
    if state['status'] != 'halted_incomplete' or state['generation_calls_issued'] != sum(bool(e.get('issued')) for e in terminals.values()):
        raise ValueError('Parent state is not a reconciled incomplete run.')
    return plan, questions, list(terminals.values()), roles


def prepare(cfg: dict, parent: dict[str, Path], config_path: Path) -> dict:
    paths = recovery_paths(parent)
    if any(p.exists() for p in paths.values()):
        raise ValueError('Recovery artifacts exist; no overwrite or replay permitted.')
    plan, questions, events, roles = inspect_parent(cfg, parent, config_path)
    attempted = {(e['item_id'], e['agent_id']) for e in events}
    pending = [{'item_id': q['item_id'], 'agent_id': i} for q in questions for i in range(4)
               if (q['item_id'], i) not in attempted]
    if not pending:
        raise ValueError('No unattempted slots remain. Recorded failures are never retried.')
    preserved = {str(p.relative_to(ROOT)): diagnostic.digest(p) for p in parent.values() if p.exists()}
    manifest = {**plan, 'status': 'recovery_awaiting_approval', 'run_id': str(uuid.uuid4()),
        'parent_run_id': plan['run_id'], 'parent_plan_sha256': diagnostic.digest(parent['plan']),
        'parent_artifacts_sha256': preserved, 'parent_source_sha256': plan['source_sha256'],
        'source_sha256': recovery_source_digest(), 'created_at': datetime.now(timezone.utc).isoformat(),
        'recorded_attempts': len(events), 'recorded_completed': sum(e['event'] == 'call_completed' for e in events),
        'recorded_incomplete_or_failed': sum(e['event'] != 'call_completed' for e in events),
        'new_requests_required': len(pending), 'pending_slots': pending, 'generation_calls_issued': 0,
        'recovery_policy': 'Reuse recorded STOP responses; never retry any recorded slot. Incomplete responses remain unavailable. Stop on any new failure.',
        'roles': [asdict(role) for role in roles]}
    with paths['plan'].open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'status': manifest['status'], 'recorded_attempts': len(events),
        'new_requests_required': len(pending), 'minimum_fresh_remaining_quota': len(pending) + cfg['daily_quota_reserve'],
        'plan': str(paths['plan']), 'plan_sha256': diagnostic.digest(paths['plan']), 'gemini_calls_made': 0}, indent=2))
    return manifest


def execute(cfg: dict, parent: dict[str, Path], config_path: Path, approval: Path | None) -> None:
    paths = recovery_paths(parent)
    if approval is None:
        raise ValueError('Recovery locked: new explicit approval and fresh quota/billing receipt required.')
    manifest = json.loads(paths['plan'].read_text(encoding='utf-8'))
    receipt = json.loads(approval.read_text(encoding='utf-8'))
    diagnostic.validate_approval(receipt, paths['plan'], cfg, manifest['new_requests_required'])
    if manifest['source_sha256'] != recovery_source_digest():
        raise ValueError('Recovery source changed; review a new plan.')
    for name, checksum in manifest['parent_artifacts_sha256'].items():
        if diagnostic.digest(ROOT / name) != checksum:
            raise ValueError('Parent artifacts changed after recovery preparation.')
    if any(paths[k].exists() for k in ('calls', 'records', 'state', 'summary', 'report')):
        raise ValueError('Recovery already attempted; automatic replay refused.')
    plan, questions, events, roles = inspect_parent(cfg, parent, config_path)
    attempted = {(e['item_id'], e['agent_id']): e for e in events}
    pending = [{'item_id': q['item_id'], 'agent_id': i} for q in questions for i in range(4)
               if (q['item_id'], i) not in attempted]
    if pending != manifest['pending_slots'] or len(pending) != manifest['new_requests_required']:
        raise ValueError('Unattempted slots no longer match approval.')
    key = (dotenv_values(ROOT / '.env').get('GEMINI_API_KEY') or '').strip()
    if not key:
        raise ValueError('Missing local GEMINI_API_KEY.')
    os.environ['GEMINI_API_KEY'] = key
    # No retries and no budget increase: parent attempts + new attempts <= 80.
    raw = GeminiLLM(cfg['model'], cfg['max_output_tokens'], cfg['temperature'], cfg['timeout'],
        cfg['gemini_thinking_budget'], True, {**cfg['gemini_rate_limits'], 'rpd': len(pending)},
        thinking_level=cfg['gemini_thinking_level'])
    backend = JournalBackend(raw, paths['calls'], manifest['run_id'], cfg['minimum_request_spacing_seconds'],
                             answer_format=plan['answer_format'])
    evaluator = diagnostic.benchmark(cfg)
    refs = {r['item_id']: r['reference'] for r in read_rows(parent['references'])}
    rows, stopped = [], False
    try:
        manifest['model_metadata'] = raw.verify_model()
        # Conservatively separate sessions' minute windows; the session-local
        # limiter otherwise cannot see parent traffic that just occurred.
        latest = max(datetime.fromisoformat(e['timestamp']) for e in events)
        wait = max(0., 60. - (datetime.now(timezone.utc) - latest).total_seconds())
        while wait > 0:
            step = min(wait, 30.)
            sleep(step)
            wait -= step
        for q in questions:
            start = perf_counter()
            slots = {i: attempted[(q['item_id'], i)] for i in range(4) if (q['item_id'], i) in attempted}
            reason = None
            backend.start_question(q['item_id'], 'empty')
            for i in range(4):
                if i in slots or stopped:
                    continue
                backend.index = i  # Exact slot ID despite reused/unavailable earlier slots.
                before = len(backend.events)
                try:
                    Agent(i, backend, role=roles[i], answer_format=plan['answer_format']).reason(q['question'], 1, ())
                except BaseException as exc:
                    reason = str(exc) if isinstance(exc, PilotCallError) else type(exc).__name__
                    stopped = True
                if len(backend.events) > before:
                    slots[i] = backend.events[-1]
            completed = {i:e for i,e in slots.items() if e['event'] == 'call_completed'}
            answers = [extract_for_format(completed[i]['output_text'], plan['answer_format']) if i in completed else None for i in range(4)]
            status = 'completed' if len(completed) == 4 else ('failed_or_incomplete' if slots else 'not_run')
            row = {**q, 'run_id': manifest['run_id'], 'parent_run_id': plan['run_id'], 'simulated': False,
                'status': status, 'reference': refs[q['item_id']], 'reason': reason,
                'diagnostics': question_diagnostics(answers, refs[q['item_id']], plan['answer_format'],
                    evaluator.evaluate, [i in completed for i in range(4)]),
                'rounds': 1, 'agents': 4, 'topology': 'empty', 'active_edges': 0,
                'responses': [{'agent_id':i, 'source_call_id':e['call_id'], 'source_run_id':e['run_id'],
                    'text':e.get('output_text'), 'event':e['event'], 'usage':e.get('usage'),
                    'inference_latency':e.get('inference_latency'), 'finish_reason':e.get('finish_reason')}
                    for i,e in sorted(slots.items())], 'wall_seconds_this_recovery':perf_counter()-start,
                'plan_sha256':diagnostic.digest(paths['plan'])}
            rows.append(row)
            append_jsonl(paths['records'], row)
        manifest['status'] = 'recovery_halted' if stopped else 'recovery_finished_with_recorded_missing_responses'
    finally:
        raw.close()
        manifest.setdefault('status', 'recovery_halted')
        if manifest['status'] == 'recovery_awaiting_approval':
            manifest['status'] = 'recovery_halted'
        manifest['generation_calls_issued'] = raw.generation_calls_issued
        manifest['cumulative_recorded_attempts'] = len(events) + raw.generation_calls_issued
        paths['state'].write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        diagnostic.report(manifest, rows, events + backend.events, paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run'])
    parser.add_argument('--config', type=Path, default=diagnostic.DEFAULT_CONFIG)
    parser.add_argument('--approval-file', type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    cfg, parent = diagnostic.settings(args.config)
    if args.action == 'prepare':
        prepare(cfg, parent, args.config)
    else:
        execute(cfg, parent, args.config, args.approval_file)


if __name__ == '__main__':
    try:
        main()
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from None
