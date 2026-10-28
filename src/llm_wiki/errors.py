"""Domain errors. The CLI renders any WikiError as `error: <msg>` with exit 2."""


class WikiError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigError(WikiError):
    pass


class WikiNotFound(WikiError):
    pass


class UnsupportedFormat(WikiError):
    pass


class RefusedSource(WikiError):
    pass


class GitError(WikiError):
    pass


class ClaudeError(WikiError):
    """A `claude -p` call failed. `cost_usd` is what the failed attempts billed."""

    def __init__(self, message: str, *, cost_usd: float = 0.0):
        super().__init__(message)
        self.cost_usd = cost_usd


class ClaudeNotInstalled(ClaudeError):
    pass


class ClaudeAuthError(ClaudeError):
    pass


class ClaudeUsageLimit(ClaudeError):
    pass


class ClaudeBudgetExceeded(ClaudeError):
    pass


class ClaudeSchemaError(ClaudeError):
    pass


# the errors that stop an ingest run: no later call can succeed
STOP = (ClaudeUsageLimit, ClaudeAuthError, ClaudeNotInstalled)
