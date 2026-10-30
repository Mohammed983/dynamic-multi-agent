"""Shared journal regressions: local files only, no API clients or pricing model."""
import json
import os

import pytest

from src.pilot import append_jsonl


def test_journal_preserves_order_unicode_and_existing_records(tmp_path):
    path = tmp_path / 'calls.jsonl'
    events = [{'event': 'request_started', 'question': 'A quantity of é items'},
              {'event': 'call_incomplete', 'usage_unknown': False, 'finish_reason': 'MAX_TOKENS'}]
    for event in events:
        append_jsonl(path, event)
    assert [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] == events


def test_journal_is_flushed_before_fsync(tmp_path, monkeypatch):
    path = tmp_path / 'calls.jsonl'
    snapshots = []
    def synced(fd):
        assert os.fstat(fd).st_size > 0
        snapshots.append(path.read_text(encoding='utf-8'))
    monkeypatch.setattr(os, 'fsync', synced)
    append_jsonl(path, {'event': 'request_started'})
    assert len(snapshots) == 1
    assert json.loads(snapshots[0]) == {'event': 'request_started'}


def test_invalid_event_does_not_corrupt_prior_journal_records(tmp_path):
    path = tmp_path / 'calls.jsonl'
    append_jsonl(path, {'event': 'request_started'})
    original = path.read_bytes()
    with pytest.raises(TypeError):
        append_jsonl(path, {'invalid': object()})
    assert path.read_bytes() == original


def test_journal_write_errors_are_not_silently_hidden(tmp_path):
    with pytest.raises(FileNotFoundError):
        append_jsonl(tmp_path / 'missing' / 'calls.jsonl', {'event': 'request_started'})
