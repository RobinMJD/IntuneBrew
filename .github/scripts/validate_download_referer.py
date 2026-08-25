#!/usr/bin/env python3

import argparse
from urllib.parse import urlparse


def url_origin(url):
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        return None
    return ("https", parsed.hostname.lower(), 443)


def is_approved_referer(referer, source, homepage):
    if any(ord(character) < 32 or ord(character) == 127 for character in referer):
        return False
    try:
        parsed = urlparse(referer)
    except ValueError:
        return False
    if parsed.query or parsed.fragment:
        return False
    allowed_origins = {
        origin
        for origin in (url_origin(source), url_origin(homepage))
        if origin is not None
    }
    return url_origin(referer) in allowed_origins


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--referer", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--homepage", required=True)
    args = parser.parse_args()
    raise SystemExit(
        0
        if is_approved_referer(args.referer, args.source, args.homepage)
        else 1
    )


if __name__ == "__main__":
    main()
