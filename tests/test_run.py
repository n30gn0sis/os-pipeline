import sys

import pytest

from osp.run import CommandError, RecordingRunner, Runner

PY = sys.executable


def test_run_captures_output_and_logs(tmp_path):
    log = tmp_path / "logs" / "x.log"
    res = Runner(log_path=log).run([PY, "-c", "print('hello')"])
    assert res.returncode == 0
    assert "hello" in res.stdout
    text = log.read_text(encoding="utf-8")
    assert text.startswith("$ ")
    assert "hello" in text


def test_log_is_appended_across_calls(tmp_path):
    log = tmp_path / "x.log"
    r = Runner(log_path=log)
    r.run([PY, "-c", "print('one')"])
    r.run([PY, "-c", "print('two')"])
    text = log.read_text(encoding="utf-8")
    assert "one" in text and "two" in text


def test_run_redacts_secrets_in_log_and_command_line(tmp_path):
    log = tmp_path / "x.log"
    Runner(log_path=log, secrets=["s3cret"]).run([PY, "-c", "print('value=s3cret')", "s3cret"])
    text = log.read_text(encoding="utf-8")
    assert "s3cret" not in text
    assert "value=***" in text


def test_run_raises_command_error_with_log_path(tmp_path):
    log = tmp_path / "x.log"
    with pytest.raises(CommandError) as ei:
        Runner(log_path=log).run([PY, "-c", "import sys; sys.exit(3)"])
    assert ei.value.returncode == 3
    assert ei.value.log_path == log


def test_run_check_false_returns_returncode():
    res = Runner().run([PY, "-c", "import sys; sys.exit(2)"], check=False)
    assert res.returncode == 2


def test_run_passes_input():
    res = Runner().run([PY, "-c", "import sys; print(sys.stdin.read().upper())"], input="abc")
    assert "ABC" in res.stdout


def test_with_log_keeps_secrets(tmp_path):
    r = Runner(secrets=["pw"]).with_log(tmp_path / "y.log")
    assert r.redact("pw!") == "***!"


def test_recording_runner_records_and_fakes_results():
    r = RecordingRunner(results={"false": (1, ""), "dpkg-query": (0, "a\t1\n")})
    r.run(["echo", "hi"], input="x")
    assert r.calls == [["echo", "hi"]]
    assert r.inputs == ["x"]
    assert r.run(["dpkg-query"]).stdout == "a\t1\n"
    with pytest.raises(CommandError):
        r.run(["false"])
    assert r.run(["false"], check=False).returncode == 1
