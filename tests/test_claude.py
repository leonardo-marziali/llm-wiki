import dataclasses
import json
import os
import stat
import time
from pathlib import Path

import pytest
from conftest import ok

from llm_wiki import claude as claude_mod
from llm_wiki.claude import (
    REQUIRED_FLAGS,
    build_argv,
    child_env,
    classify,
    claude_binary,
    deny_rules,
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
from llm_wiki.images import prepare_for_vision


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


def test_deny_rules_dirs_and_files():
    rules = deny_rules(
        ["wiki/b/x.md", ".wiki/quarantine/", "wiki/a/y.md", "wiki/b/x.md"]
    )
    assert rules == (
        "Read(./.wiki/quarantine/**)",
        "Read(./wiki/a/y.md)",
        "Read(./wiki/b/x.md)",
    )
    assert deny_rules([]) == ()


def test_build_argv_deny_only_for_edit():
    rules = deny_rules([".wiki/quarantine/", "wiki/a/y.md"])
    edit = build_argv(_call("edit", deny_read=rules), "claude", Path("/s"))
    i = edit.index("--disallowed-tools")
    assert edit[i + 1 : i + 3] == list(rules)
    plan = build_argv(_call(deny_read=rules), "claude", Path("/s"))
    assert "--disallowed-tools" not in plan
    bare_edit = build_argv(_call("edit"), "claude", Path("/s"))
    assert "--disallowed-tools" not in bare_edit


def test_required_flags_include_disallowed_tools():
    assert "--disallowed-tools" in REQUIRED_FLAGS
    call = _call("edit", deny_read=deny_rules(["wiki/a.md"]))
    flags = {a for a in build_argv(call, "claude", Path("/s")) if a.startswith("--")}
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


def test_failed_call_reports_cost(fake_claude):
    failed = {
        "stdout": {
            "is_error": True,
            "result": "API Error: 529 overloaded",
            "total_cost_usd": 0.01,
        },
        "exit": 1,
    }
    fake_claude.queue(failed, failed, failed, failed)
    call = _call()
    with pytest.raises(ClaudeError) as ei:
        run(call, sleep=lambda s: None)
    assert ei.value.cost_usd == pytest.approx(0.04)
    assert ClaudeError("x").cost_usd == 0.0


def test_budget_error_carries_its_attempt_cost(fake_claude):
    fake_claude.queue(
        {
            "stdout": {
                "subtype": "error_max_budget_usd",
                "is_error": True,
                "total_cost_usd": 1.2,
            },
            "exit": 1,
        }
    )
    call = _call()
    with pytest.raises(ClaudeBudgetExceeded) as ei:
        run(call)
    assert ei.value.cost_usd == pytest.approx(1.2)


def test_enoexec_is_claude_error(tmp_path):
    bin_ = tmp_path / "claude"
    bin_.write_bytes(b"\x7fELFgarbage")
    bin_.chmod(0o755)
    call = _call()
    with pytest.raises(ClaudeError, match="cannot execute") as ei:
        run(call, binary=str(bin_))
    assert not isinstance(ei.value, ClaudeNotInstalled)


def test_llm_wiki_claude_bare_name_resolved_via_path(fake_claude, monkeypatch):
    fake_dir = Path(__file__).parent / "fake_claude"
    monkeypatch.setenv("LLM_WIKI_CLAUDE", "claude")
    monkeypatch.setenv("PATH", str(fake_dir))
    assert claude_binary() == str(fake_dir / "claude")
    monkeypatch.setenv("PATH", str(fake_dir.parent))
    with pytest.raises(ClaudeNotInstalled, match="not found on PATH"):
        claude_binary()


IMG_HEAD = [
    "claude",
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--input-format",
    "stream-json",
]


def _image_call(path, **kw):
    return _call(images=(path,), **kw)


def test_image_call_argv(tmp_path):
    img = tmp_path / "a.png"
    argv = build_argv(_image_call(img, json_schema={"a": 1}), "claude", Path("/s"))
    assert argv[: len(IMG_HEAD)] == IMG_HEAD
    # Everything after the output/input flags is the text-only argv, unchanged.
    assert argv[len(IMG_HEAD) :] == [
        *BASE[4:],
        "--json-schema",
        '{"a":1}',
    ]
    assert {a for a in argv if a.startswith("--")} <= set(REQUIRED_FLAGS)


def test_text_only_call_unchanged():
    assert build_argv(_call(), "claude", Path("/s")) == BASE
    assert "--input-format" not in BASE
    assert "--verbose" not in BASE


def test_make_call_defaults_to_no_images():
    assert _call().images == ()


def test_image_call_stdin_is_stream_json(fake_claude, make_png, tmp_path):
    import base64

    src = make_png(tmp_path / "src.png", size=(300, 200))
    fake_claude.queue({"stdout": ok(structured={"a": 1})})
    run(_image_call(src, json_schema={"a": 1}))
    (c,) = fake_claude.calls()
    assert c["stdin"].count("\n") == 1
    assert c["stdin"].endswith("\n")
    msg = c["stdin_message"]
    assert msg["type"] == "user"
    assert msg["message"]["role"] == "user"
    text, image = msg["message"]["content"]
    assert text == {"type": "text", "text": "P"}
    assert image["type"] == "image"
    assert image["source"]["type"] == "base64"
    assert image["source"]["media_type"] == "image/png"
    sent = base64.b64decode(image["source"]["data"])
    assert sent.startswith(b"\x89PNG")
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(sent)) as im:
        assert im.size == (300, 200)


