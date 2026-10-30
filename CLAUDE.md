@AGENTS.md

# Claude Code

- The Superpowers plugin is enabled for this project in `.claude/settings.json`.
  Use its skills for the workflow in `AGENTS.md`: `brainstorming`,
  `writing-plans`, `subagent-driven-development` (or `executing-plans`),
  `test-driven-development`, `systematic-debugging`, `requesting-code-review`
  and `verification-before-completion`.
- The DevContainer image adds the user-level tooling: the pyright LSP, and
  Markdown, Prettier, Python and SonarQube hooks that check the files you touch.
  Fix what they report before you move on.
- `.claude/settings.local.json` is the user's own; never commit it.
