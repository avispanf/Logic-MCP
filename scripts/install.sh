#!/bin/zsh
# Developer installation. End users install the prebuilt app from Releases.
set -euo pipefail
script_dir=${0:A:h}
repo_dir=${script_dir:h}
python_path=${LOGIC_MCP_PYTHON:-python3}
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/logic-mcp-install.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT
"$python_path" -m venv "$build_dir/venv"
"$build_dir/venv/bin/python" -m pip install -r "$repo_dir/requirements-lock.txt"
LOGIC_MCP_PYTHON="$build_dir/venv/bin/python" LOGIC_MCP_OUTPUT="$build_dir/output" "$script_dir/build_app.sh"
target="$HOME/Applications/Logic MCP.app"
if [[ -e "$target" ]]; then
  identifier=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$target/Contents/Info.plist")
  [[ "$identifier" == com.avispanf.logic-mcp-menubar ]] || { print -u2 'Unexpected existing application'; exit 1; }
  backup="$target.backup-$(date +%Y%m%d%H%M%S)"
  mv "$target" "$backup"
fi
mkdir -p "$HOME/Applications"
ditto "$build_dir/output/Logic MCP.app" "$target"
"$target/Contents/Resources/runtime/logic-mcp" --control setup
open "$target"
print "Installed: $target"
