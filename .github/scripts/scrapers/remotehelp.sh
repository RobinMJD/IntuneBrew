#!/bin/bash
set -euo pipefail

download_url="https://aka.ms/downloadremotehelpmacos"
temp_pkg=$(mktemp "${TMPDIR:-/tmp}/remotehelp.XXXXXX.pkg")
expand_dir=$(mktemp -d "${TMPDIR:-/tmp}/remotehelp-expand.XXXXXX")
trap 'rm -f "$temp_pkg"; rm -rf "$expand_dir"' EXIT

effective_url=$(curl -fLsS -o "$temp_pkg" -w '%{url_effective}' "$download_url")
[ -s "$temp_pkg" ] || { echo "Remote Help download was empty" >&2; exit 1; }

sha=$(shasum -a 256 "$temp_pkg" | awk '{print $1}')
pkgutil --expand-full "$temp_pkg" "$expand_dir/package" >/dev/null
package_info=$(find "$expand_dir/package" -type f -name PackageInfo -print \
  | awk '{ print length, $0 }' | sort -n | head -1 | cut -d" " -f2-)
[ -n "$package_info" ] || { echo "Remote Help package has no PackageInfo" >&2; exit 1; }
package_id=$(sed -n 's/.*identifier="\([^"]*\)".*/\1/p' "$package_info" | head -1)
version=$(sed -n 's/.*version="\([^"]*\)".*/\1/p' "$package_info" | head -1)
[ -n "$package_id" ] || { echo "Remote Help PackageInfo has no identifier" >&2; exit 1; }

filename=$(basename "${effective_url%%\?*}")
case "$filename" in
  *.pkg) ;;
  *) filename="RemoteHelp.pkg" ;;
esac

if [ -z "$version" ]; then
  version=$(curl -fLsS "https://learn.microsoft.com/en-us/intune/intune-service/fundamentals/remote-help-macos" \
    | grep -o '<strong>[0-9.]*</strong>' | sed 's/<[^>]*>//g' | head -1)
fi
[ -n "$version" ] || { echo "Could not determine Remote Help version" >&2; exit 1; }

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
