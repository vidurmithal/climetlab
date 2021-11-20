# (C) Copyright 2020 ECMWF.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.
#

import cgi
import datetime
import json
import logging
import os
import re
import time

import pytz
import requests
from dateutil.parser import parse as parse_date

from climetlab.core.settings import SETTINGS
from climetlab.core.statistics import record_statistics

from .downloader import Downloader
from .parts_heuristics import parts_heuristics

LOG = logging.getLogger(__name__)


# S3 does not support multiple ranges
class S3Streamer:
    def __init__(self, url, request, parts, headers, **kwargs):
        self.url = url
        self.parts = parts
        self.request = request
        self.headers = headers
        self.kwargs = kwargs

    def __call__(self, chunk_size):
        # See https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html

        headers = dict(**self.headers)
        # TODO: add assertions

        for i, part in enumerate(self.parts):
            if i == 0:
                request = self.request
            else:
                offset, length = part
                headers["range"] = f"bytes={offset}-{offset+length-1}"
                request = requests.get(
                    self.url,
                    stream=True,
                    headers=headers,
                    **self.kwargs,
                )
                try:
                    request.raise_for_status()
                except Exception:
                    LOG.error("URL %s: %s", self.url, request.text)
                    raise

            header = request.headers
            bytes = header["content-range"]
            LOG.debug("HEADERS %s", header)
            m = re.match(r"^bytes (\d+)d?-(\d+)d?/(\d+)d?$", bytes)
            assert m, header
            start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))

            assert end >= start
            assert start < total
            assert end < total

            assert start == part[0], (bytes, part)
            # (end + 1 == total) means that we overshoot the end of the file,
            # this happens when we round transfer blocks
            assert (end == part[0] + part[1] - 1) or (end + 1 == total), (bytes, part)

            for chunk in request.iter_content(chunk_size):
                yield chunk


class MultiPartStreamer:
    def __init__(self, url, request, parts, boundary, **kwargs):
        self.request = request
        self.size = int(request.headers["content-length"])
        self.encoding = "utf-8"
        self.parts = parts
        self.boundary = boundary

    def __call__(self, chunk_size):
        from email.parser import HeaderParser

        from requests.structures import CaseInsensitiveDict

        header_parser = HeaderParser()
        marker = f"--{self.boundary}\r\n".encode(self.encoding)
        end_header = b"\r\n\r\n"
        end_data = b"\r\n"

        end_of_input = f"--{self.boundary}--\r\n".encode(self.encoding)

        if chunk_size < len(end_data):
            chunk_size = len(end_data)

        iter_content = self.request.iter_content(chunk_size)
        chunk = next(iter_content)

        # Some servers start with \r\n
        if chunk[:2] == end_data:
            chunk = chunk[2:]

        LOG.debug("MARKER %s", marker)
        part = 0
        while True:
            while len(chunk) < max(len(marker), len(end_of_input)):
                more = next(iter_content)
                assert more is not None
                chunk += more

            if chunk.find(end_of_input) == 0:
                assert part == len(self.parts)
                break

            pos = chunk.find(marker)
            assert pos == 0, (pos, chunk)

            chunk = chunk[pos + len(marker) :]
            while True:
                pos = chunk.find(end_header)
                if pos != -1:
                    break
                more = next(iter_content)
                assert more is not None
                chunk += more
                assert len(chunk) < 1024 * 16

            pos += len(end_header)
            header = chunk[:pos].decode(self.encoding)
            header = CaseInsensitiveDict(header_parser.parsestr(header))
            chunk = chunk[pos:]
            # kind = header["content-type"]
            bytes = header["content-range"]
            LOG.debug("HEADERS %s", header)
            m = re.match(r"^bytes (\d+)d?-(\d+)d?/(\d+)d?$", bytes)
            assert m, header
            start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))

            assert end >= start
            assert start < total
            assert end < total

            size = end - start + 1

            assert start == self.parts[part][0]
            # (end + 1 == total) means that we overshoot the end of the file,
            # this happens when we round transfer blocks
            assert (end == self.parts[part][0] + self.parts[part][1] - 1) or (
                end + 1 == total
            ), (bytes, self.parts[part])

            while size > 0:
                if len(chunk) >= size:
                    yield chunk[:size]
                    chunk = chunk[size:]
                    size = 0
                else:
                    yield chunk
                    size -= len(chunk)
                    chunk = next(iter_content)

            assert chunk.find(end_data) == 0
            chunk = chunk[len(end_data) :]
            part += 1


