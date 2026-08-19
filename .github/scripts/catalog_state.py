#!/usr/bin/env python3
"""Generate and validate the immutable catalog publication marker."""

import argparse
import io
import json
import re
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


SCHEMA_VERSION = 1
STATE_KEYS = {
    "catalogCommit",
    "packageStorageBaseUrl",
    "publishedAt",
    "repository",
    "runId",
    "schemaVersion",
    "workflowName",
    "workflowPath",
}
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
PUBLISHED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def git_output(*args):
    return subprocess.check_output(
        ["git", *args],
        text=True,
        encoding="utf-8",
        stderr=subprocess.PIPE,
    )


def validate_https_url(value, field_name):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
    ):
        raise ValueError(
            f"{field_name} must be an HTTPS URL without a trailing slash, "
            "query string, or fragment"
        )


def validate_catalog_snapshot(catalog_commit):
    if not COMMIT_PATTERN.fullmatch(catalog_commit):
        raise ValueError("catalogCommit must be an exact lowercase 40-character SHA")

    try:
        git_output("cat-file", "-e", f"{catalog_commit}^{{commit}}")
        archive_bytes = subprocess.check_output(
            [
                "git",
                "archive",
                "--format=tar",
                catalog_commit,
                "supported_apps.json",
                "Apps",
            ],
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError(
            f"{catalog_commit} is not a commit containing the catalog snapshot"
        ) from error

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        files = {
            member.name: archive.extractfile(member).read().decode("utf-8")
            for member in archive.getmembers()
            if member.isfile()
        }

    try:
        supported_apps = json.loads(files["supported_apps.json"])
    except KeyError as error:
        raise ValueError("catalogCommit does not contain supported_apps.json") from error
    except json.JSONDecodeError as error:
        raise ValueError("supported_apps.json is not valid JSON") from error

    if not isinstance(supported_apps, dict):
        raise ValueError("supported_apps.json must contain a JSON object")

    for app_name, app_url in supported_apps.items():
        if not isinstance(app_name, str) or not app_name:
            raise ValueError("supported_apps.json contains an invalid app name")
        if not isinstance(app_url, str):
            raise ValueError(f"supported_apps.json URL for {app_name} is not a string")

        parsed_url = urlsplit(app_url)
        expected_path = PurePosixPath("Apps", f"{app_name}.json")
        actual_path = PurePosixPath(unquote(parsed_url.path).lstrip("/"))
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.query
            or parsed_url.fragment
            or not actual_path.as_posix().endswith(expected_path.as_posix())
        ):
            raise ValueError(
                f"supported_apps.json entry {app_name} does not reference "
                f"Apps/{app_name}.json through a stable HTTPS URL"
            )

        app_path = f"Apps/{app_name}.json"
        try:
            app_data = json.loads(files[app_path])
        except KeyError as error:
            raise ValueError(
                f"catalogCommit does not contain referenced file {app_path}"
            ) from error
        except json.JSONDecodeError as error:
            raise ValueError(f"{app_path} is not valid JSON") from error

        if not isinstance(app_data, dict):
            raise ValueError(f"{app_path} must contain a JSON object")

    return supported_apps


def validate_published_at(value):
    if not isinstance(value, str) or not PUBLISHED_AT_PATTERN.fullmatch(value):
        raise ValueError("publishedAt must be a whole-second UTC timestamp ending in Z")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise ValueError("publishedAt is not a valid UTC timestamp") from error


def validate_state(state, expected):
    if not isinstance(state, dict) or set(state) != STATE_KEYS:
        raise ValueError(
            "catalog state must contain exactly: " + ", ".join(sorted(STATE_KEYS))
        )
    if type(state["schemaVersion"]) is not int or state["schemaVersion"] != SCHEMA_VERSION:
        raise ValueError(f"schemaVersion must be {SCHEMA_VERSION}")
    if (
        not isinstance(state["repository"], str)
        or not REPOSITORY_PATTERN.fullmatch(state["repository"])
    ):
        raise ValueError("repository must use owner/name format")
    if type(state["runId"]) is not int or state["runId"] <= 0:
        raise ValueError("runId must be a positive integer")
    if not isinstance(state["workflowName"], str) or not state["workflowName"]:
        raise ValueError("workflowName must be a non-empty string")
    if (
        not isinstance(state["workflowPath"], str)
        or not state["workflowPath"].startswith(".github/workflows/")
        or not state["workflowPath"].endswith((".yml", ".yaml"))
    ):
        raise ValueError("workflowPath must identify a workflow under .github/workflows")

    validate_published_at(state["publishedAt"])
    validate_https_url(state["packageStorageBaseUrl"], "packageStorageBaseUrl")
    validate_catalog_snapshot(state["catalogCommit"])

    for field_name, expected_value in expected.items():
        if expected_value is not None and state[field_name] != expected_value:
            raise ValueError(
                f"{field_name} is {state[field_name]!r}, expected {expected_value!r}"
            )


def utc_timestamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def build_state(args):
    storage_url = args.package_storage_base_url.rstrip("/")
    published_at = args.published_at or utc_timestamp()
    state = {
        "catalogCommit": args.catalog_commit,
        "packageStorageBaseUrl": storage_url,
        "publishedAt": published_at,
        "repository": args.repository,
        "runId": int(args.run_id),
        "schemaVersion": SCHEMA_VERSION,
        "workflowName": args.workflow_name,
        "workflowPath": args.workflow_path,
    }
    validate_state(
        state,
        {
            "catalogCommit": args.catalog_commit,
            "repository": args.repository,
            "runId": int(args.run_id),
            "workflowName": args.workflow_name,
            "workflowPath": args.workflow_path,
        },
    )
    return state


def generate(args):
    state = build_state(args)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_file(args):
    state_path = Path(args.state_file)
    with state_path.open(encoding="utf-8") as state_file:
        state = json.load(state_file)
    validate_state(
        state,
        {
            "catalogCommit": args.catalog_commit,
            "repository": args.repository,
            "runId": int(args.run_id),
            "workflowName": args.workflow_name,
            "workflowPath": args.workflow_path,
        },
    )


def add_common_arguments(parser):
    parser.add_argument("--catalog-commit", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--run-id", required=True, type=int)


def parse_args():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate")
    add_common_arguments(generate_parser)
    generate_parser.add_argument("--package-storage-base-url", required=True)
    generate_parser.add_argument("--published-at")
    generate_parser.add_argument("--output", required=True)
    generate_parser.set_defaults(func=generate)

    validate_parser = subparsers.add_parser("validate")
    add_common_arguments(validate_parser)
    validate_parser.add_argument("--state-file", required=True)
    validate_parser.set_defaults(func=validate_file)

    return parser.parse_args()


def main():
    args = parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"catalog state validation failed: {error}") from error


if __name__ == "__main__":
    main()
