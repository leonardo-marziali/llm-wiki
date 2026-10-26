import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from llm_wiki.claude import REQUIRED_FLAGS
from llm_wiki.cli import app
from llm_wiki.doctor import parse_version

runner = CliRunner()


def _by_name(result):
    return {c["name"]: c for c in json.loads(result.output)}


def _doctor(*args):
    return runner.invoke(app, ["doctor", *args, "--format", "json"])


def test_parse_version():
    assert parse_version("2.1.310 (Claude Code)") == (2, 1, 310)
    assert parse_version("garbage") is None


def test_doctor_all_ok_offline(fake_claude, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    res = _doctor("--no-probe")
    checks = _by_name(res)
    assert checks["claude"]["status"] == "ok"
    assert "2.1.310" in checks["claude"]["detail"]
    assert "claude-version" not in checks and "claude-flags" not in checks
    assert checks["git"]["status"] == "ok"
    assert "api-key-env" not in checks
    assert not [c for c in checks.values() if c["status"] == "fail"]
    assert fake_claude.calls() == []


def test_doctor_old_version_fails(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "2.0.1 (Claude Code)")
    res = _doctor("--no-probe")
    assert res.exit_code == 1
    c = _by_name(res)["claude-version"]
    assert c["status"] == "fail"
    assert "2.0.1" in c["detail"] and "2.1.310" in c["detail"]


def test_doctor_missing_flag_fails(fake_claude, monkeypatch):
    flags = [f for f in REQUIRED_FLAGS if f != "--permission-prompts"]
    monkeypatch.setenv("FAKE_CLAUDE_HELP", "\n".join(flags))
    res = _doctor()
    assert res.exit_code == 1
    c = _by_name(res)["claude-flags"]
    assert c["status"] == "fail" and "--permission-prompts" in c["detail"]


def test_doctor_missing_claude_fails_without_traceback(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_WIKI_CLAUDE", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    res = runner.invoke(app, ["doctor"])
    assert res.exit_code == 1
    assert "Traceback" not in res.output
    assert any(ln.startswith("fail") and "claude" in ln for ln in res.output.splitlines())
    assert "claude-version" not in res.output and "claude-flags" not in res.output


def test_doctor_nonexistent_claude_path_fails_cleanly(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_WIKI_CLAUDE", str(tmp_path / "nope"))
    res = _doctor()
    assert res.exit_code == 1
    assert _by_name(res)["claude"]["status"] == "fail"


def test_doctor_warns_on_api_key_env(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    res = _doctor()
    c = _by_name(res)["api-key-env"]
    assert c["status"] == "warn" and "ignored" in c["detail"]
    assert res.exit_code == 0


def test_optional_tools_missing_are_warnings_not_failures(fake_claude, monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))  # isolate git from any gitleaks/rg beside it
    py_dir = str(Path(shutil.which("python3")).parent)
    fake_dir = str(Path(fake_claude.queue_file).parent)
    monkeypatch.setenv("PATH", f"{bin_dir}:{fake_dir}:{py_dir}")
    if shutil.which("rg") or shutil.which("gitleaks"):  # python's dir also holds them
        py_link = tmp_path / "pybin"
        py_link.mkdir()
        (py_link / "python3").symlink_to(shutil.which("python3"))
        monkeypatch.setenv("PATH", f"{bin_dir}:{fake_dir}:{py_link}")
    res = _doctor()
    checks = _by_name(res)
    assert checks["gitleaks"]["status"] == "warn"
    assert checks["ripgrep"]["status"] == "warn"
    assert res.exit_code == 0


def test_doctor_json_format(fake_claude):
    res = _doctor("--no-probe")
    data = json.loads(res.output)
    assert isinstance(data, list)
    assert set(data[0]) == {"name", "status", "detail"}


def test_doctor_text_format(fake_claude):
    res = runner.invoke(app, ["doctor"])
    assert res.output.splitlines()[0].startswith("ok    claude ")


def test_doctor_runs_outside_any_wiki(fake_claude):
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_doctor_unknown_wiki_is_error(fake_claude):
    res = runner.invoke(app, ["doctor", "--wiki", "nope"])
    assert res.exit_code == 2
