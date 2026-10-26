import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from llm_wiki.claude import REQUIRED_FLAGS
from llm_wiki.cli import app
from conftest import ok
from llm_wiki import doctor as doctor_mod
from llm_wiki.config import LlmSettings
from llm_wiki.doctor import PROBE_SCHEMA, parse_version, probe_edit_confinement, probe_model, run_doctor

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
    res = _doctor("--no-probe")
    assert res.exit_code == 1
    c = _by_name(res)["claude-flags"]
    assert c["status"] == "fail" and "--permission-prompts" in c["detail"]


def test_doctor_missing_claude_fails_without_traceback(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_WIKI_CLAUDE", raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))  # the claude row is the only failure
    monkeypatch.setenv("PATH", str(bin_dir))
    res = _doctor()
    assert [c["name"] for c in json.loads(res.output) if c["status"] == "fail"] == ["claude"]
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
    res = _doctor("--no-probe")
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
    res = _doctor("--no-probe")
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
    res = runner.invoke(app, ["doctor", "--no-probe"])
    assert res.output.splitlines()[0].startswith("ok    claude ")


def test_doctor_runs_outside_any_wiki(fake_claude):
    assert runner.invoke(app, ["doctor", "--no-probe"]).exit_code == 0


def test_doctor_unknown_wiki_is_error(fake_claude):
    res = runner.invoke(app, ["doctor", "--wiki", "nope"])
    assert res.exit_code == 2


def test_version_timeout(fake_claude, monkeypatch):
    monkeypatch.setattr(doctor_mod, "_TIMEOUT_S", 0.2)
    monkeypatch.setenv("FAKE_CLAUDE_VERSION_SLEEP", "2")
    c = _by_name(_doctor("--no-probe"))["claude"]
    assert c["status"] == "fail" and "timed out" in c["detail"]


def test_version_nonzero_exit(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_VERSION_EXIT", "7")
    c = _by_name(_doctor("--no-probe"))["claude"]
    assert c["status"] == "fail" and "exited 7" in c["detail"]


def test_unparsable_version(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "banana")
    c = _by_name(_doctor("--no-probe"))["claude-version"]
    assert c["status"] == "fail" and "banana" in c["detail"]


def test_failing_help_reports_exit_code(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_HELP_EXIT", "5")
    c = _by_name(_doctor("--no-probe"))["claude-flags"]
    assert c["status"] == "fail" and "exited 5" in c["detail"]


def test_flags_match_whole_tokens(fake_claude, monkeypatch):
    flags = [f if f != "--model" else "--model-foo" for f in REQUIRED_FLAGS]
    monkeypatch.setenv("FAKE_CLAUDE_HELP", "\n".join(flags))
    c = _by_name(_doctor("--no-probe"))["claude-flags"]
    assert c["status"] == "fail" and "--model" in c["detail"].split(": ", 1)[1].split(", ")


S = LlmSettings()
PROBE_OK = ok(structured={"ok": True, "canary": ""})
PROBE_RESP = {"stdout": PROBE_OK, "echo_token": True}


def test_probe_model_ok(fake_claude):
    fake_claude.queue(PROBE_RESP)
    c = probe_model("plan", S)
    assert c.status == "ok" and c.name == f"model:{S.model('plan')}"
    (call,) = fake_claude.calls()
    assert "CLAUDE.md" in call["listdir"]
    assert "--json-schema" in call["argv"]
    assert json.loads(call["argv"][call["argv"].index("--json-schema") + 1]) == PROBE_SCHEMA
    assert call["argv"][call["argv"].index("--max-budget-usd") + 1] == "0.25"


def test_probe_model_detects_canary_leak(fake_claude):
    fake_claude.queue({**PROBE_RESP, "echo_canary": True})
    c = probe_model("plan", S)
    assert c.status == "fail" and "isolation" in c.detail


def test_probe_model_detects_ignored_system_prompt(fake_claude):
    fake_claude.queue({"stdout": PROBE_OK})  # no token echoed
    c = probe_model("plan", S)
    assert c.status == "fail" and "system prompt file ignored" in c.detail


def test_hidden_flag_not_required_in_help(fake_claude, monkeypatch):
    from llm_wiki.claude import HIDDEN_FLAGS
    monkeypatch.setenv("FAKE_CLAUDE_HELP", "\n".join(f for f in REQUIRED_FLAGS if f not in HIDDEN_FLAGS))
    assert "claude-flags" not in _by_name(_doctor("--no-probe"))


def test_text_output_pads_to_widest_name(fake_claude):
    fake_claude.queue(PROBE_RESP, PROBE_RESP, {"stdout": ok(), "write": {"inside.txt": "ok"}})
    lines = runner.invoke(app, ["doctor"]).output.splitlines()
    cols = {len(ln) - len(ln.split(None, 2)[2]) for ln in lines}
    assert len(cols) == 1, lines


def test_probe_model_auth_fail(fake_claude):
    fake_claude.queue({"exit": 1, "stderr": "Not logged in - run /login"})
    c = probe_model("plan", S)
    assert c.status == "fail" and "claude /login" in c.detail


def test_probe_model_unknown_model_fail(fake_claude):
    fake_claude.queue({"exit": 1, "stderr": "model claude-x not found"})
    c = probe_model("plan", S)
    assert c.status == "fail" and "model id not available" in c.detail


def test_probe_model_schema_fail(fake_claude):
    fake_claude.queue({"stdout": ok()}, {"stdout": ok()})
    c = probe_model("plan", S)
    assert c.status == "fail" and "structured output unsupported" in c.detail


def test_probe_model_usage_limit_fails_cleanly(fake_claude):
    fake_claude.queue({"exit": 1, "stderr": "Claude usage limit reached"})
    c = probe_model("plan", S)
    assert c.status == "fail" and "usage limit" in c.detail


def test_probes_dedupe_models(fake_claude):
    fake_claude.queue(PROBE_RESP, PROBE_RESP,
                      {"stdout": ok(), "write": {"inside.txt": "ok"}})
    checks = run_doctor(S, probe=True)
    names = [c.name for c in checks]
    assert names[-3:] == [f"model:{S.model('plan')}", f"model:{S.model('deep')}", "edit-confinement"]
    assert len(fake_claude.calls()) == 3


def test_edit_probe_ok(fake_claude):
    fake_claude.queue({"stdout": ok(), "write": {"inside.txt": "ok"}})
    assert probe_edit_confinement(S).status == "ok"
    (call,) = fake_claude.calls()
    assert call["argv"][call["argv"].index("--max-budget-usd") + 1] == "0.25"


def test_edit_probe_detects_escape(fake_claude):
    fake_claude.queue({"stdout": ok(), "write": {"inside.txt": "ok"}, "write_abs_from_prompt": True})
    c = probe_edit_confinement(S)
    assert c.status == "fail" and "outside the wiki" in c.detail


def test_edit_probe_detects_no_write(fake_claude):
    fake_claude.queue({"stdout": ok()})
    c = probe_edit_confinement(S)
    assert c.status == "fail" and "permission mode" in c.detail


def test_no_probe_makes_no_calls(fake_claude):
    run_doctor(S, probe=False)
    assert fake_claude.calls() == []


def test_probes_skipped_when_claude_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_WIKI_CLAUDE", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    checks = run_doctor(S, probe=True)
    assert [c.name for c in checks][:1] == ["claude"]
    assert not [c for c in checks if c.name.startswith("model:")]


def test_edit_probe_error_after_escape_still_reports_escape(fake_claude):
    fake_claude.queue({"stdout": "boom", "exit": 1, "write_abs_from_prompt": True})
    c = probe_edit_confinement(S)
    assert c.status == "fail" and "outside the wiki" in c.detail


def test_probe_model_non_leaking_canary_text_is_ok(fake_claude):
    fake_claude.queue({"stdout": ok(structured={"ok": True, "canary": "none"}), "echo_token": True})
    assert probe_model("plan", S).status == "ok"


def test_probe_budget_error_does_not_advise_settings_key(fake_claude):
    fake_claude.queue({"stdout": {"subtype": "error_max_budget_usd", "is_error": True, "result": "x"},
                       "exit": 1})
    c = probe_model("plan", S)
    assert c.status == "fail" and "_budget_usd" not in c.detail


def test_probes_skipped_when_version_too_old(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "2.0.1 (Claude Code)")
    checks = run_doctor(S, probe=True)
    assert not [c for c in checks if c.name.startswith("model:")]
    assert fake_claude.calls() == []
