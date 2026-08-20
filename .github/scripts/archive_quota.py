#!/usr/bin/env python3
"""Fail-closed archive expansion quota probes."""

import argparse
import gzip
import tarfile
import struct


class QuotaExceeded(ValueError):
    pass


def _add(total, members, size, max_bytes, max_members):
    total += size
    members += 1
    if total > max_bytes or members > max_members:
        raise QuotaExceeded("archive exceeds quota")
    return total, members


def zip_totals(path, max_bytes, max_members):
    total = members = 0
    with open(path, "rb") as source:
        while signature := source.read(4):
            if signature == b"PK\x03\x04":
                header = source.read(26)
                if len(header) != 26:
                    raise ValueError("truncated ZIP header")
                (
                    _version,
                    flags,
                    _compression,
                    _time,
                    _date,
                    _crc,
                    compressed,
                    uncompressed,
                    name_length,
                    extra_length,
                ) = struct.unpack("<HHHHHIIIHH", header)
                if flags & 0x08:
                    raise ValueError("ZIP data descriptors require central-directory scan")
                total, members = _add(
                    total,
                    members,
                    uncompressed,
                    max_bytes,
                    max_members,
                )
                source.seek(name_length + extra_length + compressed, 1)
            elif signature in {b"PK\x01\x02", b"PK\x05\x06", b"PK\x06\x06"}:
                break
            else:
                raise ValueError("invalid ZIP signature")
    return total, members


def archive_totals(path, archive_format, max_bytes, max_members):
    if archive_format == "zip":
        return zip_totals(path, max_bytes, max_members)
    if archive_format in {"tar.gz", "tar.xz", "tar.bz2"}:
        total = members = 0
        mode = {
            "tar.gz": "r|gz",
            "tar.xz": "r|xz",
            "tar.bz2": "r|bz2",
        }[archive_format]
        with tarfile.open(path, mode=mode) as archive:
            for item in archive:
                total, members = _add(
                    total,
                    members,
                    item.size,
                    max_bytes,
                    max_members,
                )
        return total, members
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
            size, members = archive_totals(
                args.file,
                args.format,
                args.max_bytes,
                args.max_members,
            )
            print(size, members)
        elif not gzip_within_limit(args.file, args.max_bytes):
            raise SystemExit(1)
    except (OSError, ValueError, tarfile.TarError) as error:
        raise SystemExit(f"archive quota probe failed: {error}") from error


if __name__ == "__main__":
    main()
