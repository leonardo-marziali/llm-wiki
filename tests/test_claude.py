import json
from pathlib import Path

import pytest
from conftest import ok

from llm_wiki.claude import (
    REQUIRED_FLAGS, build_argv, child_env, claude_binary, make_call, parse_output, run,
)
from llm_wiki.config import LlmSettings
from llm_wiki.errors import ClaudeError, ClaudeNotInstalled, WikiError


def _call(role="plan", **kw):
    return make_call(role, LlmSettings(), prompt="P", system_prompt="S", **kw)


BASE = [
    "claude", "-p", "--output-format", "json", "--model", "claude-sonnet-5-5",
    "--system-prompt-file", "/s", "--tools", "", "--no-session-persistence",
    "--setting-sources", "", "--strict-mcp-config", "--permission-prompts", "none",
    "--max-budget-usd", "1.00",
]


def test_plan_argv_exact():
    assert build_argv(_call(), "claude", Path("/s")) == BASE


def test_edit_argv_adds_permission_flags():
    argv = build_argv(_call("edit", cwd=Path("/w")), "claude", Path("/s"))
    assert argv[argv.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert argv[-3:] == ["--permission-mode", "acceptEdits", "--restricted"]
    assert "--bare" not in argv


def test_schema_is_compact_sorted_json():
    argv = build_argv(_call(json_schema={"b": 1, "a": {"y": [1, 2], "x": 0}}), "claude", Path("/s"))
    assert argv[-2:] == ["--json-schema", '{"a":{"x":0,"y":[1,2]},"b":1}']


def test_required_flags_cover_build_argv():
    for call in (_call(), _call("edit"), _call(json_schema={"a": 1})):
        flags = {a for a in build_argv(call, "claude", Path("/s")) if a.startswith("--")}
        assert flags <= set(REQUIRED_FLAGS)


def test_child_env_strips_api_keys():
    env = child_env({
        "ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t",
        "CLAUDE_CODE_OAUTH_TOKEN": "o", "PATH": "/bin",
    })
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "o" and env["PATH"] == "/bin"


def test_child_env_strips_nested_session_markers():
    env = child_env({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_parse_output_success():
    res = parse_output(json.dumps(ok("hi", {"a": 1}, 0.01)))
    assert (res.text, res.structured, res.cost_usd, res.session_id) == ("hi", {"a": 1}, 0.01, "s1")


def test_parse_output_rejects_non_json():
    with pytest.raises(ClaudeError):
        parse_output("not json")


def test_run_passes_prompt_on_stdin_and_system_prompt_via_file(fake_claude):
    fake_claude.queue({"stdout": ok()})
    run(_call())
    (c,) = fake_claude.calls()
    assert c["stdin"] == "P" and c["system_prompt"] == "S"
    assert not Path(c["argv"][c["argv"].index("--system-prompt-file") + 1]).exists()


def test_plan_call_cwd_is_empty_tempdir(fake_claude):
    fake_claude.queue({"stdout": ok()})
    run(_call())
    (c,) = fake_claude.calls()
    assert c["cwd"] != str(Path.cwd()) and c["listdir"] == []


def test_edit_call_cwd_is_given_dir(fake_claude, tmp_path):
    fake_claude.queue({"stdout": ok()})
    run(_call("edit", cwd=tmp_path))
    assert fake_claude.calls()[0]["cwd"] == str(tmp_path.resolve())


def test_large_system_prompt_goes_through_file(fake_claude):
    big = "x" * 200_000
    fake_claude.queue({"stdout": ok()})
    run(make_call("plan", LlmSettings(), prompt="P", system_prompt=big))
    (c,) = fake_claude.calls()
    assert all(len(a.encode()) <= 131_072 for a in c["argv"]) and c["system_prompt"] == big


def test_missing_binary_raises_not_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_WIKI_CLAUDE", str(tmp_path / "nope"))
    with pytest.raises(ClaudeNotInstalled):
        run(_call())
    monkeypatch.delenv("LLM_WIKI_CLAUDE")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ClaudeNotInstalled):
        claude_binary()
    assert issubclass(ClaudeNotInstalled, WikiError)  # CLI renders WikiError as exit 2
