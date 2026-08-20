#!/usr/bin/env python3
"""Select a primary bundle/package identifier from an expanded macOS package."""

import argparse
import plistlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path


EXCLUDED_PARTS = {
    "Frameworks",
    "LoginItems",
    "XPCServices",
    "Sparkle",
    "Helpers",
    "PlugIns",
    "Plugins",
}
HELPER_TOKENS = {
    "helper",
    "plugin",
    "plugins",
    "updater",
    "update",
    "updates",
    "shortcut",
    "shortcuts",
    "framework",
}
CONCRETE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def concrete(value):
    return bool(value and CONCRETE_ID.fullmatch(value))


def excluded(path):
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return True
    bundle = next(
        (part for part in reversed(path.parts) if part.endswith((".app", ".pkg"))),
        path.name,
    )
    tokens = set(re.findall(r"[a-z0-9]+", bundle.lower()))
    return bool(tokens & HELPER_TOKENS)


def app_identifiers(root):
    result = []
    for plist in root.rglob("*.app/Contents/Info.plist"):
        if excluded(plist.relative_to(root)):
            continue
        try:
            with plist.open("rb") as handle:
                identifier = plistlib.load(handle).get("CFBundleIdentifier")
        except (OSError, plistlib.InvalidFileException):
            continue
        if concrete(identifier):
            result.append((identifier, plist))
    return result


def package_identifiers(root):
    result = []
    for info in root.rglob("PackageInfo"):
        if excluded(info.relative_to(root)):
            continue
        try:
            identifier = ET.parse(info).getroot().get("identifier")
        except (OSError, ET.ParseError):
            continue
        if concrete(identifier):
            result.append((identifier, info))
    return result


def choose(candidates, expected):
    matches = sorted({identifier for identifier, _ in candidates})
    if concrete(expected) and expected in matches:
        return expected
    if len(matches) == 1:
        return matches[0]
    return None


def select_identity(root, expected):
    apps = app_identifiers(root)
    if apps:
        return choose(apps, expected)
    return choose(package_identifiers(root), expected)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--expected", default="")
    args = parser.parse_args()
    root = Path(args.root).resolve(strict=True)
    selected = select_identity(root, args.expected)
    if selected is None:
        raise SystemExit("no unambiguous primary package identity")
    print(selected)


if __name__ == "__main__":
    main()
