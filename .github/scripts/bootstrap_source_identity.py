#!/usr/bin/env python3
"""Safely bootstrap Homebrew source identity for existing private packages."""

import argparse
import json
import re
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[2]
APPS_DIR = ROOT / "Apps"
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def normalized_version(value):
    return value.split(",", 1)[0].split("_", 1)[0]


def private_package(app):
    return (
        app.get("type") in {"app", "pkg_in_dmg", "pkg_in_pkg"}
        and str(app.get("url", "")).lower().endswith(".pkg")
    )


def bootstrap_manifest(app, cask):
    source_sha = str(cask.get("sha256", ""))
    if (
        not private_package(app)
        or not SHA256.fullmatch(source_sha)
        or app.get("version") != normalized_version(cask["version"])
        or app.get("vendor_url") != cask.get("url")
    ):
        return False

    app["source_version"] = cask["version"]
    app["source_sha256"] = source_sha.lower()
    app["source_sha256_provenance"] = "homebrew"
    return True


def mark_unverified_source(app, cask):
    if (
        not private_package(app)
        or cask.get("sha256") != "no_check"
        or app.get("version") != normalized_version(cask["version"])
        or app.get("vendor_url") != cask.get("url")
    ):
        return False
    app["source_version"] = cask["version"]
    app["source_sha256"] = "no_check"
    app.pop("source_sha256_provenance", None)
    app["packaging_pending"] = True
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    changed = []
    pending = []
    session = requests.Session()
    for path in sorted(APPS_DIR.glob("*.json")):
        app = json.loads(path.read_text(encoding="utf-8"))
        token = app.get("homebrew_cask")
        if not token or app.get("deprecated"):
            continue
        response = session.get(
            f"https://formulae.brew.sh/api/cask/{token}.json",
            timeout=30,
        )
        if response.status_code != 200:
            continue
        cask = response.json()
        verified = bootstrap_manifest(app, cask)
        unverified = mark_unverified_source(app, cask)
        if verified or unverified:
            changed.append(path.name)
            if unverified:
                pending.append(path.name)
            if args.apply:
                path.write_text(
                    json.dumps(app, indent=2) + "\n",
                    encoding="utf-8",
                )
    print(
        json.dumps(
            {
                "updated": len(changed),
                "pendingNoCheck": len(pending),
                "manifests": changed,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
