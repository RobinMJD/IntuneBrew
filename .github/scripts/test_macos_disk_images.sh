#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HELPER="$ROOT/.github/scripts/macos_disk_image.py"
TEST_ROOT=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/intunebrew-disk-test.XXXXXX")
OUTER_IMAGE="$TEST_ROOT/workspace.sparseimage"
OUTER_MOUNT="$TEST_ROOT/workspace"
OUTER_DEVICE=""
INNER_DEVICE=""

resolve_test_device() {
  local image_path="$1"
  local info_plist="$TEST_ROOT/cleanup-info.plist"
  local attachment=""
  local status=0
  if ! hdiutil info -plist > "$info_plist" 2>/dev/null; then
    return 3
  fi
  attachment=$(python "$HELPER" info --plist "$info_plist" \
    --image "$image_path" 2>/dev/null) || status=$?
  [ "$status" -eq 0 ] || return "$status"
  printf '%s' "$attachment" | jq -r '.whole_device // empty'
}

cleanup() {
  local status=$?
  local cleanup_status=0
  local resolve_status=0
  trap - EXIT
  cd "$TEST_ROOT" 2>/dev/null || cd "${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
  if [ -z "$INNER_DEVICE" ] && [ -f "$OUTER_MOUNT/nested.dmg" ]; then
    if INNER_DEVICE=$(resolve_test_device "$OUTER_MOUNT/nested.dmg"); then
      :
    else
      resolve_status=$?
      [ "$resolve_status" -eq 1 ] || cleanup_status=1
    fi
  fi
  if [ -n "$INNER_DEVICE" ] \
    && ! hdiutil detach "$INNER_DEVICE" -force >/dev/null 2>&1; then
    echo "Could not detach nested test image $INNER_DEVICE" >&2
    cleanup_status=1
  else
    INNER_DEVICE=""
  fi
  if [ "$cleanup_status" -eq 0 ] \
    && [ -z "$OUTER_DEVICE" ] \
    && [ -f "$OUTER_IMAGE" ]; then
    if OUTER_DEVICE=$(resolve_test_device "$OUTER_IMAGE"); then
      :
    else
      resolve_status=$?
      [ "$resolve_status" -eq 1 ] || cleanup_status=1
    fi
  fi
  if [ "$cleanup_status" -eq 0 ] \
    && [ -n "$OUTER_DEVICE" ] \
    && ! hdiutil detach "$OUTER_DEVICE" -force >/dev/null 2>&1; then
    echo "Could not detach workspace test image $OUTER_DEVICE" >&2
    cleanup_status=1
  else
    OUTER_DEVICE=""
  fi
  if [ "$cleanup_status" -eq 0 ]; then
    rm -rf "$TEST_ROOT"
  else
    echo "Preserving test images after unsafe cleanup: $TEST_ROOT" >&2
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT

mkdir -p "$OUTER_MOUNT"
hdiutil create -quiet -size 256m -fs APFS -type SPARSE \
  -volname IntuneBrewWorkspaceTest "$OUTER_IMAGE"
hdiutil attach "$OUTER_IMAGE" -mountpoint "$OUTER_MOUNT" -nobrowse -plist \
  > "$TEST_ROOT/outer-attach.plist"
outer=$(python "$HELPER" attach --plist "$TEST_ROOT/outer-attach.plist")
OUTER_DEVICE=$(printf '%s' "$outer" | jq -r .whole_device)
test "$OUTER_DEVICE" = "${OUTER_DEVICE%%s[0-9]*}"

mkdir -p "$TEST_ROOT/payload"
printf 'nested payload\n' > "$TEST_ROOT/payload/proof.txt"
hdiutil create -quiet -fs HFS+ -srcfolder "$TEST_ROOT/payload" \
  -volname IntuneBrewNestedTest "$OUTER_MOUNT/nested.dmg"
hdiutil attach "$OUTER_MOUNT/nested.dmg" -readonly -nobrowse -plist \
  > "$OUTER_MOUNT/inner-attach.plist"
inner=$(python "$HELPER" attach --plist "$OUTER_MOUNT/inner-attach.plist")
INNER_DEVICE=$(printf '%s' "$inner" | jq -r .whole_device)
inner_mount=$(printf '%s' "$inner" | jq -r '.mounted_entities[0].mount_point')
test -f "$inner_mount/proof.txt"

cd "$TEST_ROOT"
hdiutil detach "$INNER_DEVICE" -force
INNER_DEVICE=""
hdiutil detach "$OUTER_DEVICE" -force
OUTER_DEVICE=""
echo "macOS disk image lifecycle passed"
