#!/bin/sh
# pre-push hook, run by pre-commit: refuse a push whose target is main.
# pre-commit sets PRE_COMMIT_REMOTE_BRANCH to the remote ref being updated,
# so tag pushes and pushes of other branches from main still go through.

if [ "$PRE_COMMIT_REMOTE_BRANCH" = "refs/heads/main" ]; then
    branch=${PRE_COMMIT_LOCAL_BRANCH#refs/heads/}
    echo ""
    echo "Direct pushes to main are blocked. Push a branch and open a pull request:"
    echo "  git switch -c feat/<slug> && git push -u origin HEAD"
    if [ -n "$branch" ] && [ "$branch" != "main" ]; then
        echo "  https://github.com/leonardo-marziali/llm-wiki/compare/main...$branch"
    fi
    echo ""
    exit 1
fi
