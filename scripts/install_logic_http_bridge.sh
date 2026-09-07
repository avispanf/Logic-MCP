#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_dir=${script_dir:h}
python_path="${LOGIC_MCP_PYTHON:-$HOME/dev/venv/bin/python}"
bridge_path="$repo_dir/logic_pro_http_bridge.py"
template_path="$repo_dir/bridge/com.avispanf.logic-pro-mcp-bridge.plist"
launch_agent="$HOME/Library/LaunchAgents/com.avispanf.logic-pro-mcp-bridge.plist"
label="com.avispanf.logic-pro-mcp-bridge"
uid=$(id -u)
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/logic-pro-mcp-bridge.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT

existing_pid=$(launchctl print "gui/$uid/$label" 2>/dev/null | awk '$1 == "pid" && $2 == "=" { print $3; exit }' || true)
listener_pid=$(/usr/sbin/lsof -nP -t -iTCP:8765 -sTCP:LISTEN 2>/dev/null | head -n 1 || true)
if [[ -n "$listener_pid" && "$listener_pid" != "$existing_pid" ]]; then
    print -u2 "Port 8765 is already owned by PID $listener_pid, not $label."
    exit 1
fi

[[ -x "$python_path" ]] || { print -u2 "Python is not executable: $python_path"; exit 1; }
[[ -f "$bridge_path" ]] || { print -u2 "Bridge source is missing: $bridge_path"; exit 1; }
"$python_path" -m py_compile "$bridge_path"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
install -m 644 "$template_path" "$build_dir/launch-agent.plist"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 $python_path" "$build_dir/launch-agent.plist"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:1 $bridge_path" "$build_dir/launch-agent.plist"
/usr/libexec/PlistBuddy -c "Set :StandardOutPath $HOME/Library/Logs/LogicProMCPBridge.log" "$build_dir/launch-agent.plist"
/usr/libexec/PlistBuddy -c "Set :StandardErrorPath $HOME/Library/Logs/LogicProMCPBridge.error.log" "$build_dir/launch-agent.plist"
plutil -lint "$build_dir/launch-agent.plist" >/dev/null

launchctl bootout "gui/$uid/$label" 2>/dev/null || true
install -m 644 "$build_dir/launch-agent.plist" "$launch_agent"
launchctl enable "gui/$uid/$label"

bootstrap_error=""
for _ in {1..20}; do
    if bootstrap_error=$(launchctl bootstrap "gui/$uid" "$launch_agent" 2>&1); then
        bootstrap_error=""
        break
    fi
    sleep 0.1
done
if [[ -n "$bootstrap_error" ]]; then
    print -u2 "$bootstrap_error"
    exit 1
fi

for _ in {1..30}; do
    health=$(/usr/bin/curl --silent --fail --max-time 1 http://127.0.0.1:8765/health 2>/dev/null || true)
    if [[ "$health" == *'"ready":true'* && "$health" == *'"instance_id":"bridge"'* ]]; then
        print -r -- "$health"
        print
        exit 0
    fi
    sleep 0.2
done

print -u2 "Bridge did not become healthy; see $HOME/Library/Logs/LogicProMCPBridge.error.log"
exit 1
