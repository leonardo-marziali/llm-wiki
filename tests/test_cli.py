import pytest
from typer.testing import CliRunner

from llm_wiki import __version__, errors
from llm_wiki.cli import app


def test_version_flag():
    r = CliRunner().invoke(app, ["--version"])
    assert r.exit_code == 0 and r.stdout.strip() == f"wiki {__version__}"


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
    "name", ["ConfigError", "WikiNotFound", "UnsupportedFormat", "RefusedSource", "GitError"]
)
def test_error_hierarchy(name):
    assert issubclass(getattr(errors, name), errors.WikiError)
