#!/usr/bin/env python3
"""Validate a build workflow's trusted catalog publication artifact."""

import argparse
import json
import re
from pathlib import Path


KEYS = {
    "catalogCommit",
    "repository",
    "runId",
    "workflowName",
    "workflowPath",
}
SHA = re.compile(r"^[0-9a-f]{40}$")


def load_and_validate(path, expected):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != KEYS:
        raise ValueError("publication artifact has unexpected schema")
    if not SHA.fullmatch(str(data["catalogCommit"])):
        raise ValueError("catalogCommit is not an exact lowercase SHA")
    if type(data["runId"]) is not int or data["runId"] <= 0:
        raise ValueError("runId must be a positive integer")
    for key, value in expected.items():
        if data[key] != value:
            raise ValueError(f"{key} does not match the completed build run")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--workflow-name", required=True)
    parser.add_argument("--workflow-path", required=True)
    args = parser.parse_args()
    try:
        data = load_and_validate(
            args.file,
            {
                "repository": args.repository,
                "runId": args.run_id,
                "workflowName": args.workflow_name,
                "workflowPath": args.workflow_path,
            },
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid publication provenance: {error}") from error
    print(data["catalogCommit"])


if __name__ == "__main__":
    main()
