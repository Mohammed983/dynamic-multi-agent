"""Revised prompts and frozen manifest preparation; no provider/dataset access."""
from dataclasses import asdict
import json
from pathlib import Path

import pytest
import yaml

from experiments import run_diagnostic as diagnostic
from src.agents import Agent
from src.evaluation import extract_for_format
from src.roles import load_roles
from src.runner import run_question
from src.llm_client import Completion

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('text,expected', [
    ('Brief calculation.\nFinal answer: C', 'C'), ('Final answer: A\n', 'A'),
    ('Reasoning has A and B', None), ('#### A', None), ('A', None),
    ('Final answer: A/B/C/D/E', None), ('Final answer: A or B', None),
    ('Final answer: C\nExtra reasoning', None), ('Final answer: c', None), ('', None)])
def test_v2_requires_explicit_unambiguous_final_line(text,expected):
    assert extract_for_format(text,'option',prompt_style='concise_option_v2') == expected


def test_v2_four_roles_and_routing_preserved():
    roles = load_roles(ROOT / 'configs/diverse_roles.yaml')
    class Backend:
        model, simulated = 'offline-fixture', True
        def __init__(self): self.prompts = []
        def generate(self,prompt):
            self.prompts.append(prompt)
            return Completion('Short fixture.\nFinal answer: D',10,5,.01)
    a,b = Backend(),Backend()
    for backend,topology in [(a,'fully_connected'),(b,'ring')]:
        result = run_question('Same question A)1 B)2 C)3 D)4 E)5',backend,rounds=2,topology=topology,
                              roles=roles,answer_format='option',prompt_style='concise_option_v2')
        assert result['final_answers'] == ['D'] * 4
        assert all('at most 150 words' in p and 'Final answer: X' in p for p in backend.prompts)
        for i,p in enumerate(backend.prompts):
            assert roles[i % 4].instruction in p
        assert all('Peer messages' not in p for p in backend.prompts[:4])
    assert a.prompts[:4] == b.prompts[:4]


def test_v2_prepare_reuses_exact_frozen_bytes_and_never_constructs_client(tmp_path,monkeypatch):
    cfg = yaml.safe_load((ROOT / 'configs/diagnostic_aqua_v2.yaml').read_text())
    original = yaml.safe_load((ROOT / 'configs/diagnostic_aqua.yaml').read_text())
    results,configs = tmp_path/'results',tmp_path/'configs'
    results.mkdir(); configs.mkdir()
    (configs/'diverse_roles.yaml').write_bytes((ROOT/'configs/diverse_roles.yaml').read_bytes())
    cfgpath = configs/'diagnostic_aqua_v2.yaml'
    cfgpath.write_text(yaml.safe_dump(cfg))
    roles = load_roles(configs/'diverse_roles.yaml')
    parent = results/'independent_diagnostic_aqua_plan.json'
    questions = results/'independent_diagnostic_aqua_questions.jsonl'
    references = results/'independent_diagnostic_aqua_references.jsonl'
    questions.write_text(''.join(json.dumps({'item_id':f'fixture:{i}','question':f'Problem {i} A)1 B)2 C)3 D)4 E)5','split':'validation'})+'\n' for i in range(20)))
    references.write_text(''.join(json.dumps({'item_id':f'fixture:{i}','reference':'C'})+'\n' for i in range(20)))
    parent.write_text(json.dumps({'config':original,'roles':[asdict(r) for r in roles],
        'roles_sha256':diagnostic.digest(configs/'diverse_roles.yaml'),
        'question_file_sha256':diagnostic.digest(questions),'reference_file_sha256':diagnostic.digest(references)}))
    (results/'independent_diagnostic_aqua_state.json').write_text(json.dumps({'model_metadata':{
        'name':'models/gemini-3.5-flash-lite','supported_actions':['generateContent'],'output_token_limit':65536}}))
    monkeypatch.setattr(diagnostic,'ROOT',tmp_path)
    monkeypatch.setattr(diagnostic,'source_digest',lambda:'offline-source')
    monkeypatch.setattr(diagnostic,'GeminiLLM',lambda *a,**kw:pytest.fail('Provider access during prepare'))
    monkeypatch.setattr(diagnostic,'benchmark',lambda cfg:type('Dataset',(),{'answer_format':'option',
        'load':lambda *a,**kw:pytest.fail('Frozen cohort was reloaded/reselected')})())
    cfg,paths = diagnostic.settings(cfgpath)
    diagnostic.prepare(cfg,paths,cfgpath)
    assert paths['questions'].read_bytes() == questions.read_bytes()
    assert paths['references'].read_bytes() == references.read_bytes()
    plan = json.loads(paths['plan'].read_text())
    assert plan['roles'] == [asdict(r) for r in roles]
    assert plan['generation_calls_issued'] == 0 and plan['token_budget']['maximum_output_tokens'] == 163840
    assert not paths['calls'].exists()
    with pytest.raises(ValueError,match='Explicit human approval'):
        diagnostic.validate_approval({'approved':False},paths['plan'],cfg)


@pytest.mark.parametrize('changes',[{'max_output_tokens':1024},{'max_output_tokens':65537},
    {'prompt_style':'invented'},{'output_prefix':'results/independent_diagnostic_aqua'}])
def test_v2_config_rejects_bad_cap_style_or_old_prefix(tmp_path,changes):
    cfg = yaml.safe_load((ROOT/'configs/diagnostic_aqua_v2.yaml').read_text())
    path = tmp_path/'invalid.yaml'
    path.write_text(yaml.safe_dump({**cfg,**changes}))
    with pytest.raises(ValueError): diagnostic.settings(path)
