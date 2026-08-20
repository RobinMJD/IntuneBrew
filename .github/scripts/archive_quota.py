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


def _read_exact(source, size, label):
    value = source.read(size)
    if len(value) != size:
        raise ValueError(f"truncated {label}")
    return value


def _find_eocd(source):
    source.seek(0, 2)
    file_size = source.tell()
    tail_size = min(file_size, 65557)
    source.seek(file_size - tail_size)
    tail = source.read(tail_size)
    index = tail.rfind(b"PK\x05\x06")
    if index < 0 or len(tail) - index < 22:
        raise ValueError("ZIP EOCD not found")
    eocd_offset = file_size - tail_size + index
    (
        signature,
        disk,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        comment_length,
    ) = struct.unpack("<4s4H2LH", tail[index : index + 22])
    if signature != b"PK\x05\x06" or index + 22 + comment_length != len(tail):
        raise ValueError("malformed ZIP EOCD")
    if disk != 0 or central_disk != 0 or disk_entries != total_entries:
        raise ValueError("multi-disk ZIP is unsupported")
    if (
        total_entries != 0xFFFF
        and central_size != 0xFFFFFFFF
        and central_offset != 0xFFFFFFFF
    ):
        return total_entries, central_size, central_offset, eocd_offset

    locator_offset = eocd_offset - 20
    if locator_offset < 0:
        raise ValueError("ZIP64 locator missing")
    source.seek(locator_offset)
    signature, zip64_disk, zip64_offset, disk_count = struct.unpack(
        "<4sLQL",
        _read_exact(source, 20, "ZIP64 locator"),
    )
    if signature != b"PK\x06\x07" or zip64_disk != 0 or disk_count != 1:
        raise ValueError("malformed ZIP64 locator")
    source.seek(zip64_offset)
    header = _read_exact(source, 56, "ZIP64 EOCD")
    (
        signature,
        record_size,
        _made_by,
        _needed,
        disk,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
    ) = struct.unpack("<4sQ2H2L4Q", header)
    if (
        signature != b"PK\x06\x06"
        or record_size < 44
        or disk != 0
        or central_disk != 0
        or disk_entries != total_entries
    ):
        raise ValueError("malformed ZIP64 EOCD")
    return total_entries, central_size, central_offset, zip64_offset


def _zip64_sizes(extra, uncompressed, compressed, local_offset, disk):
    values = []
    offset = 0
    while offset < len(extra):
        if len(extra) - offset < 4:
            raise ValueError("truncated ZIP extra field")
        field_id, field_size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        field = extra[offset : offset + field_size]
        if len(field) != field_size:
            raise ValueError("truncated ZIP extra data")
        offset += field_size
        if field_id == 0x0001:
            values = list(field)
            cursor = 0

            def take(size):
                nonlocal cursor
                if len(field) - cursor < size:
                    raise ValueError("truncated ZIP64 extra field")
                fmt = "<Q" if size == 8 else "<L"
                value = struct.unpack_from(fmt, field, cursor)[0]
                cursor += size
                return value

            if uncompressed == 0xFFFFFFFF:
                uncompressed = take(8)
            if compressed == 0xFFFFFFFF:
                compressed = take(8)
            if local_offset == 0xFFFFFFFF:
                local_offset = take(8)
            if disk == 0xFFFF:
                disk = take(4)
            break
    if any(
        value == sentinel
        for value, sentinel in (
            (uncompressed, 0xFFFFFFFF),
            (compressed, 0xFFFFFFFF),
            (local_offset, 0xFFFFFFFF),
            (disk, 0xFFFF),
        )
    ):
        raise ValueError("ZIP64 sentinel lacks matching extra data")
    return uncompressed, compressed, local_offset, disk


def zip_totals(path, max_bytes, max_members):
    total = members = 0
    with open(path, "rb") as source:
        entries, central_size, central_offset, central_end_limit = _find_eocd(source)
        if (
            central_offset < 0
            or central_size < 0
            or central_offset + central_size > central_end_limit
        ):
            raise ValueError("ZIP central directory is out of bounds")
        source.seek(central_offset)
        central_end = central_offset + central_size
        for _ in range(entries):
            fixed = _read_exact(source, 46, "ZIP central header")
            values = struct.unpack("<4s6H3I5H2I", fixed)
            if values[0] != b"PK\x01\x02":
                raise ValueError("invalid ZIP central signature")
            (
                _signature,
                _made_by,
                _needed,
                _flags,
                _compression,
                _time,
                _date,
                _crc,
                compressed,
                uncompressed,
                name_length,
                extra_length,
                comment_length,
                disk,
                _internal,
                _external,
                local_offset,
            ) = values
            _read_exact(source, name_length, "ZIP member name")
            extra = _read_exact(source, extra_length, "ZIP member extra")
            _read_exact(source, comment_length, "ZIP member comment")
            uncompressed, _compressed, _local_offset, disk = _zip64_sizes(
                extra,
                uncompressed,
                compressed,
                local_offset,
                disk,
            )
            if disk != 0:
                raise ValueError("multi-disk ZIP member is unsupported")
            total, members = _add(
                total,
                members,
                uncompressed,
                max_bytes,
                max_members,
            )
        if source.tell() != central_end or members != entries:
            raise ValueError("ZIP central directory length mismatch")
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
