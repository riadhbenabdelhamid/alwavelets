"""Benchmark workloads and reports must detect lost data, not enforce timing."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.benchmark import SCENARIOS, generate_trace, file_hash, run_sample


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_generated_workload_is_reproducible_and_reader_modes_agree(tmp_path, scenario):
    a, b = tmp_path / 'a.vcd', tmp_path / 'b.vcd'
    meta = generate_trace(a, scenario, 8192, signals=5, seed=12)
    assert generate_trace(b, scenario, 8192, signals=5, seed=12) == meta
    assert file_hash(a) == file_hash(b)
    full, lazy = run_sample(str(a), 'full', 2), run_sample(str(a), 'lazy', 2)
    assert full['stored_changes'] == meta['changes']
    assert full['max_time'] == lazy['max_time'] == meta['max_time']
    assert full['view_digest'] == lazy['view_digest']


def test_benchmark_cli_records_samples_and_compares_existing_input(tmp_path):
    trace = tmp_path / 'input.vcd'
    generate_trace(trace, 'mixed', 8192, signals=5)
    report = tmp_path / 'report.json'
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / 'tools/benchmark.py'), '--input', str(trace),
               '--repeat', '1', '--output', str(report)]
    first = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, check=True)
    assert 'main RSS(MiB)' in first.stdout
    data = json.loads(report.read_text())
    assert data['schema_version'] == 1
    assert data['traces'][0]['sha256'] == file_hash(trace)
    assert len(data['results']) == 2
    for row in data['results']:
        assert len(row['samples']) == 1
        assert row['median']['load_s'] >= 0
    second = subprocess.run(command + ['--baseline', str(report)], cwd=tmp_path,
                            text=True, capture_output=True, check=True)
    assert second.stdout.count('baseline delta:') == 2
