#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HELPER="$ROOT/.github/scripts/macos_disk_image.py"
TEST_ROOT=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/intunebrew-disk-test.XXXXXX")
OUTER_IMAGE="$TEST_ROOT/workspace.sparseimage"
OUTER_MOUNT="$TEST_ROOT/workspace"
OUTER_DEVICE=""
INNER_IMAGE="$OUTER_MOUNT/nested.dmg"
INNER_DEVICE=""
DETACH_ATTEMPTS=4
DETACH_SLEEP_SECONDS=1

resolve_test_attachment() {
  local image_path="$1"
  local info_plist=""
  local attachment=""
  local status=0
  info_plist=$(mktemp "$TEST_ROOT/hdiutil-info.XXXXXX") || return 3
  if ! hdiutil info -plist > "$info_plist" 2>/dev/null; then
    rm -f "$info_plist"
    return 3
  fi
  attachment=$(python "$HELPER" info --plist "$info_plist" \
    --image "$image_path" 2>/dev/null) || status=$?
  rm -f "$info_plist"
  [ "$status" -eq 0 ] || return "$status"
  printf '%s\n' "$attachment"
}

device_is_unmounted() {
  local device="$1"
  local mount_output=""
  mount_output=$(mount) || return 1
  ! printf '%s\n' "$mount_output" | awk -v device="$device" '
    $1 == device || index($1, device "s") == 1 { found=1 }
    END { exit(found ? 0 : 1) }
  '
}

retry_force_unmount() {
  local operation="$1"
  local device="$2"
  local attempt=1
  while [ "$attempt" -le "$DETACH_ATTEMPTS" ]; do
    if diskutil "$operation" force "$device" >/dev/null 2>&1; then
      return 0
    fi
    device_is_unmounted "$device" && return 0
    [ "$attempt" -ge "$DETACH_ATTEMPTS" ] || sleep "$DETACH_SLEEP_SECONDS"
    attempt=$((attempt + 1))
  done
  return 1
}

force_unmount_attachment() {
  local attachment="$1"
  local device=""
  while IFS= read -r device; do
    [ -z "$device" ] || retry_force_unmount unmount "$device"
  done < <(printf '%s' "$attachment" | jq -r \
    '.mounted_entities[]?.device')
  while IFS= read -r device; do
    [ -z "$device" ] || retry_force_unmount unmountDisk "$device"
  done < <(printf '%s' "$attachment" | jq -r \
    '.synthesized_devices[]?')
}

TEST_DETACH_DEVICE=""
detach_test_image() {
  local image_path="$1"
  local whole_device="$2"
  local attachment=""
  local attempt=1
  local detach_output=""
  local detach_status=0
  local resolve_status=0
  TEST_DETACH_DEVICE="$whole_device"
  while [ "$attempt" -le "$DETACH_ATTEMPTS" ]; do
    detach_status=0
    detach_output=$(hdiutil detach "$TEST_DETACH_DEVICE" -force 2>&1) \
      || detach_status=$?
    attachment=""
    resolve_status=0
    attachment=$(resolve_test_attachment "$image_path") || resolve_status=$?
    [ "$resolve_status" -ne 1 ] || return 0
    [ "$resolve_status" -eq 0 ] || return 1
    TEST_DETACH_DEVICE=$(printf '%s' "$attachment" | jq -r \
      '.backing_device // .whole_device // empty')
    [ -n "$TEST_DETACH_DEVICE" ] || return 1
    if [ "$detach_status" -ne 0 ] \
      && ! printf '%s' "$detach_output" | grep -qi "Resource busy"; then
      return 1
    fi
    [ "$attempt" -ge "$DETACH_ATTEMPTS" ] || sleep "$DETACH_SLEEP_SECONDS"
    attempt=$((attempt + 1))
  done
  return 1
}

assert_image_detached() {
  local image_path="$1"
  local status=0
  resolve_test_attachment "$image_path" >/dev/null || status=$?
  [ "$status" -eq 1 ]
}

