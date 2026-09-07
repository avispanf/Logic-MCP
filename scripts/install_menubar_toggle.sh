#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_dir=${script_dir:h}
source_file="$repo_dir/menubar/LogicMCPMenuBar.swift"
info_file="$repo_dir/menubar/Info.plist"
python_path="${LOGIC_MCP_PYTHON:-$HOME/dev/venv/bin/python}"
[[ -x "$python_path" ]] || { print -u2 "Missing Python: $python_path"; exit 1; }
launch_agent_template="$repo_dir/menubar/LaunchAgent.plist"
app_dir="$HOME/Applications/Logic MCP MCU Toggle.app"
contents_dir="$app_dir/Contents"
launch_agent="$HOME/Library/LaunchAgents/com.avispanf.logic-mcp-menubar.plist"
label="com.avispanf.logic-mcp-menubar"
uid=$(id -u)
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/logic-mcp-menubar.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT

xcrun swiftc -parse-as-library -O \
    -framework AppKit -framework Foundation \
    "$source_file" \
    -o "$build_dir/LogicMCPMenuBar"

if [[ -d "$app_dir" ]]; then
    existing_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app_dir/Contents/Info.plist" 2>/dev/null || true)
    if [[ "$existing_id" != "$label" ]]; then
        print -u2 "Refusing to replace unexpected app at $app_dir"
        exit 1
    fi
fi

mkdir -p "$contents_dir/MacOS" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
launchctl bootout "gui/$uid/$label" 2>/dev/null || true
pkill -x LogicMCPMenuBar 2>/dev/null || true
install -m 755 "$build_dir/LogicMCPMenuBar" "$contents_dir/MacOS/LogicMCPMenuBar"
install -m 644 "$info_file" "$contents_dir/Info.plist"
/usr/libexec/PlistBuddy -c "Add :MCPPythonPath string $python_path" "$contents_dir/Info.plist"
/usr/libexec/PlistBuddy -c "Add :MCPHelperPath string $repo_dir/logic_manual_mode.py" "$contents_dir/Info.plist"
codesign --force --sign - "$app_dir" >/dev/null

install -m 644 "$launch_agent_template" "$build_dir/launch-agent.plist"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 $contents_dir/MacOS/LogicMCPMenuBar" "$build_dir/launch-agent.plist"
/usr/libexec/PlistBuddy -c "Set :StandardOutPath $HOME/Library/Logs/LogicMCPMenuBar.log" "$build_dir/launch-agent.plist"
/usr/libexec/PlistBuddy -c "Set :StandardErrorPath $HOME/Library/Logs/LogicMCPMenuBar.error.log" "$build_dir/launch-agent.plist"
plutil -lint "$build_dir/launch-agent.plist" >/dev/null
install -m 644 "$build_dir/launch-agent.plist" "$launch_agent"
launchctl enable "gui/$uid/$label"
for attempt in {1..20}; do
    if launchctl bootstrap "gui/$uid" "$launch_agent"; then
        print "$app_dir"
        exit 0
    fi
    sleep 0.2
done
print -u2 "LaunchAgent failed to start: $label"
exit 1
