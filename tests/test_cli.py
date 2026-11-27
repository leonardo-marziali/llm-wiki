import pytest
from typer.testing import CliRunner

from llm_wiki import __version__, errors
from llm_wiki.cli import app


def test_version_flag():
    r = CliRunner().invoke(app, ["--version"])
    assert r.exit_code == 0
    assert r.stdout.strip() == f"wiki {__version__}"


def test_no_args_shows_help():
    r = CliRunner().invoke(app, [])
    assert "Usage" in r.stdout


@pytest.fixture
def boom_command():
    """Register throwaway commands on the real app, removing them afterwards."""
    before = list(app.registered_commands)

    @app.command("boom")
    def boom():
        raise errors.ConfigError("bad config")

    @app.command("crash")
    def crash():
        raise RuntimeError("unexpected")

    yield
    app.registered_commands[:] = before


def test_wiki_error_exits_2_with_stderr_message(boom_command):
    r = CliRunner().invoke(app, ["boom"])
    assert r.exit_code == 2
    assert r.stderr.strip() == "error: bad config"
    assert "Traceback" not in r.stdout + r.stderr


def test_non_wiki_errors_are_not_swallowed(boom_command):
    r = CliRunner().invoke(app, ["crash"])
    assert isinstance(r.exception, RuntimeError)


@pytest.mark.parametrize(
    "name",
    ["ConfigError", "WikiNotFound", "UnsupportedFormat", "RefusedSource", "GitError"],
)
def test_error_hierarchy(name):
    assert issubclass(getattr(errors, name), errors.WikiError)


def test_cli_ingest_yes_exit_codes(tmp_path, make_wiki, fake_claude):
    from importlib import resources

    from test_ingest_run import ingest_responses

    from llm_wiki import gitops

    root = make_wiki(tmp_path)
    (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    gitignore = resources.files("llm_wiki") / "templates" / "gitignore"
    (root / ".gitignore").write_text(gitignore.read_text("utf-8"), encoding="utf-8")
    gitops.init_repo(root)
    gitops.commit_all(root, "init")
    runner = CliRunner()

    r = runner.invoke(app, ["ingest", "--wiki", str(root), "--yes"])
    assert r.exit_code == 0, r.output
    assert "nothing to do" in r.stdout

    (root / "inbox").mkdir(exist_ok=True)
    (root / "inbox" / "new.md").write_text("# New\n\nRebase.\n", encoding="utf-8")
    r = runner.invoke(app, ["ingest", "--wiki", str(root)])  # stdin is no terminal
    assert r.exit_code == 2
    assert "the gate needs a terminal; pass --yes" in r.stderr
    assert fake_claude.calls() == []

    # a broken link in open-questions.md fails finalize's lint: --yes rejects, exit 1
    plan, edit = ingest_responses("raw/new.md", "# New\n\nRebase.\n", "new-md")
    edit["write"]["wiki/open-questions.md"] = "# Open questions\n\n- [x](nope.md)\n"
    fake_claude.queue(plan, edit)
    r = runner.invoke(app, ["ingest", "--wiki", str(root), "--yes"])
    assert r.exit_code == 1, r.output
    assert "rejected" in r.stdout
    assert (root / "inbox" / "new.md").is_file()
    assert gitops.current_branch(root) == "main"


def _init(tmp_path, name="acme"):
    from llm_wiki.init import init_wiki

    return init_wiki(tmp_path / name, name=name, description="Old.").ref


def test_describe_command(tmp_path):
    _init(tmp_path)
    r = CliRunner().invoke(app, ["describe", "New text.", "--wiki", "acme"])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == "described wiki acme"


def test_rename_command_prints_pointer_hint(tmp_path):
    _init(tmp_path)
    r = CliRunner().invoke(app, ["rename", "beta", "--wiki", "acme"])
    assert r.exit_code == 0, r.output
    assert r.stdout.splitlines() == [
        "renamed wiki acme to beta",
        "update project pointers with: wiki skill install --project DIR --pointer",
    ]


def test_rename_command_bad_name_exits_2(tmp_path):
    _init(tmp_path)
    r = CliRunner().invoke(app, ["rename", "Bad Name", "--wiki", "acme"])
    assert r.exit_code == 2
    assert r.stderr.startswith("error: invalid wiki name")


def test_move_command(tmp_path):
    _init(tmp_path)
    target = tmp_path / "moved"
    r = CliRunner().invoke(app, ["move", str(target), "--wiki", "acme"])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == f"moved wiki acme to {target.resolve()}"


def test_move_command_non_empty_target_exits_2(tmp_path):
    _init(tmp_path)
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    r = CliRunner().invoke(app, ["move", str(tmp_path / "full"), "--wiki", "acme"])
    assert r.exit_code == 2
    assert "is not empty" in r.stderr


def test_describe_command_warns_on_stderr_and_host_repo(tmp_path):
    import subprocess

    from llm_wiki.init import init_wiki

    host = tmp_path / "host"
    host.mkdir()
    subprocess.run(["git", "-C", str(host), "init", "-q"], check=True)
    root = init_wiki(host / "w", name="acme", description="Old.").ref.path
    ov = root / "wiki" / "overview.md"
    ov.write_text(ov.read_text().replace("**TL;DR** Old.", "**TL;DR** Mine."))
    subprocess.run(["git", "-C", str(host), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(host), "commit", "-qm", "chore: wiki"], check=True)
    r = CliRunner().invoke(app, ["describe", "New.", "--wiki", "acme"])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == (
        "described wiki acme (not committed — review and commit it in "
        f"{host.resolve()})"
    )
    assert r.stderr.strip() == "warning: wiki/overview.md: TL;DR was edited; left as is"
