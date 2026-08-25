#!/usr/bin/env python3
"""Run lsof with bounded wall-clock time and diagnostic output."""

import argparse
import subprocess
import sys


def run_lsof(path, timeout, max_lines, stdout=sys.stdout, stderr=sys.stderr):
    command = ["lsof", "-nP", "+f", "--", path]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        print(f"Could not start lsof: {error}", file=stderr)
        return 2

    timed_out = False
    try:
        output, _unused = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        output, _unused = process.communicate()

    lines = output.splitlines()
    for line in lines[:max_lines]:
        print(line, file=stdout)
    if len(lines) > max_lines:
        print(f"[lsof output truncated after {max_lines} lines]", file=stderr)
    if timed_out:
        print(f"[lsof timed out after {timeout} seconds]", file=stderr)
        return 124
    return process.returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--max-lines", type=int, default=100)
    args = parser.parse_args()
    if args.timeout < 1 or args.max_lines < 1:
        parser.error("timeout and max-lines must be positive")
    return run_lsof(args.path, args.timeout, args.max_lines)


if __name__ == "__main__":
    raise SystemExit(main())
