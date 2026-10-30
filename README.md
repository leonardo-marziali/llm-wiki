# llm-wiki

[![PyPI](https://img.shields.io/pypi/v/llm-wiki-tool)](https://pypi.org/p/llm-wiki-tool)
[![Python](https://img.shields.io/pypi/pyversions/llm-wiki-tool)](https://pypi.org/p/llm-wiki-tool)

`wiki` keeps small, capped, LLM-maintained wikis that coding agents read. You
drop source files (documents, slides, sheets, PDFs, images, notes) into a wiki;
`wiki ingest` has headless Claude Code plan and write cited pages on a branch,
and you accept or reject the result at one gate. Agents read the wiki through
the read-only `wiki-lookup` skill, at no LLM cost.

## Requirements

- Python 3.12 or later
- git, with `user.name` and `user.email` configured
- [Claude Code](https://code.claude.com) logged in with a subscription
  (`claude /login`); `wiki` never uses an API key

## Install

```sh
pipx install llm-wiki-tool      # or: pipx install 'llm-wiki-tool[docling]'
wiki doctor                     # checks claude, its flags, login, isolation and image input
```

The package is `llm-wiki-tool` on PyPI; the command it installs is `wiki`.
Releases and their notes are in [CHANGELOG.md](CHANGELOG.md).

## Quick start

```sh
wiki init ~/wikis/payments --description "Payments team conventions and architecture"
cp design.docx diagram.png ~/wikis/payments/inbox/
wiki ingest --wiki payments     # plan, write on a branch, then accept or reject
wiki search "refund flow" --wiki payments
wiki status --all
wiki skill install              # the wiki-lookup skill, for every repo
```

A wiki is either its own git repo or a folder inside an existing repo. Wikis are
found through `--wiki NAME|PATH`, the current directory, the registry
(`$LLM_WIKI_HOME/wikis.toml`, written by `wiki init`) or `LLM_WIKI_PATH`, a list
of wiki directories separated by the platform path separator. Running
`wiki init` on an existing or cloned wiki only registers it.

Claude Code asks once before it runs the skill. Once you approve, the skill's
own `allowed-tools` cover `wiki status`, `wiki search` and reading pages. To
skip the prompt, for example in headless `claude -p` runs, add
`"Skill(wiki-lookup)"` to `permissions.allow` in your Claude Code settings.

To make one repo prefer a wiki, run
`wiki skill install --project <repo> --pointer --wiki <name>`. It writes a short
marked block into `<repo>/CLAUDE.md`.

## Commands

| Command              | What it does                                                     |
| -------------------- | ---------------------------------------------------------------- |
| `wiki init`          | Create or register a wiki                                        |
| `wiki ingest`        | Ingest the inbox, paths or `--note` text, then one gate          |
| `wiki search`        | Ranked page search for agents (`--format json`)                  |
| `wiki status`        | Usage against caps, stale pages, pending and quarantined         |
| `wiki lint`          | Deterministic checks of pages, links, citations and caps         |
| `wiki lint --deep`   | Also a sampled LLM pass; writes `.wiki/reports/lint-<date>.md`   |
| `wiki lint --fix`    | Repair the findings on a branch, then one gate (`--yes`)         |
| `wiki consolidate`   | Split, merge, promote and archive pages, then one gate (`--yes`) |
| `wiki skill install` | Install the read-only `wiki-lookup` skill                        |
| `wiki doctor`        | Check `claude`, its flags, login, isolation and read denial      |

## Caps and consolidate

Every wiki has hard caps (500 pages, 100 per section, 100 sources); `wiki.toml`
may only lower them, under `[caps]`, and each `warn_*` level defaults to 80% of
its `max_*`. Near a cap, `wiki status` prints a `near cap:` line. At the page
cap, ingest still updates existing pages but refuses new ones: the source is
kept in `raw/` as failed, and the message ends with "run `wiki consolidate` or
start a new wiki". `wiki consolidate` then frees room on a branch, behind the
same gate: it retargets links to retired pages, merges duplicate titles (one
page becomes a `superseded` stub; a to-be twin of an as-is page or a retired
page is left to you), splits pages over 400 lines while the cap leaves room for
one new page, and promotes to-be pages whose fact is now built (at most 3 a
run). Retired pages that nothing links to any more move to
`<section>/_archive/`, and their ids are never reused for topic pages (a source
page's id follows its raw path, so a raw file re-added at the same path gets its
`src-…` id back); the summary names each retired page that still waits and the
files that link it. At the source cap, ingest refuses new sources instead: start
a new wiki or delete obsolete raw sources. Maintenance never adds evidence or
refreshes `last_verified`; stale pages and changed sources stay yours to
re-check. The next `wiki ingest` retries the failed source.

A page with a secret or injection finding gets no maintenance job, and its text
never enters a plan or `--deep` prompt; when `wiki/open-questions.md` has such a
finding, no edit may change it. Edit sessions work in the wiki root with file
tools; `claude` denies them reading the flagged files and `.wiki/quarantine/`
(`Read(./…)` rules passed with `--disallowed-tools`, which also keep those files
out of Grep), and `wiki doctor` probes that the denial holds (`read-denial`).

## Upgrading existing wikis

`wiki init` writes a wiki's `AGENTS.md` (its rules for the model) once, and the
tool never rewrites it. Wikis created by an older version keep their old rules.
To pick up new rules, compare your `AGENTS.md` with the packaged template
(`llm_wiki/templates/AGENTS.md` in the installed package) and copy the changes
by hand, keeping your wiki's own name, description and sections table.

Wikis created before `wiki lint --deep` existed lack `.wiki/reports/` in their
`.gitignore`. Add that line so the deep reports stay out of git; the commands
work either way.

Wikis created before 0.1.1 also lack `.DS_Store`, `Thumbs.db` and `desktop.ini`
in their `.gitignore`. Once Finder or Explorer drops one of these files outside
`inbox/` and `raw/`, `wiki ingest` refuses to start ("the wiki has uncommitted
changes"). Add the three lines and commit `.gitignore`.