class DecodeMultipart:
    def __init__(self, url, request, parts, **kwargs):
        self.request = request
        assert request.status_code == 206, request.status_code

        content_type = request.headers["content-type"]

        if content_type.startswith("multipart/byteranges; boundary="):
            _, boundary = content_type.split("=")
            print("******  MULTI-PART supported by server", url)
            self.streamer = MultiPartStreamer(url, request, parts, boundary, **kwargs)
        else:
            print("******  MULTI-PART *NOT* supported by server", url)
            self.streamer = S3Streamer(url, request, parts, **kwargs)

    def __call__(self, chunk_size):
        return self.streamer(chunk_size)


class PartFilter:
    def __init__(self, parts, positions=None):
        self.parts = parts

        if positions is None:
            positions = [x[0] for x in parts]
        self.positions = positions

        assert len(self.parts) == len(self.positions)

    def __call__(self, streamer):
        def execute(chunk_size):
            stream = streamer(chunk_size)
            chunk = next(stream)
            pos = 0
            for (_, length), offset in zip(self.parts, self.positions):
                offset -= pos

                while offset > len(chunk):
                    pos += len(chunk)
                    offset -= len(chunk)
                    chunk = next(stream)
                    assert chunk

                chunk = chunk[offset:]
                pos += offset
                size = length
                while size > 0:
                    if len(chunk) >= size:
                        yield chunk[:size]
                        chunk = chunk[size:]
                        pos += size
                        size = 0
                    else:
                        yield chunk
                        size -= len(chunk)
                        pos += len(chunk)
                        chunk = next(stream)

            # Drain stream, so we don't created error messages in the server's logs
            while True:
                try:
                    next(stream)
                except StopIteration:
                    break

        return execute


def compress_parts(parts):
    last = -1
    result = []
    # Compress and check
    for offset, length in parts:
        assert offset >= 0 and length > 0
        assert offset >= last, (
            f"Offsets and lengths must be in order, and not overlapping:"
            f" offset={offset}, end of previous part={last}"
        )
        if offset == last:
            # Compress
            offset, prev_length = result.pop()
            length += prev_length

        result.append((offset, length))
        last = offset + length
    return result


def compute_byte_ranges(parts, method, url):

    if callable(method):
        blocks = method(parts)
    else:
        blocks = parts_heuristics(method)(parts)

    blocks = compress_parts(blocks)

    assert len(blocks) > 0
    assert len(blocks) <= len(parts)

    record_statistics(
        "byte-ranges",
        method=str(method),
        url=url,
        parts=parts,
        blocks=blocks,
    )

    i = 0
    positions = []
    block_offset, block_length = blocks[i]
    for offset, length in parts:
        while offset > block_offset + block_length:
            i += 1
            block_offset, block_length = blocks[i]
        start = i
        while offset + length > block_offset + block_length:
            i += 1
            block_offset, block_length = blocks[i]
        end = i
        # Sanity check: assert that each parts is contain in a rounded part
        assert start == end
        positions.append(offset - blocks[i][0] + sum(blocks[j][1] for j in range(i)))

    return blocks, positions


def NoFilter(x):
    return x


