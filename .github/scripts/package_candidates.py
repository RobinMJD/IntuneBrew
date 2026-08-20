#!/usr/bin/env python3
"""Select a deterministic, resumable batch of packages to build."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
PACKAGE_TYPES = {"app", "pkg_in_dmg", "pkg_in_pkg"}


def prior_manifest(path):
    result = subprocess.run(
        ["git", "show", f"HEAD:{path.as_posix()}"],
        text=True,
        capture_output=True,
        check=False,
    )
    return json.loads(result.stdout) if result.returncode == 0 else {}


def is_private_package(app, storage_base):
    url = str(app.get("url", ""))
    return (
        url.startswith(storage_base.rstrip("/") + "/")
        and url.lower().endswith(".pkg")
        and SHA256.fullmatch(str(app.get("sha", "")))
    )


def needs_package(current, prior, storage_base):
    if current.get("type") not in PACKAGE_TYPES or current.get("deprecated"):
        return False
    if not is_private_package(prior, storage_base):
        return True
    source_sha = current.get("source_sha256")
    if source_sha == "no_check":
        return True
    return not (
        current.get("source_version")
        and current.get("source_version") == prior.get("source_version")
        and SHA256.fullmatch(str(source_sha or ""))
        and source_sha == prior.get("source_sha256")
        and current.get("vendor_url") == prior.get("vendor_url")
    )


def sort_key(item):
    name, current, prior = item
    # Oldest no_check verification rotates first; migrations have no timestamp.
    return (prior.get("source_checked_at", ""), name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-packages", type=int, required=True)
    parser.add_argument("--storage-base-url", required=True)
    parser.add_argument("--scope", choices=("all", "partial"), required=True)
    parser.add_argument("--tokens", default="")
    parser.add_argument("--apps-dir", default="Apps")
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args()
    if not 1 <= args.max_packages <= 100:
        raise SystemExit("max-packages must be between 1 and 100")

    tokens = set(args.tokens.split())
    candidates = []
    for path in sorted(Path(args.apps_dir).glob("*.json")):
        current = json.loads(path.read_text(encoding="utf-8"))
        if args.scope == "partial" and current.get("homebrew_cask") not in tokens:
            continue
        prior = prior_manifest(path)
        if needs_package(current, prior, args.storage_base_url):
            candidates.append((path.stem, current, prior))

    candidates.sort(key=sort_key)
    selected = candidates[: args.max_packages]
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "selected-packages.txt").write_text(
        "\n".join(name for name, _, _ in selected) + ("\n" if selected else ""),
        encoding="utf-8",
    )
    (output / "package-candidates.json").write_text(
        json.dumps(
            {
                "selected": [name for name, _, _ in selected],
                "candidateCount": len(candidates),
                "remainingCount": max(0, len(candidates) - len(selected)),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        entries = []
        for name, current, _ in selected:
            entries.append(f"{name}:{current['url']}")
        with open(env_file, "a", encoding="utf-8") as handle:
            handle.write(f"APPS_TO_BUILD={' '.join(entries)}\n")


if __name__ == "__main__":
    main()
