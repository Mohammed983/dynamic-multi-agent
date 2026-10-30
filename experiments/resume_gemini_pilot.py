"""One reviewed HTTP-504 recovery, reusing verified real prefix responses.

Not an automatic retry loop: refuses any history except the single diagnosed
gateway timeout and retains every original event/outcome. Uses at most 241 total
attempts for the original 240-response experiment, within confirmed free quota.
"""
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
from time import perf_counter
import uuid

from dotenv import dotenv_values

from experiments import gemini_pilot as pilot
from src.evaluation import exact_match
from src.gemini_client import GeminiLLM
from src.llm_client import Completion
from src.pilot import append_jsonl
from src.runner import run_question


def latest_outcomes(rows: list[dict]) -> list[dict]:
    latest = {}
    for row in rows:
        latest[(row['item_id'], row['topology'])] = row
    return list(latest.values())


class RecoveryJournal(pilot.JournalBackend):
    def __init__(self, *args, previous_attempts: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.previous_attempts = previous_attempts

    def generate(self, prompt):
        if self.previous_attempts + self.raw.generation_calls_issued >= 241:
            raise pilot.PilotCallError('Reviewed 241-attempt total limit reached; no request sent.')
        return super().generate(prompt)


class RealPrefixReplay:
    """Replay only byte-verified, previously completed real responses; never synthesize."""
    simulated = False

    def __init__(self, delegate: RecoveryJournal, prefix: list[dict], failed: dict | None):
        self.delegate, self.model, self.prefix = delegate, delegate.model, prefix
        self.failed, self.index, self.retry_sent = failed, 0, False

    def generate(self, prompt: str):
        if self.index < len(self.prefix):
            event = self.prefix[self.index]
            expected = (self.index // 4 + 1, self.index % 4)
            if (event['event'] != 'call_completed' or event['prompt'] != prompt
                    or (event['round'], event['agent_id']) != expected):
                raise pilot.PilotCallError('Real-prefix identity/prompt validation failed; no new request sent.')
            usage = event['usage']
            completion = Completion(event['output_text'], usage['prompt_token_count'],
                usage['candidates_token_count'] + usage['thoughts_token_count'], event['inference_latency'])
            self.delegate.completed[expected] = {
                'agent_id': event['agent_id'], 'round': event['round'], 'text': completion.text,
                'answer': event['answer'], 'input_tokens': completion.input_tokens,
                'output_tokens': completion.output_tokens, 'latency': completion.latency}
            self.index += 1
            self.delegate.index += 1
            return completion
        if self.failed is not None and not self.retry_sent:
            if prompt != self.failed['prompt']:
                raise pilot.PilotCallError('Retry prompt differs from the frozen failed request; refused.')
            self.delegate.context['retry_of_call_id'] = self.failed['call_id']
            self.retry_sent = True
        try:
            return self.delegate.generate(prompt)
        finally:
            self.delegate.context.pop('retry_of_call_id', None)


def recovery_report(state: dict, records: list[dict], events: list[dict]) -> None:
    pilot.render_report(state, records, events)
    body = pilot.REPORT.read_text(encoding='utf-8')
    body = body.replace('at most 240 generation requests with no automatic retries',
        'at most 241 total attempts for 240 requested successful responses, with no SDK automatic retries')
    body += ('\n## Reviewed gateway-timeout recovery\n\n'
        'The initial segment stopped at attempt 46 on HTTP 504 during the second question, '
        'fully connected graph, round 3, agent 1. The 60-second gateway timeout returned no '
        'usage or solution. It remains recorded as an unknown-usage failed attempt, not zero '
        'tokens. No quota or billing error was observed.\n\n'
        'After diagnosing this transport failure, the run was continued under the same explicit '
        'free-tier authorization and settings. The nine previously completed real responses for '
        'that question were reused from their saved call records, with exact prompt/identity checks. '
        'Only the timed-out request was retried; the prefix was not regenerated or replaced with '
        'mock content. The existing synchronous runner rebuilt the same prior-round inboxes. '
        'One extra attempt increases the ceiling to 241, safely below the confirmed 499 remaining '
        'daily requests. The SDK still performs one attempt per invocation.\n\n'
        'Raw outcome JSONL retains initial failed/not-run snapshots followed by explicit revisions; '
        'use gemini_10_question_pilot_final.jsonl for the latest unique 20 question/topology outcomes. '
        'Do not average the raw revision rows as independent runs. The call journal is append-only '
        'and keeps the failed request alongside its linked retry. Initial report/state snapshots '
        'are preserved as *_initial.md/json. A recovery manifest records hashes and the new segment.\n\n'
        'Question wall times for the recovered question include both active execution segments '
        'but exclude the manual diagnostic pause. Agent inference latencies retain the original '
        'successful API durations, not replay-processing speed. Request-latency totals additionally '
        'include the failed timeout; token totals remain known provider counts plus one unknown '
        'failed-attempt usage. No exact total including that failed usage is invented.\n')
    pilot.REPORT.write_text(body, encoding='utf-8')


def main() -> None:
    logging.disable(logging.CRITICAL)
    access = json.loads(pilot.ACCESS.read_text(encoding='utf-8'))
    pilot.validate_access(access)
    plan = json.loads(pilot.PLAN.read_text(encoding='utf-8'))
    old_state = json.loads(pilot.STATE.read_text(encoding='utf-8'))
    events = pilot.read_events()
    rows = [json.loads(line) for line in pilot.OUTCOMES.read_text(encoding='utf-8').splitlines()]
    records = latest_outcomes(rows)
    failures = [e for e in events if e['event'] != 'call_completed']
    recovery_path = pilot.RESULTS / 'gemini_10_question_pilot_recovery.json'
    if recovery_path.exists():
        raise ValueError('A reviewed recovery already exists; refusing another automatic recovery.')
    if (len(failures) != 1 or failures[0].get('http_status') != 504
            or not failures[0].get('usage_unknown') or len(events) != 46
            or old_state['issued_generation_calls'] != 46):
        raise ValueError('History differs from the diagnosed single HTTP 504; refusing recovery.')
    if pilot.sources_digest() != plan['source_sha256'] or pilot.digest(pilot.CONFIG) != plan['profile_sha256']:
        raise ValueError('Original runtime/profile changed; refusing unreviewed recovery.')
    if access['remaining_daily_requests'] - 241 < 20:
        raise ValueError('Insufficient confirmed quota including recovery and daily reserve.')
    questions, refs, _ = pilot.frozen_items()
    failure = failures[0]
    key_pair = (failure['item_id'], failure['topology'])
    prefix = [e for e in events if (e['item_id'], e['topology']) == key_pair and e['event'] == 'call_completed']
    if len(prefix) != 9:
        raise ValueError('Expected nine complete real prefix responses; refusing regeneration.')
    initial_report = pilot.RESULTS / 'gemini_10_question_pilot_initial.md'
    initial_state = pilot.RESULTS / 'gemini_10_question_pilot_initial.json'
    shutil.copy2(pilot.REPORT, initial_report)
    shutil.copy2(pilot.STATE, initial_state)
    manifest = {'status': 'running', 'segment_id': str(uuid.uuid4()),
        'started_at': datetime.now(timezone.utc).isoformat(), 'reason': 'Reviewed HTTP 504 transport recovery',
        'original_plan_sha256': pilot.digest(pilot.PLAN), 'initial_journal_sha256': pilot.digest(pilot.CALLS),
        'initial_outcomes_sha256': pilot.digest(pilot.OUTCOMES), 'helper_sha256': pilot.digest(Path(__file__)),
        'previous_attempts': 46, 'max_total_attempts': 241, 'failed_call_id': failure['call_id'],
        'reused_real_call_ids': [e['call_id'] for e in prefix]}
    recovery_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    key = (dotenv_values(pilot.ROOT / '.env').get('GEMINI_API_KEY') or '').strip()
    if not key:
        raise ValueError('Missing local Gemini key.')
    os.environ['GEMINI_API_KEY'] = key
    cfg = plan['config']
    raw = GeminiLLM(cfg['model'], cfg['max_output_tokens'], cfg['temperature'], cfg['timeout'],
        cfg['gemini_thinking_budget'], True, {**cfg['gemini_rate_limits'], 'rpd': 195},
        thinking_level=cfg['gemini_thinking_level'])
    journal = RecoveryJournal(raw, pilot.CALLS, plan['run_id'], interval=5.1, previous_attempts=46)
    journal.events = list(events)
    state = {**old_state, 'status': 'running_recovery', 'recovery': manifest}
    fatal, reason, started = False, None, perf_counter()
    try:
        raw.verify_model()
        for index, old in enumerate(records):
            if old['status'] == 'completed':
                continue
            item = next(q for q in questions if q['item_id'] == old['item_id'])
            record = {**old, 'outcome_revision': 2, 'supersedes_status': old['status']}
            if not fatal:
                journal.start_question(item['item_id'], old['topology'])
                journal.context['continuation_segment_id'] = manifest['segment_id']
                replay = RealPrefixReplay(journal, prefix if (item['item_id'], old['topology']) == key_pair else [],
                                         failure if (item['item_id'], old['topology']) == key_pair else None)
                before = perf_counter()
                try:
                    result = run_question(item['question'], replay, count=4, rounds=3, topology=old['topology'])
                    record.update(result, status='completed', correct=exact_match(result['prediction'], refs[item['item_id']]))
                    record.pop('reason', None)
                except pilot.PilotCallError as exc:
                    record.update(status='failed_or_incomplete', reason=str(exc))
                    fatal, reason = exc.fatal, str(exc)
                except BaseException as exc:
                    record.update(status='failed_or_incomplete', reason=type(exc).__name__)
                    fatal, reason = True, type(exc).__name__
                record['question_wall_seconds'] = perf_counter() - before
                if (item['item_id'], old['topology']) == key_pair:
                    record['question_wall_seconds'] += old['question_wall_seconds']
                    record['reused_real_prefix_call_ids'] = manifest['reused_real_call_ids']
                    record['reviewed_retry_of_call_id'] = failure['call_id']
                    record['latency'] = record['question_wall_seconds']
            else:
                record.update(status='not_run', reason=reason)
            records[index] = record
            append_jsonl(pilot.OUTCOMES, record)
            state.update(status='running_recovery' if not fatal else 'halted_incomplete',
                issued_generation_calls=46 + raw.generation_calls_issued,
                elapsed_wall_seconds=old_state['elapsed_wall_seconds'] + perf_counter() - started)
            pilot.STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')
            recovery_report(state, records, journal.events)
            print(f"{item['item_id']} {old['topology']}: {record['status']}; correct={record['correct']}; "
                  f"total attempts={46+raw.generation_calls_issued}/241", flush=True)
    finally:
        raw.close()
        state.update(status='completed' if all(r['status'] == 'completed' for r in records) else 'finished_with_incomplete_questions',
            issued_generation_calls=46 + raw.generation_calls_issued,
            elapsed_wall_seconds=old_state['elapsed_wall_seconds'] + perf_counter() - started)
        state['preservation_check'] = {name: (pilot.ROOT / name).exists() and pilot.digest(pilot.ROOT / name) == expected
                                      for name, expected in plan['preserved_artifact_sha256'].items()}
        pilot.STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')
        final = pilot.RESULTS / 'gemini_10_question_pilot_final.jsonl'
        final.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
        manifest.update(status=state['status'], total_attempts=state['issued_generation_calls'])
        recovery_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        recovery_report(state, records, journal.events)


if __name__ == '__main__':
    try:
        main()
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from None