class HTTPDownloader(Downloader):
    supports_parts = True

    _headers = None
    _url = None

    def headers(self, url):
        if self._headers is None or url != self._url:
            self._url = url
            self._headers = {}
            if self.owner.fake_headers is not None:
                self._headers = dict(**self.owner.fake_headers)
            else:
                try:
                    r = requests.head(
                        url,
                        headers=self.owner.http_headers,
                        verify=self.owner.verify,
                        allow_redirects=True,
                    )
                    r.raise_for_status()
                    for k, v in r.headers.items():
                        self._headers[k.lower()] = v
                    LOG.debug(
                        "HTTP headers %s",
                        json.dumps(self._headers, sort_keys=True, indent=4),
                    )
                except Exception:
                    self._url = None
                    self._headers = {}
                    LOG.exception("HEAD %s", url)
        return self._headers

    def extension(self, url):

        ext = super().extension(url)

        if ext == ".unknown":
            # Only check for "content-disposition" if
            # the URL does not end with an extension
            # so we avoid fetching the headers unesseraly

            headers = self.headers(url)

            if "content-disposition" in headers:
                value, params = cgi.parse_header(headers["content-disposition"])
                assert value == "attachment", value
                if "filename" in params:
                    ext = super().extension(params["filename"])

        return ext

    def title(self, url):
        headers = self.headers(url)
        if "content-disposition" in headers:
            value, params = cgi.parse_header(headers["content-disposition"])
            assert value == "attachment", value
            if "filename" in params:
                return params["filename"]
        return super().title(url)

    def prepare(self, url, download):

        size = None
        mode = "wb"
        skip = 0

        parts = self.owner.parts

        headers = self.headers(url)
        if "content-length" in headers:
            try:
                size = int(headers["content-length"])
            except Exception:
                LOG.exception("content-length %s", url)

        # content-length is the size of the encoded body
        # so we cannot rely on it to check the file size
        encoded = headers.get("content-encoding") is not None

        http_headers = dict(**self.owner.http_headers)

        if not parts and not encoded and os.path.exists(download):

            bytes = os.path.getsize(download)

            if size is not None:
                assert bytes < size, (bytes, size, url, download)

            if bytes > 0:
                if headers.get("accept-ranges") == "bytes":
                    mode = "ab"
                    http_headers["range"] = f"bytes={bytes}-"
                    LOG.info(
                        "%s: resuming download from byte %s",
                        download,
                        bytes,
                    )
                    skip = bytes
                else:
                    LOG.warning(
                        "%s: %s bytes already download, but server does not support restarts",
                        download,
                        bytes,
                    )

        range_method = self.owner.range_method

        filter = NoFilter

        if parts:
            # We can trust the size
            encoded = None
            size = sum(p[1] for p in parts)
            if headers.get("accept-ranges") != "bytes":
                LOG.warning(
                    "Server for %s does not support byte ranges, downloading whole file",
                    url,
                )
                filter = PartFilter(parts)
                parts = None
            else:
                ranges = []
                if range_method:

                    rounded, positions = compute_byte_ranges(parts, range_method, url)
                    filter = PartFilter(parts, positions)
                    parts = rounded

                for offset, length in parts:
                    ranges.append(f"{offset}-{offset+length-1}")

                http_headers["range"] = f"bytes={','.join(ranges)}"

                # print("RANGES", http_headers["range"])

        r = requests.get(
            url,
            stream=True,
            verify=self.owner.verify,
            timeout=SETTINGS.get("url-download-timeout"),
            headers=http_headers,
        )
        try:
            r.raise_for_status()
        except Exception:
            LOG.error("URL %s: %s", url, r.text)
            raise

        if parts and len(parts) > 1:
            self.stream = filter(
                DecodeMultipart(
                    url,
                    r,
                    parts,
                    verify=self.owner.verify,
                    timeout=SETTINGS.get("url-download-timeout"),
                    headers=http_headers,
                )
            )
        else:
            self.stream = filter(r.iter_content)

        LOG.debug(
            "url prepare size=%s mode=%s skip=%s encoded=%s",
            size,
            mode,
            skip,
            encoded,
        )
        return size, mode, skip, encoded

    def transfer(self, f, pbar, watcher):
        total = 0
        start = time.time()
        for chunk in self.stream(chunk_size=self.owner.chunk_size):
            watcher()
            if chunk:
                f.write(chunk)
                total += len(chunk)
                pbar.update(len(chunk))
        record_statistics(
            "transfer", url=self.owner.url, total=total, elapsed=time.time() - start
        )
        return total

    def cache_data(self, url):
        return self.headers(url)

    def out_of_date(self, url, path, cache_data):

        if SETTINGS.get("check-out-of-date-urls"):
            return False

        if cache_data is not None:

            # TODO: check 'cache-control' to see if we should check the etag
            if "cache-control" in cache_data:
                pass

            if "expires" in cache_data:
                if cache_data["expires"] != "0":  # HTTP1.0 legacy
                    try:
                        expires = parse_date(cache_data["expires"])
                        now = pytz.UTC.localize(datetime.datetime.utcnow())
                        if expires > now:
                            LOG.debug("URL %s not expired (%s > %s)", url, expires, now)
                            return False
                    except Exception:
                        LOG.exception(
                            "Failed to check URL expiry date '%s'",
                            cache_data["expires"],
                        )

            try:
                headers = self.headers(url)
            except requests.exceptions.ConnectionError:
                return False

            cached_etag = cache_data.get("etag")
            remote_etag = headers.get("etag")

            if cached_etag != remote_etag and remote_etag is not None:
                LOG.warning("Remote content of URL %s has changed", url)
                if (
                    SETTINGS.get("download-out-of-date-urls")
                    or self.owner.update_if_out_of_date
                ):
                    LOG.warning("Invalidating cache version and re-downloading %s", url)
                    return True
                LOG.warning(
                    "To enable automatic downloading of updated URLs set the 'download-out-of-date-urls'"
                    " setting to True",
                )
            else:
                LOG.debug("Remote content of URL %s unchanged", url)

        return False
