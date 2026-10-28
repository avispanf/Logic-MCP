#!/bin/zsh
set -euo pipefail
app="$HOME/Applications/Logic MCP.app/Contents/Resources/runtime/logic-mcp"
[[ -x "$app" ]] || app="/Applications/Logic MCP.app/Contents/Resources/runtime/logic-mcp"
if [[ -x "$app" ]]; then
  exec "$app" --control setup
fi
exec "${0:A:h}/install.sh" "$@"