def test_image_temp_files_are_removed(fake_claude, make_png, tmp_path):
    src = make_png(tmp_path / "src.png")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    fake_claude.queue({"stdout": ok()})
    run(_call("edit", cwd=wiki, images=(src,)))
    (c,) = fake_claude.calls()
    assert c["cwd"] == str(wiki)
    assert c["listdir"] == []  # prepared images never land in the model's cwd
    assert list(wiki.iterdir()) == []
    assert src.exists()


def test_image_schema_retry_resends_images(fake_claude, make_png, tmp_path):
    src = make_png(tmp_path / "src.png")
    fake_claude.queue({"stdout": ok()}, {"stdout": ok(structured={"a": 1})})
    run(_image_call(src, json_schema={"a": 1}))
    first, second = fake_claude.calls()
    assert len(second["stdin_message"]["message"]["content"]) == 2
    assert "did not match" in second["stdin_message"]["message"]["content"][0]["text"]
    assert (
        first["stdin_message"]["message"]["content"][1]
        == (second["stdin_message"]["message"]["content"][1])
    )


def test_stream_output_last_result_line_parsed(fake_claude, make_png, tmp_path):
    src = make_png(tmp_path / "src.png")
    fake_claude.queue(
        {
            "stdout_lines": [
                {"type": "system", "subtype": "init"},
                ok("early", cost=0.5),
                {"type": "assistant", "message": {}},
                ok("final", structured={"a": 1}, cost=0.02),
            ]
        }
    )
    res = run(_image_call(src, json_schema={"a": 1}))
    assert res.text == "final"
    assert res.structured == {"a": 1}
    assert res.cost_usd == pytest.approx(0.02)


def test_stream_output_without_result_is_transient(fake_claude, make_png, tmp_path):
    src = make_png(tmp_path / "src.png")
    fake_claude.queue(
        {"stdout_lines": [{"type": "system", "subtype": "init"}]},
        {"stdout": ok("fine")},
    )
    res = run(_image_call(src), sleep=lambda s: None)
    assert res.text == "fine"
    assert len(fake_claude.calls()) == 2


def test_stream_output_without_result_exhausts_retries(fake_claude, make_png, tmp_path):
    src = make_png(tmp_path / "src.png")
    fake_claude.queue(*[{"stdout_lines": [{"type": "system"}]}] * 4)
    call = _image_call(src)
    with pytest.raises(ClaudeError):
        run(call, sleep=lambda s: None)
    assert len(fake_claude.calls()) == 4


def test_stream_error_result_is_classified(fake_claude, make_png, tmp_path):
    src = make_png(tmp_path / "src.png")
    fake_claude.queue(
        {
            "stdout_lines": [
                {"type": "system"},
                {
                    "type": "result",
                    "subtype": "error_max_budget_usd",
                    "is_error": True,
                    "total_cost_usd": 0.11,
                },
            ],
            "exit": 1,
        }
    )
    call = _image_call(src)
    with pytest.raises(ClaudeBudgetExceeded) as ei:
        run(call)
    assert ei.value.cost_usd == pytest.approx(0.11)


