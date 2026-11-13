#!/usr/bin/env bash
# Stop hook: if the working tree has changes but CLAUDE.md is not among them,
# ask Claude (once per stop) to check whether CLAUDE.md needs updating.
input=$(cat)
# Already continued once because of this hook: don't loop.
if printf '%s' "$input" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi
changed=$(git status --porcelain 2>/dev/null | sed 's/^...//')
[ -z "$changed" ] && exit 0
if printf '%s\n' "$changed" | grep -qx 'CLAUDE.md'; then
  exit 0
fi
cat <<'JSON'
{"decision":"block","reason":"Files changed but CLAUDE.md did not. Check whether CLAUDE.md (scripts, flags, outputs, rules, CI, conventions, status/roadmap) needs updating and update it. If nothing there is affected, reply that no update is needed."}
JSON
