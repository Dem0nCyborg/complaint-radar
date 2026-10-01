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


def _stream_lines_from_byte(url: str, start_byte: int, chunk_size: int, timeout: int) -> Iterator[tuple[int, bytes]]:
    """Yields (offset_of_next_line_start, raw_line_bytes) for each non-empty line.

    `start_byte` MUST be the byte offset of the start of a line -- either 0,
    or a value this same function previously yielded (and the caller fully
    processed). A `Range` request can land mid-line, so never pass an
    arbitrary offset.

    Deliberately does NOT parse JSON itself: callers that need to tolerate
    malformed/unexpected rows (e.g. ingest_metadata.py, after a `datasets`
    Arrow-cast crash on a schema-inconsistent row -- see CLAUDE.md #17) do
    their own per-line `json.loads` with their own error handling, instead
    of one bad line taking down the whole scan.
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
                    yield offset, stripped
        stripped = buffer.strip()
        if stripped:
            offset += len(buffer)
            yield offset, stripped


def iter_lines_resumable(
    url: str,
    start_byte: int = 0,
    chunk_size: int = 1 << 20,
    timeout: int = 60,
    max_retries: int = 8,
) -> Iterator[tuple[int, bytes]]:
    """Like `_stream_lines_from_byte`, but transparently reconnects (via a
    fresh Range request from the last safely-processed line) on a transient
    network error, instead of crashing a long-running full-file scan.
    """
    offset = start_byte
    retries = 0
    while True:
        try:
            for next_offset, line in _stream_lines_from_byte(url, offset, chunk_size, timeout):
                yield next_offset, line
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


def iter_jsonl_resumable(url: str, start_byte: int = 0, **kwargs) -> Iterator[tuple[int, dict]]:
    """Convenience wrapper for callers that trust every line to be valid
    JSON (fetch_reviews.py -- the reviews file has shown a consistent
    schema). A bad line here raises, same as before.
    """
    for offset, line in iter_lines_resumable(url, start_byte=start_byte, **kwargs):
        yield offset, json.loads(line)
