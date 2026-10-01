"""Byte-offset-resumable streaming reader for a remote JSONL file over HTTP.

Used instead of `datasets`' `IterableDataset.skip()`, which was found (by
reading its source -- see CLAUDE.md #16) to re-iterate the underlying stream
from byte 0 on every call and simply discard the first n parsed records. For
a file the size of the Home_and_Kitchen reviews JSONL (~31GB), that makes
every resume re-download everything already scanned. This module instead
issues an HTTP Range request at a specific byte offset (confirmed working:
the HF CDN serves this file with `Accept-Ranges: bytes` and returns a real
`206 Partial Content`), so a resume only has to read what's left.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import requests


def get_content_length(url: str, timeout: int = 30) -> int | None:
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        resp.raise_for_status()
        length = resp.headers.get("Content-Length")
        return int(length) if length is not None else None
    except requests.exceptions.RequestException:
        return None


def _stream_jsonl_from_byte(url: str, start_byte: int, chunk_size: int, timeout: int) -> Iterator[tuple[int, dict]]:
    """Yields (offset_of_next_line_start, parsed_json_obj).

    `start_byte` MUST be the byte offset of the start of a line -- either 0,
    or a value this same function previously yielded (and the caller fully
    processed). A `Range` request can land mid-line, so never pass an
    arbitrary offset.
    """
    headers = {"Range": f"bytes={start_byte}-"} if start_byte else {}
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        offset = start_byte
        buffer = b""
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                offset += len(line) + 1
                stripped = line.strip()
                if stripped:
                    yield offset, json.loads(stripped)
        stripped = buffer.strip()
        if stripped:
            offset += len(buffer)
            yield offset, json.loads(stripped)


def iter_jsonl_resumable(
    url: str,
    start_byte: int = 0,
    chunk_size: int = 1 << 20,
    timeout: int = 60,
    max_retries: int = 8,
) -> Iterator[tuple[int, dict]]:
    """Like `_stream_jsonl_from_byte`, but transparently reconnects (via a
    fresh Range request from the last safely-processed line) on a transient
    network error, instead of crashing a long-running full-file scan.
    """
    offset = start_byte
    retries = 0
    while True:
        try:
            for next_offset, obj in _stream_jsonl_from_byte(url, offset, chunk_size, timeout):
                yield next_offset, obj
                offset = next_offset
                retries = 0  # reset backoff after any successful progress
            return
        except requests.exceptions.RequestException as exc:
            retries += 1
            if retries > max_retries:
                raise
            wait = min(60, 2**retries)
            print(f"\nNetwork error ({exc}); reconnecting at byte {offset:,} in {wait}s "
                  f"(retry {retries}/{max_retries})...")
            time.sleep(wait)
