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
    """A `claude -p` call failed."""


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
