#!/usr/bin/env bash
# PostToolUse hook: enforce docs/development-practices.md on every Python edit.
#
# Silently auto-fixes formatting and lint. Surfaces back to Claude only what it
# cannot fix: remaining ruff violations and import-contract breaches (§1.3),
# since those mean an architectural seam has leaked.
#
# No-ops cleanly when the project is not yet scaffolded, uv is missing, or the
# package is not yet installed into the venv.

f=$(jq -r '.tool_response.filePath // .tool_input.file_path // empty')
[ -n "$f" ] || exit 0
case "$f" in *.py) ;; *) exit 0 ;; esac

root=$(git -C "$(dirname "$f")" rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -n "$root" ] && [ -f "$root/pyproject.toml" ] || exit 0
cd "$root" || exit 0
command -v uv >/dev/null 2>&1 || exit 0

# import-linter prints a large box-drawing banner; drop lines that are only art.
strip_banner() { grep -vE '^[[:space:]╔═╗╝║╚╣╠╦╩╬─│┌┐└┘▶◀▲▼]*$'; }

problems=""

uv run --quiet ruff check --fix "$f" >/dev/null 2>&1
uv run --quiet ruff format "$f" >/dev/null 2>&1

if ! remaining=$(uv run --quiet ruff check "$f" 2>&1); then
  problems="ruff (not auto-fixable):
$remaining"
fi

if grep -q 'tool\.importlinter' pyproject.toml 2>/dev/null; then
  if ! imports=$(uv run --quiet lint-imports 2>&1); then
    # "Could not find package" means the project isn't installed yet, not that a
    # contract was breached. Stay silent rather than crying wolf every edit.
    case "$imports" in
      *"Could not find package"*) ;;
      *)
        imports=$(printf '%s\n' "$imports" | strip_banner)
        problems="$problems

import contracts (see docs/development-practices.md §1.3):
$imports"
        ;;
    esac
  fi
fi

[ -n "${problems// /}" ] || exit 0

jq -n --arg p "$problems" --arg f "$f" '{
  systemMessage: "Practices check failed — see docs/development-practices.md",
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: ("Automated practices check failed after editing \($f).\nFix before continuing.\n\n\($p)")
  }
}'