def test_unreadable_image_is_refused(fake_claude, tmp_path):
    from llm_wiki.errors import RefusedSource

    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    call = _image_call(bad)
    with pytest.raises(RefusedSource):
        run(call)
    assert fake_claude.calls() == []


def _noise(path, size, fmt):
    import os

    from PIL import Image

    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(path, fmt)
    return path


def test_prepare_for_vision_downscales_2000px_webp(tmp_path):
    from PIL import Image, ImageDraw

    src = tmp_path / "big.webp"
    img = Image.new("RGB", (2000, 1000), "white")
    ImageDraw.Draw(img).rectangle((100, 100, 900, 600), fill="black")
    img.save(src, "WEBP")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = prepare_for_vision(src, out_dir)
    assert out.parent == out_dir
    with Image.open(out) as im:
        assert im.format in ("PNG", "JPEG")
        assert max(im.size) <= 1568
        assert im.size == (1568, 784)
    assert out.stat().st_size <= 3_750_000
    assert src.exists()


def test_prepare_for_vision_keeps_small_png_as_png(tmp_path):
    from PIL import Image

    src = _noise(tmp_path / "small.png", (100, 80), "PNG")
    out = prepare_for_vision(src, tmp_path)
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.size == (100, 80)


def test_prepare_for_vision_uses_jpeg_when_png_too_big(tmp_path):
    from PIL import Image

    src = _noise(tmp_path / "noise.png", (1600, 1200), "PNG")
    out = prepare_for_vision(src, tmp_path)
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert max(im.size) <= 1568
    assert out.suffix == ".jpg"
    assert out.stat().st_size <= 3_750_000


def test_prepare_for_vision_keeps_alpha_png(tmp_path):
    from PIL import Image

    src = tmp_path / "alpha.png"
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(src)
    out = prepare_for_vision(src, tmp_path)
    with Image.open(out) as im:
        assert im.format == "PNG"


@pytest.mark.parametrize("mode", ["CMYK", "YCbCr", "LAB", "F"])
def test_prepare_for_vision_handles_modes_png_cannot_store(tmp_path, mode):
    from PIL import Image

    src = tmp_path / "odd.tif"
    Image.new(mode, (50, 50)).save(src, "TIFF")
    out = prepare_for_vision(src, tmp_path)
    with Image.open(out) as im:
        assert im.format in ("PNG", "JPEG")
        assert im.size == (50, 50)


def test_prepare_for_vision_accepts_cmyk_jpeg(tmp_path):
    from PIL import Image

    src = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (50, 50), (0, 255, 255, 0)).save(src, "JPEG")
    out = prepare_for_vision(src, tmp_path)
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.mode == "RGB"


def _hanging_claude(tmp_path):
    """A claude that starts a grandchild, records its pid, then hangs."""
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "hang"
    script.write_text(
        f"#!/bin/sh\nsleep 60 &\necho $! > '{pid_file}'\nwait\n", encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, pid_file


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill(0); count it as gone
    stat_file = Path(f"/proc/{pid}/stat")
    return not (stat_file.exists() and stat_file.read_text().split()[2] == "Z")


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_timeout_kills_grandchild(tmp_path):
    script, pid_file = _hanging_claude(tmp_path)
    call = dataclasses.replace(_call(), timeout_s=1)
    assert claude_mod._spawn(call, str(script), tmp_path) is None
    pid = int(pid_file.read_text())
    try:
        assert _gone_within(pid, 2)
    finally:
        if _alive(pid):
            os.kill(pid, 9)


def test_timeout_kill_tolerates_gone_group(tmp_path, monkeypatch):
    script, _ = _hanging_claude(tmp_path)
    real = os.killpg

    def killpg(pgid, sig):  # the group dies just before the kill lands
        real(pgid, sig)
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", killpg)
    call = dataclasses.replace(_call(), timeout_s=1)
    assert claude_mod._spawn(call, str(script), tmp_path) is None
