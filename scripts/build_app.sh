#!/bin/zsh
set -euo pipefail
script_dir=${0:A:h}
repo_dir=${script_dir:h}
python_path=${LOGIC_MCP_PYTHON:-python3}
backend_path=${LOGIC_MCP_BACKEND:-$HOME/.local/bin/LogicProMCP-codex}
output_dir=${LOGIC_MCP_OUTPUT:-$repo_dir/dist}
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/logic-mcp-build.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT
[[ -x "$backend_path" ]] || { print -u2 "Set LOGIC_MCP_BACKEND to the supported LogicProMCP binary"; exit 1; }
"$python_path" -c 'import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from logic_pro_http_bridge import validate_backend; validate_backend(Path(sys.argv[2]))' "$repo_dir" "$backend_path"
app_dir="$build_dir/Logic MCP.app"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources/bin" "$output_dir"
xcrun swiftc -parse-as-library -O -target arm64-apple-macos14.0 \
  -framework AppKit -framework Foundation "$repo_dir/menubar/LogicMCPMenuBar.swift" \
  -o "$app_dir/Contents/MacOS/LogicMCPMenuBar"
"$python_path" -m PyInstaller --noconfirm --clean --onedir --name logic-mcp \
  --distpath "$build_dir/frozen" --workpath "$build_dir/work" --specpath "$build_dir" \
  --paths "$repo_dir" --collect-submodules mcp.server --collect-submodules mcp.client \
  --collect-submodules mcp.shared --collect-data mcp --collect-all mcp_types \
  --add-data "$repo_dir/logic_plugins_mcp.py:." \
  --hidden-import logic_plugins_mcp --hidden-import logic_audio_mcp \
  --hidden-import logic_pro_http_bridge --hidden-import run_mix_audit \
  "$repo_dir/logic_mcp_app.py"
ditto "$build_dir/frozen/logic-mcp" "$app_dir/Contents/Resources/runtime"
install -m 755 "$backend_path" "$app_dir/Contents/Resources/bin/LogicProMCP"
install -m 644 "$repo_dir/menubar/Info.plist" "$app_dir/Contents/Info.plist"
install -m 644 "$repo_dir/menubar/Help.html" "$app_dir/Contents/Resources/Help.html"
install -m 644 "$repo_dir/LICENSE" "$app_dir/Contents/Resources/LICENSE"
install -m 644 "$repo_dir/THIRD_PARTY_NOTICES.md" "$app_dir/Contents/Resources/THIRD_PARTY_NOTICES.md"
if [[ -n ${LOGIC_MCP_SHARE_DIR:-} ]]; then
  ditto "$LOGIC_MCP_SHARE_DIR" "$app_dir/Contents/Resources/share/logic-pro-mcp"
fi
# Sign every nested Mach-O executable/library, then the outer bundle.
identity=${LOGIC_MCP_SIGN_IDENTITY:--}
sign_args=(--force --sign "$identity")
if [[ "$identity" != "-" ]]; then sign_args+=(--timestamp --options runtime); fi
while IFS= read -r -d '' binary; do
  if /usr/bin/file -b "$binary" | /usr/bin/grep -q 'Mach-O'; then codesign "${sign_args[@]}" "$binary"; fi
done < <(find "$app_dir/Contents/Resources" -type f -print0)
codesign "${sign_args[@]}" "$app_dir/Contents/MacOS/LogicMCPMenuBar"
"$python_path" "$repo_dir/scripts/package_metadata.py" "$app_dir" "$app_dir/Contents/Resources/bin/LogicProMCP"
codesign "${sign_args[@]}" "$app_dir"
codesign --verify --deep --strict "$app_dir"
if [[ -n ${LOGIC_MCP_NOTARY_PROFILE:-} ]]; then
  [[ "$identity" != "-" ]] || { print -u2 'Notarization requires Developer ID'; exit 1; }
  ditto -c -k --keepParent "$app_dir" "$build_dir/notarize.zip"
  xcrun notarytool submit "$build_dir/notarize.zip" --keychain-profile "$LOGIC_MCP_NOTARY_PROFILE" --wait
  xcrun stapler staple "$app_dir"
fi
target="$output_dir/Logic MCP.app"
[[ ! -e "$target" ]] || { print -u2 "Output exists: $target. Choose a new LOGIC_MCP_OUTPUT directory"; exit 1; }
ditto "$app_dir" "$target"
ditto -c -k --keepParent "$target" "$output_dir/Logic-MCP-macos-arm64.zip"
mkdir "$build_dir/dmg"
ditto "$target" "$build_dir/dmg/Logic MCP.app"
ln -s /Applications "$build_dir/dmg/Applications"
hdiutil create -quiet -volname 'Logic MCP' -srcfolder "$build_dir/dmg" -ov -format UDZO "$output_dir/Logic-MCP-macos-arm64.dmg"
(cd "$output_dir" && shasum -a 256 Logic-MCP-macos-arm64.zip Logic-MCP-macos-arm64.dmg > SHA256SUMS)
print "$target"
