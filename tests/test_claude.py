import json
from pathlib import Path

import pytest
from conftest import ok

from llm_wiki.claude import (
    REQUIRED_FLAGS,
    build_argv,
    child_env,
    classify,
    claude_binary,
    make_call,
    parse_output,
    run,
)
from llm_wiki.config import LlmSettings, Role
from llm_wiki.errors import (
    ClaudeAuthError,
    ClaudeBudgetExceeded,
    ClaudeError,
    ClaudeNotInstalled,
    ClaudeSchemaError,
    ClaudeUsageLimit,
    WikiError,
)


def _call(role: Role = "plan", **kw):
    return make_call(role, LlmSettings(), prompt="P", system_prompt="S", **kw)


BASE = [
    "claude",
    "-p",
    "--output-format",
    "json",
    "--model",
    "claude-sonnet-5-5",
    "--system-prompt-file",
    "/s",
    "--tools",
    "",
    "--no-session-persistence",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--permission-prompts",
    "none",
    "--max-budget-usd",
    "1.00",
]


def test_plan_argv_exact():
    assert build_argv(_call(), "claude", Path("/s")) == BASE


def test_edit_argv_adds_permission_flags():
    argv = build_argv(_call("edit", cwd=Path("/w")), "claude", Path("/s"))
    assert argv[argv.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep"
    assert argv[-3:] == ["--permission-mode", "acceptEdits", "--restricted"]
    assert "--bare" not in argv


def test_schema_is_compact_sorted_json():
    argv = build_argv(
        _call(json_schema={"b": 1, "a": {"y": [1, 2], "x": 0}}), "claude", Path("/s")
    )
    assert argv[-2:] == ["--json-schema", '{"a":{"x":0,"y":[1,2]},"b":1}']


def test_required_flags_cover_build_argv():
    for call in (_call(), _call("edit"), _call(json_schema={"a": 1})):
        flags = {
            a for a in build_argv(call, "claude", Path("/s")) if a.startswith("--")
        }
        assert flags <= set(REQUIRED_FLAGS)


def test_child_env_strips_api_keys():
    env = child_env(
        {
            "ANTHROPIC_API_KEY": "k",
            "ANTHROPIC_AUTH_TOKEN": "t",
            "CLAUDE_CODE_OAUTH_TOKEN": "o",
            "PATH": "/bin",
        }
    )
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "o"
    assert env["PATH"] == "/bin"


def test_child_env_strips_nested_session_markers():
    env = child_env({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_parse_output_success():
    res = parse_output(json.dumps(ok("hi", {"a": 1}, 0.01)))
    assert (res.text, res.structured, res.cost_usd, res.session_id) == (
        "hi",
        {"a": 1},
        0.01,
        "s1",
    )


def test_parse_output_rejects_non_json():
    with pytest.raises(ClaudeError):
        parse_output("not json")


def test_run_passes_prompt_on_stdin_and_system_prompt_via_file(fake_claude):
    fake_claude.queue({"stdout": ok()})
    run(_call())
    (c,) = fake_claude.calls()
    assert c["stdin"] == "P"
    assert c["system_prompt"] == "S"
    assert not Path(c["argv"][c["argv"].index("--system-prompt-file") + 1]).exists()


def test_plan_call_cwd_is_empty_tempdir(fake_claude):
    fake_claude.queue({"stdout": ok()})
    run(_call())
    (c,) = fake_claude.calls()
    assert c["cwd"] != str(Path.cwd())
    assert c["listdir"] == []
    assert not Path(c["cwd"]).exists()


def test_edit_call_cwd_is_given_dir(fake_claude, tmp_path):
    fake_claude.queue({"stdout": ok()})
    run(_call("edit", cwd=tmp_path))
    assert fake_claude.calls()[0]["cwd"] == str(tmp_path.resolve())


def test_large_system_prompt_goes_through_file(fake_claude):
    big = "x" * 200_000
    fake_claude.queue({"stdout": ok()})
    run(make_call("plan", LlmSettings(), prompt="P", system_prompt=big))
    (c,) = fake_claude.calls()
    assert all(len(a.encode()) <= 131_072 for a in c["argv"])
    assert c["system_prompt"] == big


def test_missing_binary_raises_not_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_WIKI_CLAUDE", str(tmp_path / "nope"))
    call = _call()
    with pytest.raises(ClaudeNotInstalled):
        run(call)
    monkeypatch.delenv("LLM_WIKI_CLAUDE")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ClaudeNotInstalled):
        claude_binary()
    assert issubclass(ClaudeNotInstalled, WikiError)  # CLI renders WikiError as exit 2


OVERLOADED = {"stdout": "", "stderr": "API Error: 529 overloaded", "exit": 1}


def test_transient_then_success(fake_claude):
    fake_claude.queue(OVERLOADED, {"stdout": ok("fine")})
    sleeps = []
    res = run(_call(), sleep=sleeps.append)
    assert res.text == "fine"
    assert len(fake_claude.calls()) == 2
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 3.0


def test_transient_exhausts_after_three_retries(fake_claude):
    fake_claude.queue(*[OVERLOADED] * 4)
    sleeps = []
    call = _call()
    with pytest.raises(ClaudeError, match="529"):
        run(call, sleep=sleeps.append, rand=lambda: 0.5)
    assert len(fake_claude.calls()) == 4
    assert sleeps == [2, 4, 8]


def test_auth_failure_not_retried(fake_claude):
    fake_claude.queue({"stdout": "", "stderr": "Not logged in", "exit": 1})
    call = _call()
    with pytest.raises(ClaudeAuthError, match="claude /login"):
        run(call, sleep=lambda s: None)
    assert len(fake_claude.calls()) == 1


def test_usage_limit_not_retried_and_message_kept(fake_claude):
    fake_claude.queue(
        {"stdout": "", "stderr": "Claude AI usage limit reached|1761500000", "exit": 1}
    )
    call = _call()
    with pytest.raises(ClaudeUsageLimit, match=r"1761500000.*wait and re-run"):
        run(call, sleep=lambda s: None)
    assert len(fake_claude.calls()) == 1


def test_budget_exceeded(fake_claude):
    fake_claude.queue(
        {
            "stdout": {
                "type": "result",
                "subtype": "error_max_budget_usd",
                "is_error": True,
                "result": "",
                "total_cost_usd": 1.2,
                "duration_ms": 1,
                "session_id": "s",
            },
            "exit": 1,
        }
    )
    call = _call()
    with pytest.raises(ClaudeBudgetExceeded, match=r"\$1\.00.*plan_budget_usd"):
        run(call, sleep=lambda s: None)
    assert len(fake_claude.calls()) == 1


def test_budget_without_role_names_generic_setting(fake_claude):
    import dataclasses

    fake_claude.queue(
        {"stdout": {"subtype": "error_max_budget_usd", "is_error": True}, "exit": 1}
    )
    call = dataclasses.replace(_call(), role=None)
    with pytest.raises(ClaudeBudgetExceeded, match="the budget setting"):
        run(call)


def test_schema_missing_retried_once_with_error(fake_claude):
    fake_claude.queue({"stdout": ok(structured=None)}, {"stdout": ok(structured=None)})
    call = _call(json_schema={"type": "object"})
    with pytest.raises(ClaudeSchemaError):
        run(call, sleep=lambda s: None)
    first, second = fake_claude.calls()
    assert first["stdin"] == "P"
    assert second["stdin"].startswith(
        "P\n\nYour previous reply did not match the required JSON schema:"
    )
    assert second["stdin"].endswith("Reply again with only the structured output.")


def test_schema_retry_recovers(fake_claude):
    fake_claude.queue(
        {"stdout": ok(structured=None)}, {"stdout": ok(structured={"a": 1})}
    )
    res = run(_call(json_schema={"type": "object"}), sleep=lambda s: None)
    assert res.structured == {"a": 1}


def test_schema_retry_does_not_consume_transient_slot(fake_claude):
    fake_claude.queue(
        OVERLOADED,
        OVERLOADED,
        OVERLOADED,
        {"stdout": ok(structured=None)},
        {"stdout": ok(structured={"a": 1})},
    )
    res = run(_call(json_schema={"type": "object"}), sleep=lambda s: None)
    assert res.structured == {"a": 1}


def test_unparseable_output_retried_then_error(fake_claude):
    fake_claude.queue(*[{"stdout": "<html>oops"}] * 4)
    call = _call()
    with pytest.raises(ClaudeError, match="<html>oops"):
        run(call, sleep=lambda s: None)
    assert len(fake_claude.calls()) == 4


def test_hang_times_out_and_retries(fake_claude):
    import dataclasses
    import time

    fake_claude.queue({"sleep": 5}, {"stdout": ok()})
    t0 = time.monotonic()
    res = run(dataclasses.replace(_call(), timeout_s=1), sleep=lambda s: None)
    assert res.text == "done"
    assert len(fake_claude.calls()) == 2
    assert time.monotonic() - t0 < 4


def test_timeout_message_after_retries(fake_claude):
    import dataclasses

    fake_claude.queue(*[{"sleep": 3}] * 4)
    call = dataclasses.replace(_call(), timeout_s=0.3)
    with pytest.raises(ClaudeError, match=r"claude timed out after 0\.3s"):
        run(call, sleep=lambda s: None)  # float works too


def test_fatal_not_retried(fake_claude):
    fake_claude.queue({"stdout": "", "stderr": "unknown option --frob", "exit": 1})
    call = _call()
    with pytest.raises(ClaudeError, match="unknown option --frob"):
        run(call, sleep=lambda s: None)
    assert len(fake_claude.calls()) == 1


def test_classify_success_beats_text_rules():
    body = json.dumps(ok("the budget and 401 and network are fine"))
    assert classify(0, body, "") == "ok"
    assert classify(0, json.dumps(ok(structured=None)), "", has_schema=True) == "schema"
    assert classify(0, "", "") == "transient"


def test_missing_cwd_is_not_reported_as_missing_binary(fake_claude, tmp_path):
    fake_claude.queue({"stdout": ok()})
    gone = tmp_path / "gone"
    call = _call("edit", cwd=gone)
    with pytest.raises(ClaudeError, match="gone") as ei:
        run(call)
    assert not isinstance(ei.value, ClaudeNotInstalled)


def test_non_executable_binary_is_clear_error(tmp_path):
    bin_ = tmp_path / "claude"
    bin_.write_text("#!/bin/sh\n")
    bin_.chmod(0o644)
    call = _call()
    binary = str(bin_)
    with pytest.raises(ClaudeNotInstalled, match="not executable"):
        run(call, binary=binary)


def test_system_prompt_file_removed_when_spawn_fails(tmp_path, monkeypatch):
    import tempfile

    made = []
    real = tempfile.mkstemp

    def spy(*a, **kw):
        fd, name = real(*a, **kw)
        made.append(name)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy)
    call = _call()
    binary = str(tmp_path / "nope")
    with pytest.raises(ClaudeNotInstalled):
        run(call, binary=binary)
    assert made
    assert not any(Path(m).exists() for m in made)


def test_parse_output_tolerates_malformed_numbers():
    res = parse_output(
        json.dumps(
            {
                "result": "x",
                "total_cost_usd": "abc",
                "duration_ms": [1],
                "structured_output": "str",
            }
        )
    )
    assert (res.cost_usd, res.duration_ms, res.structured) == (0.0, 0, None)


def test_env_stripping_holds_through_run(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    fake_claude.queue({"stdout": ok()})
    run(_call())
    env = fake_claude.calls()[0]["env"]
    assert env["ANTHROPIC_API_KEY"] is None
    assert env["CLAUDECODE"] is None
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_rate_limit_reached_is_transient_not_usage_limit(fake_claude):
    assert classify(1, "", "API Error: 429 Rate limit reached") == "transient"
    fake_claude.queue(
        {"stdout": "", "stderr": "API Error: 429 Rate limit reached", "exit": 1},
        {"stdout": ok("fine")},
    )
    assert run(_call(), sleep=lambda s: None).text == "fine"
    assert len(fake_claude.calls()) == 2


def test_cwd_that_is_a_file_is_clear_error(fake_claude, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    call = _call("edit", cwd=f)
    with pytest.raises(ClaudeError, match="afile") as ei:
        run(call)
    assert not isinstance(ei.value, ClaudeNotInstalled)


def test_fatal_json_failure_quotes_result_text(fake_claude):
    fake_claude.queue(
        {
            "stdout": {
                "is_error": True,
                "subtype": "error_during_execution",
                "result": "model exploded",
                "session_id": "s",
            },
            "exit": 1,
        }
    )
    call = _call()
    with pytest.raises(ClaudeError) as ei:
        run(call, sleep=lambda s: None)
    assert "model exploded" in str(ei.value)
    assert "session_id" not in str(ei.value)


def test_numbers_inside_larger_tokens_do_not_look_like_status_codes(fake_claude):
    fake_claude.queue(
        {
            "stdout": "",
            "stderr": "bad request: 1500 tokens exceeds limit 1761500000",
            "exit": 1,
        }
    )
    call = _call()
    with pytest.raises(ClaudeError, match="1500 tokens"):
        run(call, sleep=lambda s: None)
    assert len(fake_claude.calls()) == 1


def test_edit_call_is_not_auto_retried(fake_claude, tmp_path):
    assert _call("edit").max_retries == 0
    assert _call().max_retries == 3
    fake_claude.queue(OVERLOADED, {"stdout": ok()})
    call = _call("edit", cwd=tmp_path)
    with pytest.raises(ClaudeError, match="529"):
        run(call, sleep=lambda s: None)
    assert len(fake_claude.calls()) == 1


def test_cost_sums_failed_transient_attempts(fake_claude):
    failed = {
        "stdout": {
            "is_error": True,
            "result": "API Error: 529 overloaded",
            "total_cost_usd": 0.02,
        },
        "exit": 1,
    }
    fake_claude.queue(failed, {"stdout": ok(cost=0.01)})
    res = run(_call(), sleep=lambda s: None)
    assert res.cost_usd == pytest.approx(0.03)


def test_cost_sums_schema_retry(fake_claude):
    fake_claude.queue(
        {"stdout": ok(structured=None, cost=0.01)},
        {"stdout": ok(structured={"a": 1}, cost=0.02)},
    )
    res = run(_call(json_schema={"type": "object"}), sleep=lambda s: None)
    assert res.cost_usd == pytest.approx(0.03)


def test_invalid_budget_option_is_fatal_not_budget():
    assert (
        classify(1, "", "error: option --max-budget-usd: invalid number 'x'") == "fatal"
    )
    assert classify(1, "", "Exceeded USD budget (1.00)") == "budget"
    assert classify(1, "", "budget of $1 exceeded") == "budget"


def test_unwritable_tempfile_is_claude_error(fake_claude, monkeypatch):
    import tempfile

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tempfile, "mkstemp", boom)
    call = _call()
    with pytest.raises(ClaudeError, match="No space left") as ei:
        run(call)
    assert not isinstance(ei.value, ClaudeNotInstalled)
    assert fake_claude.calls() == []


def test_unwritable_tempdir_is_claude_error(fake_claude, monkeypatch):
    import tempfile

    def boom(*a, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", boom)
    call = _call()
    with pytest.raises(ClaudeError, match="Permission denied") as ei:
        run(call)
    assert not isinstance(ei.value, ClaudeNotInstalled)


def test_missing_env_binary_names_the_path(monkeypatch, tmp_path):
    missing = tmp_path / "nope"
    monkeypatch.setenv("LLM_WIKI_CLAUDE", str(missing))
    with pytest.raises(
        ClaudeNotInstalled, match=f"LLM_WIKI_CLAUDE points to a missing file: {missing}"
    ):
        claude_binary()
    call = _call()
    with pytest.raises(
        ClaudeNotInstalled, match="LLM_WIKI_CLAUDE points to a missing file"
    ):
        run(call)


def test_autouse_env_never_reaches_real_claude():
    import os

    assert not Path(os.environ["LLM_WIKI_CLAUDE"]).exists()
