#!/usr/bin/env python3
"""Fail-closed archive expansion quota probes."""

import argparse
import gzip
import tarfile
import zipfile


def archive_totals(path, archive_format):
    if archive_format == "zip":
        with zipfile.ZipFile(path) as archive:
            items = archive.infolist()
            return sum(item.file_size for item in items), len(items)
    if archive_format in {"tar.gz", "tar.xz", "tar.bz2"}:
        with tarfile.open(path) as archive:
            items = archive.getmembers()
            return sum(item.size for item in items), len(items)
    raise ValueError(f"unsupported archive format: {archive_format}")


def gzip_within_limit(path, max_bytes):
    total = 0
    with gzip.open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                return False
    return True


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    archive = subparsers.add_parser("archive")
    archive.add_argument("--file", required=True)
    archive.add_argument("--format", required=True)
    archive.add_argument("--max-bytes", type=int, required=True)
    archive.add_argument("--max-members", type=int, required=True)
    gzip_parser = subparsers.add_parser("gzip")
    gzip_parser.add_argument("--file", required=True)
    gzip_parser.add_argument("--max-bytes", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.command == "archive":
            size, members = archive_totals(args.file, args.format)
            if size > args.max_bytes or members > args.max_members:
                raise SystemExit(1)
            print(size, members)
        elif not gzip_within_limit(args.file, args.max_bytes):
            raise SystemExit(1)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise SystemExit(f"archive quota probe failed: {error}") from error


if __name__ == "__main__":
    main()
