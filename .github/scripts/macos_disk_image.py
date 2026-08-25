#!/usr/bin/env python3
"""Parse hdiutil plist output without confusing partitions with image devices."""

import argparse
import json
import os
import plistlib
import re
import sys
from pathlib import PurePosixPath


DEVICE_RE = re.compile(r"^/dev/disk[0-9]+(?:s[0-9]+)*$")
WHOLE_DEVICE_RE = re.compile(r"^/dev/disk[0-9]+$")


class DiskImageParseError(ValueError):
    pass


class DiskImageNotFoundError(DiskImageParseError):
    pass


def _canonical_path(path):
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _attachment(system_entities, require_mounts=True):
    devices = []
    whole_entities = []
    mounted_entities = []
    for entity in system_entities:
        if not isinstance(entity, dict):
            continue
        device = entity.get("dev-entry")
        if not isinstance(device, str) or not DEVICE_RE.fullmatch(device):
            continue
        if device not in devices:
            devices.append(device)
        if WHOLE_DEVICE_RE.fullmatch(device):
            whole_entities.append(
                (device, entity.get("content-hint"))
            )
        mount_point = entity.get("mount-point")
        if mount_point is None:
            continue
        if (
            not isinstance(mount_point, str)
            or "\n" in mount_point
            or "\r" in mount_point
            or not PurePosixPath(mount_point).is_absolute()
        ):
            raise DiskImageParseError(f"invalid mount point for {device!r}")
        mounted_entities.append(
            {"device": device, "mount_point": mount_point}
        )

    partition_schemes = [
        device
        for device, content_hint in whole_entities
        if isinstance(content_hint, str)
        and content_hint.endswith("_partition_scheme")
    ]
    if len(partition_schemes) == 1:
        whole_device = partition_schemes[0]
    elif len(whole_entities) == 1:
        whole_device = whole_entities[0][0]
    elif not whole_entities:
        roots = {
            re.sub(r"s[0-9]+(?:s[0-9]+)*$", "", device)
            for device in devices
        }
        if len(roots) != 1:
            raise DiskImageParseError(
                f"expected one whole image device, found {sorted(roots)!r}"
            )
        whole_device = roots.pop()
    else:
        raise DiskImageParseError(
            f"could not distinguish image device from synthesized devices: "
            f"{[device for device, _ in whole_entities]!r}"
        )
    if require_mounts and not mounted_entities:
        raise DiskImageParseError("disk image has no mounted entities")

    return {
        "whole_device": whole_device,
        "mounted_entities": mounted_entities,
    }


def parse_attach_plist(data):
    entities = data.get("system-entities") if isinstance(data, dict) else None
    if not isinstance(entities, list):
        raise DiskImageParseError("attach plist has no system-entities array")
    return _attachment(entities)


def find_image_attachment(data, image_path):
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, list):
        raise DiskImageParseError("info plist has no images array")
    target = _canonical_path(image_path)
    matches = []
    for image in images:
        if not isinstance(image, dict):
            continue
        candidate = image.get("image-path")
        entities = image.get("system-entities")
        if (
            isinstance(candidate, str)
            and isinstance(entities, list)
            and _canonical_path(candidate) == target
        ):
            matches.append(_attachment(entities, require_mounts=False))
    if not matches:
        raise DiskImageNotFoundError(
            f"no attachment found for {_canonical_path(image_path)!r}"
        )
    if len(matches) != 1:
        raise DiskImageParseError(
            f"expected one attachment for {target!r}, found {len(matches)}"
        )
    return matches[0]


def _load_plist(path):
    with open(path, "rb") as handle:
        return plistlib.load(handle)


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    attach = subparsers.add_parser("attach")
    attach.add_argument("--plist", required=True)
    info = subparsers.add_parser("info")
    info.add_argument("--plist", required=True)
    info.add_argument("--image", required=True)
    args = parser.parse_args()

    try:
        data = _load_plist(args.plist)
        if args.command == "attach":
            result = parse_attach_plist(data)
        else:
            result = find_image_attachment(data, args.image)
    except DiskImageNotFoundError as error:
        print(f"Could not parse hdiutil {args.command} plist: {error}", file=sys.stderr)
        return 1
    except (DiskImageParseError, OSError, plistlib.InvalidFileException) as error:
        print(f"Could not parse hdiutil {args.command} plist: {error}", file=sys.stderr)
        return 2

    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
