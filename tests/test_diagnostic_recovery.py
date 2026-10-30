"""Recovery regression fixtures use local responses only, never the real API."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
import yaml

from experiments import recover_diagnostic as recovery
from experiments import run_diagnostic as diagnostic
from experiments.gemini_pilot import JournalBackend, PilotCallError
from src.agents import Agent
from src.evaluation import option_exact_match
from src.gemini_client import GeminiError
from src.llm_client import Completion
from src.roles import load_roles


class FakeRaw:
    model, _key = 'gemini-3.5-flash-lite', 'offline-recovery-key'
    def __init__(self, fail_at=None):
        self.fail_at, self.generation_calls_issued = fail_at, 0
        self.usage_records, self.prompts, self.last_response, self.closed = [], [], None, False
    def generate(self, prompt):
        self.prompts.append(prompt)
        self.generation_calls_issued += 1
        usage = {'prompt_token_count':10, 'candidates_token_count':5, 'thoughts_token_count':0, 'total_token_count':15}
        if self.generation_calls_issued == self.fail_at:
            self.last_response = {'text':'Unfinished local fixture', 'latency':.1,
                                  'usage':usage, 'finish_reason':'MAX_TOKENS'}
            raise GeminiError('Offline truncated fixture; finish_reason=MAX_TOKENS')
        self.usage_records.append({**usage, 'finish_reason':'STOP'})
        return Completion('Local fixture. #### A', 10, 5, .1)
    def verify_model(self):
        return {'name':self.model}
    def close(self):
        self.closed = True


@pytest.fixture
def parent(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    cfgfile = tmp_path / 'configs/diagnostic_aqua.yaml'
    cfgfile.parent.mkdir()
    cfgfile.write_bytes((root / 'configs/diagnostic_aqua.yaml').read_bytes())
    (cfgfile.parent / 'diverse_roles.yaml').write_bytes((root / 'configs/diverse_roles.yaml').read_bytes())
    cfg = yaml.safe_load(cfgfile.read_text())
    roles = load_roles(root / 'configs/diverse_roles.yaml')
    resultdir = tmp_path / 'results'
    resultdir.mkdir()
    paths = {k: resultdir / ('independent_diagnostic_aqua' + s) for k,s in {
        'plan':'_plan.json','questions':'_questions.jsonl','references':'_references.jsonl',
        'calls':'_calls.jsonl','records':'_results.jsonl','report':'_report.md',
        'summary':'_summary.json','state':'_state.json'}.items()}
    items = [{'item_id':f'fixture:{i}', 'question':f'Public fixture question {i}.\nA) 1 B) 2 C) 3 D) 4 E) 5',
              'split':'validation'} for i in range(20)]
    paths['questions'].write_text(''.join(json.dumps(i)+'\n' for i in items))
    paths['references'].write_text(''.join(json.dumps({'item_id':i['item_id'],'reference':'A'})+'\n' for i in items))
    plan = {'config':cfg,'config_sha256':diagnostic.digest(cfgfile),'source_sha256':'parent-source',
        'roles_sha256':diagnostic.digest(tmp_path / cfg['roles_config']),
        'question_file_sha256':diagnostic.digest(paths['questions']),
        'reference_file_sha256':diagnostic.digest(paths['references']),
        'roles':[{'name':r.name,'instruction':r.instruction} for r in roles],
        'run_id':'parent-fixture', 'answer_format':'option'}
    paths['plan'].write_text(json.dumps(plan))
    backend = JournalBackend(FakeRaw(fail_at=2),paths['calls'],plan['run_id'],0,answer_format='option')
    backend.start_question(items[0]['item_id'],'empty')
    Agent(0,backend,role=roles[0],answer_format='option').reason(items[0]['question'],1,())
    with pytest.raises(PilotCallError):
        Agent(1,backend,role=roles[1],answer_format='option').reason(items[0]['question'],1,())
    # Old timestamps ensure that offline fixtures do not wait for minute pacing.
    events = recovery.read_rows(paths['calls'])
    for e in events:
        e['timestamp'] = '2020-01-01T00:00:00+00:00'
    paths['calls'].write_text(''.join(json.dumps(e)+'\n' for e in events))
    paths['state'].write_text(json.dumps({'status':'halted_incomplete','generation_calls_issued':2}))
    for key in ('records','report','summary'):
        paths[key].write_text('Original fixture artifact; never overwrite.')
    monkeypatch.setattr(recovery,'ROOT',tmp_path)
    monkeypatch.setattr(recovery,'recovery_source_digest',lambda:'fixed-recovery-source')
    monkeypatch.setattr(diagnostic,'benchmark',lambda cfg:type('Eval',(),{'evaluate':staticmethod(option_exact_match)})())
    monkeypatch.setattr(recovery,'dotenv_values',lambda p:{'GEMINI_API_KEY':'offline-recovery-key'})
    monkeypatch.setenv('GEMINI_API_KEY','offline-recovery-key')
    hashes = {k:diagnostic.digest(p) for k,p in paths.items()}
    return cfg,paths,cfgfile,hashes


def receipt_for(paths,cfg,remaining=98):
    target = paths['plan'].with_name('fixture_approval.json')
    target.write_text(json.dumps({'approved':True,'plan_sha256':diagnostic.digest(paths['plan']),
        'free_tier':True,'billing_disabled':True,'remaining_daily_requests':remaining,
        'checked_at':datetime.now(timezone.utc).isoformat()}))
    return target


@pytest.mark.parametrize('fail_at',[None,2])
def test_recovery_never_retries_recorded_slots_and_preserves_all_parent_artifacts(parent,monkeypatch,fail_at):
    cfg,paths,cfgfile,hashes = parent
    manifest = recovery.prepare(cfg,paths,cfgfile)
    assert manifest['new_requests_required'] == 78
    output = recovery.recovery_paths(paths)
    receipt = receipt_for(output,cfg)
    raw = FakeRaw(fail_at)
    options = []
    def fake_client(*args,**kwargs):
        options.append(args[6])
        return raw
    monkeypatch.setattr(recovery,'GeminiLLM',fake_client)
    monkeypatch.setattr(recovery,'JournalBackend',lambda raw,path,run_id,interval,**kw:
        JournalBackend(raw,path,run_id,0,**kw))
    recovery.execute(cfg,paths,cfgfile,receipt)
    assert options[0]['rpd'] == 78
    assert raw.generation_calls_issued == (fail_at or 78) and raw.closed
    assert raw.prompts[0].startswith('You are agent 2.')
    assert all(diagnostic.digest(paths[k]) == v for k,v in hashes.items())
    rows = recovery.read_rows(output['records'])
    assert len(rows) == 20 and rows[0]['status'] == 'failed_or_incomplete'
    assert rows[0]['diagnostics']['returned'][1] is False  # Truncation never promoted to an answer.
    assert rows[0]['responses'][0]['source_run_id'] == 'parent-fixture'
    assert all('Peer messages' not in p and '#### A' not in p for p in raw.prompts)
    if fail_at:
        assert all(r['status'] == 'not_run' for r in rows[1:])
    else:
        assert all(r['status'] == 'completed' for r in rows[1:])
        summary = json.loads(output['summary'].read_text())
        assert summary['complete_four_response_questions'] == 19
        assert summary['individual_agents'][1]['returned'] == 19
    with pytest.raises(ValueError,match='already attempted'):
        recovery.execute(cfg,paths,cfgfile,receipt)


def test_recovery_approval_quota_and_parent_integrity_fail_before_client(parent,monkeypatch):
    cfg,paths,cfgfile,_ = parent
    recovery.prepare(cfg,paths,cfgfile)
    monkeypatch.setattr(recovery,'GeminiLLM',lambda *a,**k:pytest.fail('Provider client constructed'))
    with pytest.raises(ValueError,match='locked'):
        recovery.execute(cfg,paths,cfgfile,None)
    output = recovery.recovery_paths(paths)
    approval = receipt_for(output,cfg,97)
    with pytest.raises(ValueError,match='remaining-quota'):
        recovery.execute(cfg,paths,cfgfile,approval)
    approval = receipt_for(output,cfg,98)
    paths['calls'].write_text(paths['calls'].read_text()+'\n')
    with pytest.raises(ValueError,match='Parent artifacts changed'):
        recovery.execute(cfg,paths,cfgfile,approval)


@pytest.mark.parametrize('damage',['orphan','prompt','duplicate'])
def test_recovery_rejects_unreconciled_or_different_attempts(parent,damage):
    cfg,paths,cfgfile,_ = parent
    events = recovery.read_rows(paths['calls'])
    if damage == 'orphan':
        events.pop()
    elif damage == 'prompt':
        events[0]['prompt'] += ' changed'
    else:
        events += events[:2]
    paths['calls'].write_text(''.join(json.dumps(e)+'\n' for e in events))
    with pytest.raises(ValueError):
        recovery.prepare(cfg,paths,cfgfile)