cleanup_image() {
  local image_path="$1"
  local attachment=""
  local status=0
  [ -f "$image_path" ] || return 0
  attachment=$(resolve_test_attachment "$image_path") || status=$?
  if [ "$status" -eq 0 ]; then
    force_unmount_attachment "$attachment" || true
    sync
    detach_test_image "$image_path" "$(printf '%s' "$attachment" | jq -r \
      '.backing_device // .whole_device // empty')"
    return
  fi
  [ "$status" -eq 1 ]
}

cleanup() {
  local status=$?
  local cleanup_status=0
  trap - EXIT
  cd "$TEST_ROOT" 2>/dev/null || cd "${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
  if cleanup_image "$INNER_IMAGE"; then
    cleanup_image "$OUTER_IMAGE" || cleanup_status=1
  else
    cleanup_status=1
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
hdiutil create -quiet -size 2g -fs APFS -type SPARSE \
  -volname IntuneBrewWorkspaceTest "$OUTER_IMAGE"
hdiutil attach "$OUTER_IMAGE" -mountpoint "$OUTER_MOUNT" -nobrowse -plist \
  > "$TEST_ROOT/outer-attach.plist"
outer=$(python "$HELPER" attach --plist "$TEST_ROOT/outer-attach.plist")
OUTER_DEVICE=$(printf '%s' "$outer" | jq -r .backing_device)
test "$OUTER_DEVICE" = "${OUTER_DEVICE%%s[0-9]*}"
test "$(printf '%s' "$outer" | jq '.synthesized_devices | length')" -ge 1

mkdir -p "$OUTER_MOUNT/package-root/usr/local/share/intunebrew"
dd if=/dev/urandom \
  of="$OUTER_MOUNT/package-root/usr/local/share/intunebrew/recent-data.bin" \
  bs=1m count=256 2>/dev/null
pkgbuild --root "$OUTER_MOUNT/package-root" \
  --identifier com.intunebrew.disk-lifecycle-test --version 1 \
  "$OUTER_MOUNT/recent-activity.pkg"
shasum -a 256 "$OUTER_MOUNT/recent-activity.pkg" \
  > "$OUTER_MOUNT/recent-activity.pkg.sha256"
dd if="$OUTER_MOUNT/recent-activity.pkg" of=/dev/null bs=1m 2>/dev/null

mkdir -p "$TEST_ROOT/payload"
printf 'nested payload\n' > "$TEST_ROOT/payload/proof.txt"
hdiutil create -quiet -fs HFS+ -srcfolder "$TEST_ROOT/payload" \
  -volname IntuneBrewNestedTest "$INNER_IMAGE"
hdiutil attach "$INNER_IMAGE" -readonly -nobrowse -plist \
  > "$OUTER_MOUNT/inner-attach.plist"
inner=$(python "$HELPER" attach --plist "$OUTER_MOUNT/inner-attach.plist")
INNER_DEVICE=$(printf '%s' "$inner" | jq -r .backing_device)
inner_mount=$(printf '%s' "$inner" | jq -r '.mounted_entities[0].mount_point')
test -f "$inner_mount/proof.txt"

cd "$TEST_ROOT"
rm -f "$OUTER_MOUNT/inner-attach.plist" "$TEST_ROOT/outer-attach.plist"
sync
force_unmount_attachment "$inner"
detach_test_image "$INNER_IMAGE" "$INNER_DEVICE"
assert_image_detached "$INNER_IMAGE"
INNER_DEVICE=""
printf 'source\n' >> "$TEST_ROOT/detach-order"

sync
force_unmount_attachment "$outer"
detach_test_image "$OUTER_IMAGE" "$OUTER_DEVICE"
assert_image_detached "$OUTER_IMAGE"
OUTER_DEVICE=""
printf 'workspace\n' >> "$TEST_ROOT/detach-order"
test "$(cat "$TEST_ROOT/detach-order")" = "$(printf 'source\nworkspace')"

echo "macOS disk image lifecycle passed"
