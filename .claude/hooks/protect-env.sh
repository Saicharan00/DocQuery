#!/usr/bin/env bash
# PreToolUse hook: block any tool from touching a real .env secrets file.
# Allows .env.example / .env.sample / .env.template (safe placeholders).
# Reads the tool-call JSON on stdin; emits a "deny" decision when a secret
# file is referenced, otherwise stays silent (which allows the call).

data=$(cat)

# Collect every field that could name a file or contain a shell command.
target=$(printf '%s' "$data" | jq -r '
  [ .tool_input.file_path,
    .tool_input.command,
    .tool_input.path,
    .tool_input.notebook_path,
    .tool_input.pattern ]
  | map(select(. != null)) | join(" ")')

# Reference to .env* that is NOT one of the allowed placeholder templates.
if printf '%s' "$target" | grep -Eq '\.env' \
   && ! printf '%s' "$target" | grep -Eq '\.env\.(example|sample|template)'; then
  printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Blocked access to a real .env secrets file. Only .env.example (empty placeholders) is readable — reference the variable name (e.g. OPENAI_API_KEY), never its value."}}'
fi
