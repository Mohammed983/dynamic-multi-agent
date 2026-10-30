"""Prepare/evaluate a four-strategy, round-1-only diagnostic. Live run needs approval.

prepare downloads public benchmark data only; preflight is entirely local; neither
constructs a Gemini client. run cannot proceed without a fresh explicit receipt.
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
import re
from time import perf_counter
import uuid

from dotenv import dotenv_values
import yaml

from experiments.gemini_pilot import JournalBackend, PilotCallError
from src.agents import Agent
from src.benchmarks import AQUARAT, GSM8K
from src.diagnostics import question_diagnostics, summarize_diagnostics
from src.evaluation import extract_for_format
from src.gemini_client import GeminiLLM
from src.pilot import append_jsonl
from src.roles import load_roles, parse_roles
from src.runner import run_question

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / 'configs/diagnostic_aqua.yaml'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_digest() -> str:
    files = sorted((ROOT / 'src').rglob('*.py')) + [Path(__file__), ROOT / 'experiments/gemini_pilot.py']
    return hashlib.sha256(b''.join(str(p.relative_to(ROOT)).encode() + b'\0' + p.read_bytes() for p in files)).hexdigest()


def settings(path: Path, benchmark_override: str | None = None) -> tuple[dict, dict[str, Path]]:
    cfg = yaml.safe_load(path.read_text(encoding='utf-8'))
    if benchmark_override is not None:
        cfg['benchmark'] = benchmark_override
        cfg['output_prefix'] = f'results/independent_diagnostic_{benchmark_override}'
    if (cfg['model'] != 'gemini-3.5-flash-lite' or cfg['backend'] != 'gemini'
            or cfg['agents'] != 4 or cfg['rounds'] != 1 or cfg['topology'] != 'empty'
            or cfg['limit'] != 20 or cfg['split'] not in {'train', 'validation'}
            or cfg['gemini_thinking_level'] != 'minimal' or cfg['max_retries'] != 0):
        raise ValueError('Diagnostic must use 20 train/validation items, four Gemini 3.5 agents, one independent round, no retries.')
    limits = cfg['gemini_rate_limits']
    if (set(limits) != {'rpm', 'tpm', 'rpd'} or any(type(v) is not int or v <= 0 for v in limits.values())
            or limits['rpm'] > 12 or limits['tpm'] > 200000 or limits['rpd'] != 80
            or cfg['minimum_request_spacing_seconds'] < 5.1):
        raise ValueError('Retain the conservative 12 RPM / 200000 input TPM / 80-request diagnostic caps.')
    if cfg['benchmark'] not in {'gsm8k', 'aqua_rat'}:
        raise ValueError('Diagnostic benchmark must be aqua_rat or gsm8k.')
    style = cfg.get('prompt_style', 'default')
    if style not in {'default', 'concise_option_v2'}:
        raise ValueError('Unknown diagnostic prompt style.')
    if style == 'concise_option_v2' and (cfg.get('experiment_version') != 2 or cfg['benchmark'] != 'aqua_rat'
            or cfg['split'] != 'validation' or cfg['max_output_tokens'] not in {2048, 4096}
            or not cfg.get('frozen_source_plan') or not cfg.get('model_metadata_source')
            or cfg['output_prefix'] == 'results/independent_diagnostic_aqua'):
        raise ValueError('Version 2 requires AQUA validation, a new prefix, frozen source and a supported higher cap.')
    base = ROOT / cfg['output_prefix']
    if base.parent.resolve() != (ROOT / 'results').resolve():
        raise ValueError('Use an output prefix directly inside this project results directory.')
    paths = {name: base.with_name(base.name + suffix) for name, suffix in {
        'plan': '_plan.json', 'questions': '_questions.jsonl', 'references': '_references.jsonl',
        'calls': '_calls.jsonl', 'records': '_results.jsonl', 'report': '_report.md',
        'summary': '_summary.json', 'state': '_state.json'}.items()}
    return cfg, paths


def benchmark(cfg: dict):
    source = cfg['datasets'][cfg['benchmark']]
    if not re.fullmatch(r'[0-9a-f]{40}', source['revision']):
        raise ValueError('Diagnostic data must be pinned to a full revision, not main.')
    if cfg['benchmark'] == 'aqua_rat':
        return AQUARAT(source['revision'], cfg.get('difficulty_selection'))
    return GSM8K(source['validation_fraction'], source['revision'], cfg.get('difficulty_selection'))


def prepare(cfg: dict, paths: dict[str, Path], config_path: Path) -> None:
    if any(path.exists() for path in paths.values()):
        raise ValueError('Diagnostic artifacts already exist; refuse overwrite/reselection or automatic replay.')
    roles_path = ROOT / cfg['roles_config']
    roles = load_roles(roles_path, 4)
    dataset = benchmark(cfg)
    reuse = {}
    if cfg.get('frozen_source_plan'):
        source = ROOT / cfg['frozen_source_plan']
        metadata_path = ROOT / cfg['model_metadata_source']
        if any(p.resolve().parent != (ROOT / 'results').resolve() for p in (source, metadata_path)):
            raise ValueError('Frozen source/metadata must be local results artifacts.')
        old = json.loads(source.read_text(encoding='utf-8'))
        for key in ('benchmark', 'split', 'limit', 'seed', 'agents', 'rounds', 'topology', 'roles_config',
                    'datasets', 'difficulty_selection', 'model', 'temperature', 'gemini_thinking_level'):
            if cfg[key] != old['config'][key]:
                raise ValueError(f'Frozen revision changed {key}; do not silently revise the cohort/roles.')
        if digest(roles_path) != old['roles_sha256'] or [asdict(role) for role in roles] != old['roles']:
            raise ValueError('Frozen role definitions changed.')
        old_questions = source.with_name(source.name.removesuffix('_plan.json') + '_questions.jsonl')
        old_refs = source.with_name(source.name.removesuffix('_plan.json') + '_references.jsonl')
        if digest(old_questions) != old['question_file_sha256'] or digest(old_refs) != old['reference_file_sha256']:
            raise ValueError('Frozen dataset hashes changed.')
        public = [json.loads(line) for line in old_questions.read_text().splitlines()]
        refs = [json.loads(line) for line in old_refs.read_text().splitlines()]
        if (len(public) != 20 or len({r['item_id'] for r in public}) != 20
                or len({r['question'] for r in public}) != 20 or len(refs) != 20
                or {r['item_id'] for r in public} != {r['item_id'] for r in refs}
                or any(r['split'] != 'validation' or 'reference' in r or 'rationale' in r for r in public)):
            raise ValueError('Invalid frozen cohort or runtime/evaluation separation.')
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))['model_metadata']
        if (metadata['name'].removeprefix('models/') != cfg['model']
                or 'generateContent' not in metadata['supported_actions']
                or type(metadata['output_token_limit']) is not int
                or cfg['max_output_tokens'] > metadata['output_token_limit']):
            raise ValueError('Output cap not supported by the stored model metadata.')
        reuse = {'frozen_source_plan_sha256': digest(source), 'frozen_source_plan': str(source),
                 'historical_model_metadata': metadata, 'model_metadata_source_sha256': digest(metadata_path),
                 'model_support_caveat': 'Stored metadata only; live metadata is rechecked after fresh approval.'}
        paths['questions'].write_bytes(old_questions.read_bytes())
        paths['references'].write_bytes(old_refs.read_bytes())
    else:
        # Public-data preparation only; no Gemini client or credential access.
        os.environ['HF_HOME'] = str(ROOT / 'data/hf_home')
        os.environ['HF_DATASETS_CACHE'] = str(ROOT / 'data/hf_datasets')
        os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
        items = dataset.load(cfg['split'], limit=20, seed=cfg['seed'])
        if len(items) != 20 or len({item.item_id for item in items}) != 20 or len({item.question for item in items}) != 20:
            raise ValueError('Exactly 20 distinct items are required; no duplicate or implicit replacement.')
        public = [{'item_id': item.item_id, 'question': item.question, 'split': item.split,
                   'provenance': item.provenance} for item in items]
        refs = [{'item_id': item.item_id, 'reference': item.reference} for item in items]
        paths['questions'].write_text(''.join(json.dumps(row) + '\n' for row in public), encoding='utf-8')
        paths['references'].write_text(''.join(json.dumps(row) + '\n' for row in refs), encoding='utf-8')
    plan = {'status': 'awaiting_explicit_approval', 'run_id': str(uuid.uuid4()),
            'created_at': datetime.now(timezone.utc).isoformat(), 'config': cfg,
            'config_sha256': digest(config_path), 'source_sha256': source_digest(),
            'roles': [asdict(role) for role in roles], 'roles_sha256': digest(roles_path),
            'question_file_sha256': digest(paths['questions']), 'reference_file_sha256': digest(paths['references']),
            'answer_format': dataset.answer_format, 'request_estimate': 80, 'generation_calls_issued': 0,
            'selection_method': 'Predeclared question-text characteristics only; no rationale, label or model outcomes.',
            'difficulty_caveat': 'Complexity proxy, not proven model difficulty. No claim that the ceiling effect is resolved.',
            'versions': {name: importlib.metadata.version(name) for name in ('google-genai', 'datasets', 'numpy', 'networkx')},
            **reuse}
    if cfg.get('prompt_style') == 'concise_option_v2':
        prompts = [Agent(i, None, role=roles[i], answer_format='option', prompt_style=cfg['prompt_style'])
                   .build_prompt(row['question'], ()) for row in public for i in range(4)]
        estimate = sum(len(prompt) for prompt in prompts) / 4
        plan['token_budget'] = {
            'input_estimate': round(estimate), 'input_estimate_range': [round(estimate * .65), round(estimate * 1.5)],
            'input_estimate_method': 'Local character count / 4; rough, not Gemini tokenization or observed usage.',
            'planned_output_estimate_range': [80 * 200, 80 * 500],
            'output_estimate_assumption': '200-500 tokens/call for concise mathematical explanations; not measured.',
            'maximum_output_tokens': 80 * cfg['max_output_tokens'],
            'output_cap_includes_thinking': True,
            'generation_requests': 80, 'metadata_reads_before_generation': 1}
    paths['plan'].write_text(json.dumps(plan, indent=2), encoding='utf-8')
    report(plan, [], [], paths)
    print(json.dumps({'status': plan['status'], 'prepared_questions': 20, 'requests_if_approved': 80,
                      'plan': str(paths['plan']), 'plan_sha256': digest(paths['plan']), 'gemini_calls_made': 0}, indent=2))


def preflight(cfg: dict, paths: dict[str, Path]) -> dict:
    """Read local evidence only. Do not mistake historical quota for current availability."""
    key_present = bool((dotenv_values(ROOT / '.env').get('GEMINI_API_KEY') or '').strip())
    previous = ROOT / 'results/gemini_10_question_pilot_summary.json'
    estimate = json.loads(previous.read_text()).get('quota_remaining_after_own_calls_estimate') if previous.exists() else None
    return {'gemini_api_calls_made': 0, 'local_key_present': key_present, 'current_model_api_access': 'not_rechecked_without_approval',
            'current_free_tier_and_billing': 'fresh_human_confirmation_required',
            'actual_remaining_daily_requests': 'unverified', 'historical_local_remaining_estimate': estimate,
            'historical_estimate_is_not_a_quota_check': True, 'requests_required': 80,
            'minimum_current_remaining_requests': 80 + cfg['daily_quota_reserve'],
            'configured_rate_limits': cfg['gemini_rate_limits'],
            'plan_present': paths['plan'].exists(), 'live_run_authorized': False}


def validate_approval(receipt: dict, plan_path: Path, cfg: dict, requests_required: int = 80) -> None:
    if type(requests_required) is not int or not 0 <= requests_required <= 80:
        raise ValueError('Invalid number of planned generation requests.')
    if (receipt.get('approved') is not True or receipt.get('plan_sha256') != digest(plan_path)
            or receipt.get('free_tier') is not True or receipt.get('billing_disabled') is not True):
        raise ValueError('Explicit human approval for this exact plan and confirmed free/billing-disabled project is required.')
    remaining = receipt.get('remaining_daily_requests')
    if type(remaining) is not int or remaining < requests_required + cfg['daily_quota_reserve']:
        raise ValueError('Need a fresh actual remaining-quota check covering 80 requests plus the reserve.')
    checked = datetime.fromisoformat(receipt['checked_at'])
    if not 0 <= (datetime.now(timezone.utc) - checked).total_seconds() <= 3600:
        raise ValueError('The operator quota/billing confirmation must be within the last hour.')


def report(plan: dict, records: list[dict], events: list[dict], paths: dict[str, Path]) -> None:
    minimum_quota = plan.get('new_requests_required', 80) + plan['config']['daily_quota_reserve']
    lines = ['# Independent reasoning diagnostic', '', f"Status: **{plan['status']}**.", '',
             'Only round 1 is allowed. Four role-specific agents receive the same question and no '
             'peer messages, previous response, rationale or reference answer. There are no communication '
             'edges. Role order is fixed and must be reused unchanged in later topology comparisons.', '',
             f"Benchmark: {plan['config']['benchmark']}; split: {plan['config']['split']}; "
             f"requested items: 20; planned generation requests: 80. Model: gemini-3.5-flash-lite, minimal thinking.", '',
             'Selection uses a pinned dataset, fixed seed, original split membership and the predeclared '
             'question-only complexity proxy. No topology/model outcomes, correct labels or rationales '
             'enter selection. Greater proxy score is not proof of greater mathematical difficulty.', '',
             '## Metrics and coverage', '']
    if plan['config'].get('prompt_style') == 'concise_option_v2':
        lines += [f"Revised protocol v2: {plan['config']['max_output_tokens']}-token output cap; concise role-specific "
                  'explanations and a strict final line `Final answer: X` (one uppercase A–E option). '
                  'Same frozen cohort and roles; all 80 calls would be new under these revised prompts. '
                  'No responses are reused from the prior protocol.', '']
    if not records:
        lines += ['**No Gemini inference has run.** Accuracy, unanimity, disagreement and error cases '
                  'are unavailable until an explicitly approved real diagnostic is completed.', '']
    else:
        summary = summarize_diagnostics(records, 20)
        paths['summary'].write_text(json.dumps(summary, indent=2), encoding='utf-8')
        lines += ['| Agent ID / role | Correct / 20 planned | Returned | Planned accuracy |', '|---|---:|---:|---:|']
        for row, role in zip(summary['individual_agents'], plan['roles']):
            percentage = f"{row['planned_accuracy']:.0%}" if row['planned_accuracy'] is not None else 'N/A'
            lines += [f"| {row['agent_id']} / {role['name']} | {row['correct']}/20 | {row['returned']} | {percentage} |"]
        majority = f"{summary['majority_accuracy_planned']:.0%}" if summary['majority_accuracy_planned'] is not None else 'N/A'
        lines += ['', f"Majority-vote accuracy: {summary['majority_correct']}/20 ({majority}).",
                  f"Unanimous agreement: {summary['unanimous_count']}/20 ({summary['unanimous_rate_planned']:.0%}).",
                  f"Disagreement: {summary['disagreement_count']}/20 ({summary['disagreement_rate_planned']:.0%}).",
                  f"Missing/unparseable category: {summary['missing_or_unparseable_count']}/20.", '',
                  'Primary accuracies use all 20 planned questions, exposing failures rather than silently '
                  'dropping them. Returned-answer accuracies and coverage are in the summary JSON. '
                  'Unanimity/disagreement require all four parsed answers; None values never count as '
                  'agreement. Pairwise agreement is recorded with unknown pairs marked null.', '',
                  '## Questions with at least one observed incorrect agent', '']
        ids = summary['items_with_observed_incorrect_agent']
        lines += [f'- {item_id}' for item_id in ids] if ids else ['None observed in the available records; unavailable answers are not invented errors.']
        lines += ['', '## Individual responses and correctness', '']
        for row in records:
            lines += [f"### {row['item_id']} / {row['status']}", '', row['question'], '']
            if row.get('reason'):
                lines += [f"Reason: {row['reason']}", '']
            if row.get('diagnostics'):
                lines += ['```json', json.dumps(row['diagnostics'], indent=2), '```', '']
            for event in events:
                if event['item_id'] == row['item_id']:
                    lines += [f"Agent {event['agent_id']}: {event['event']}; finish reason: {event.get('finish_reason')}; "
                              f"error: {event.get('error')}; usage unknown: {event.get('usage_unknown')}.", '',
                              '``````````text', event.get('output_text') or '[No returned text]', '``````````', '']
    lines += ['## Limits and next stage', '',
              'This is a validation/training diagnostic, not final test evaluation. Diverse strategy '
              'instructions encourage but do not guarantee independent errors or harder reasoning. '
              'AQUA options and alphabetic vote tie-breaking introduce chance/tie effects. The existing '
              'numeric vote tie rule is retained for GSM8K. Do not infer communication gains from these '
              'independent responses. Inspect unanimous errors as well as disagreements.', '',
              'Future communication experiments need a separate reviewed plan and frozen question/role '
              'manifests. Error-selected cohorts are exploratory and must not be presented as an unbiased '
              'held-out comparison. Rounds 2/3 and MLP training are never started by this command.', '',
              'Before any live Gemini request, provide explicit approval plus a fresh AI Studio check '
              f'for Free tier, billing disabled and at least {minimum_quota} daily requests remaining. The local '
              'preflight cannot certify account-wide remaining quota. No billing API or paid fallback '
              'is used. Quota/auth/network/usage failures stop without retry or mock substitution.', '']
    paths['report'].write_text('\n'.join(lines), encoding='utf-8')


def execute(cfg: dict, paths: dict[str, Path], config_path: Path, approval: Path | None) -> None:
    # Approval is checked before any backend construction, metadata GET or inference.
    if approval is None:
        raise ValueError('Run is locked: supply an explicit human-approved receipt with --approval-file.')
    plan = json.loads(paths['plan'].read_text(encoding='utf-8'))
    receipt = json.loads(approval.read_text(encoding='utf-8'))
    validate_approval(receipt, paths['plan'], cfg)
    if cfg != plan['config']:
        raise ValueError('Effective configuration differs from the frozen diagnostic plan.')
    if paths['calls'].exists() or paths['records'].exists():
        raise ValueError('Prior diagnostic attempts exist; no automatic replay or overwrite.')
    if (source_digest() != plan['source_sha256'] or digest(config_path) != plan['config_sha256']
            or digest(ROOT / cfg['roles_config']) != plan['roles_sha256']
            or digest(paths['questions']) != plan['question_file_sha256']
            or digest(paths['references']) != plan['reference_file_sha256']):
        raise ValueError('Approved config/source/roles/questions/references changed; prepare a reviewed plan.')
    roles = parse_roles(plan['roles'], 4)
    questions = [json.loads(line) for line in paths['questions'].read_text().splitlines()]
    refs = {row['item_id']: row['reference'] for row in
            (json.loads(line) for line in paths['references'].read_text().splitlines())}
    key = (dotenv_values(ROOT / '.env').get('GEMINI_API_KEY') or '').strip()
    if not key:
        raise ValueError('Missing local GEMINI_API_KEY.')
    os.environ['GEMINI_API_KEY'] = key
    raw = GeminiLLM(cfg['model'], cfg['max_output_tokens'], cfg['temperature'], cfg['timeout'],
                    cfg['gemini_thinking_budget'], True, cfg['gemini_rate_limits'],
                    thinking_level=cfg['gemini_thinking_level'])
    backend = JournalBackend(raw, paths['calls'], plan['run_id'], cfg['minimum_request_spacing_seconds'],
                             answer_format=plan['answer_format'], prompt_style=cfg.get('prompt_style', 'default'))
    evaluator = benchmark(cfg)
    records, stopped = [], False
    try:
        plan['model_metadata'] = raw.verify_model()  # First real Gemini call only after approved free-access receipt.
        for item in questions:
            row = {**item, 'status': 'not_run', 'diagnostics': None, 'reference': refs[item['item_id']],
                   'run_id': plan['run_id'], 'simulated': False, 'plan_sha256': digest(paths['plan'])}
            if not stopped:
                backend.start_question(item['item_id'], 'empty')
                before = len(backend.events)
                start = perf_counter()
                try:
                    result = run_question(item['question'], backend, count=4, rounds=1, topology='empty',
                                          roles=roles, answer_format=plan['answer_format'],
                                          prompt_style=cfg.get('prompt_style', 'default'))
                    row.update(result, status='completed')
                except BaseException as exc:
                    row.update(status='failed_or_incomplete', reason=str(exc) if isinstance(exc, PilotCallError) else type(exc).__name__)
                    stopped = True
                row['wall_seconds'] = perf_counter() - start
                current = backend.events[before:]
                completed = {event['agent_id']: event for event in current if event['event'] == 'call_completed'}
                answers = [extract_for_format(completed[i]['output_text'], plan['answer_format'],
                            prompt_style=cfg.get('prompt_style', 'default')) if i in completed else None for i in range(4)]
                row['diagnostics'] = question_diagnostics(answers, refs[item['item_id']], plan['answer_format'],
                                                         evaluator.evaluate, [i in completed for i in range(4)])
            records.append(row)
            append_jsonl(paths['records'], row)
            plan.update(status='halted_incomplete' if stopped else 'running', generation_calls_issued=raw.generation_calls_issued)
            paths['state'].write_text(json.dumps(plan, indent=2), encoding='utf-8')
            report(plan, records, backend.events, paths)
            print(f"{item['item_id']}: {row['status']}; requests={raw.generation_calls_issued}/80", flush=True)
    finally:
        raw.close()
        plan.update(status='completed' if len(records) == 20 and not stopped else 'halted_incomplete',
                    generation_calls_issued=raw.generation_calls_issued)
        paths['state'].write_text(json.dumps(plan, indent=2), encoding='utf-8')
        report(plan, records, backend.events, paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'preflight', 'run', 'report'])
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--benchmark', choices=['aqua_rat', 'gsm8k'])
    parser.add_argument('--approval-file', type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    cfg, paths = settings(args.config, args.benchmark)
    if args.action == 'prepare':
        prepare(cfg, paths, args.config)
    elif args.action == 'preflight':
        print(json.dumps(preflight(cfg, paths), indent=2))
    elif args.action == 'run':
        execute(cfg, paths, args.config, args.approval_file)
    else:
        plan = json.loads((paths['state'] if paths['state'].exists() else paths['plan']).read_text())
        rows = [json.loads(line) for line in paths['records'].read_text().splitlines()] if paths['records'].exists() else []
        events = [e for e in (json.loads(line) for line in paths['calls'].read_text().splitlines())
                  if e['event'] != 'request_started'] if paths['calls'].exists() else []
        report(plan, rows, events, paths)


if __name__ == '__main__':
    try:
        main()
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from None
