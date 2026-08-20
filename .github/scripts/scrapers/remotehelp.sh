#!/bin/bash
set -euo pipefail

download_url="https://aka.ms/downloadremotehelpmacos"
temp_pkg=$(mktemp "${TMPDIR:-/tmp}/remotehelp.XXXXXX.pkg")
trap 'rm -f "$temp_pkg"' EXIT

effective_url=$(curl -fLsS --connect-timeout 30 --max-time 600 --retry 3 \
  --max-filesize 2147483648 -o "$temp_pkg" -w '%{url_effective}' "$download_url")
[ -s "$temp_pkg" ] || { echo "Remote Help download was empty" >&2; exit 1; }

sha=$(shasum -a 256 "$temp_pkg" | awk '{print $1}')
filename=$(basename "${effective_url%%\?*}")
version=$(printf '%s' "$filename" \
  | sed -n 's/^Microsoft_Remote_Help_\([0-9][0-9.]*\)_installer\.pkg$/\1/p')
[ -n "$version" ] || {
  echo "Unexpected Remote Help installer filename: $filename" >&2
  exit 1
}
package_id="com.microsoft.remotehelp"

cat > Apps/remotehelp.json <<EOF
{
  "name": "Remote Help",
  "description": "Microsoft Remote Help for secure help desk connections with role-based access controls",
  "version": "$version",
  "source_version": "$version",
  "url": "$effective_url",
  "vendor_url": "$download_url",
  "bundleId": "$package_id",
  "bundleId_source": "override",
  "homepage": "https://learn.microsoft.com/en-us/mem/intune/fundamentals/remote-help-macos",
  "fileName": "$filename",
  "sha": "$sha",
  "source_sha256": "$sha",
  "artifact_kind": "pkg",
  "artifact_pkg": "$filename",
  "type": "app"
}
EOF
