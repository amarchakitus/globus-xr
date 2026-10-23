"""fsspec filesystem for Globus Connect Server collections over HTTPS.

URLs look like ``globus://<collection_id>/<path>``. Reads go straight to the
collection's HTTPS server with a bearer token; directory listings go through
the Globus Transfer API, so Zarr stores open with or without consolidated
metadata.
"""

from __future__ import annotations

import asyncio
import functools
import posixpath
import ssl

import aiohttp
import certifi
from fsspec.asyn import sync_wrapper
from fsspec.implementations.http import HTTPFileSystem

from globus_xr import auth


class GlobusAccessError(PermissionError):
    """Globus refused a request: no access, or a missing, expired or revoked token."""


def _access_error(exc: BaseException | None) -> GlobusAccessError | None:
    """The GlobusAccessError behind an exception, if any (aiohttp and fsspec wrap it)."""
    while exc is not None:
        if isinstance(exc, GlobusAccessError):
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


@functools.cache
def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


# Connections per HTTPS server for each filesystem. dask reading a sharded store
# chunk by chunk otherwise opens dozens at once, and a GCS server short of
# Apache workers then stops answering anyone for minutes.
MAX_CONNECTIONS = 16
# Largest file tail kept in memory. zarr reads a shard's index from its end:
# 16 bytes per chunk plus a checksum, so a few KB for typical shards.
MAX_CACHED_TAIL = 2**20
# Range reads of one file that arrive within COALESCE_WINDOW seconds of each
# other are merged into one request when at most COALESCE_MAX_GAP bytes apart,
# up to COALESCE_MAX_BYTES per request. Each request costs GCS 0.2-0.5 s, so a
# short wait is worth it when dask reads many chunks of one shard at once.
COALESCE_WINDOW = 0.005
COALESCE_MAX_GAP = 2**20
COALESCE_MAX_BYTES = 32 * 2**20


async def _get_client(
    limit_per_host=MAX_CONNECTIONS, **kwargs
) -> aiohttp.ClientSession:
    # Verify HTTPS with certifi's CA bundle, like globus-sdk (via requests) does
    # for the Globus APIs. aiohttp otherwise uses the interpreter's OpenSSL
    # defaults, which on some Python builds point at a CA file that doesn't
    # exist, failing every request.
    kwargs.setdefault(
        "connector",
        aiohttp.TCPConnector(ssl=_ssl_context(), limit_per_host=limit_per_host),
    )
    return aiohttp.ClientSession(**kwargs)


async def _auth_middleware(
    req: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
) -> aiohttp.ClientResponse:
    collection_id = auth.collection_for_host(req.url.host)
    if collection_id is None:
        return await handler(req)
    authorizer = auth.get_app().get_authorizer(collection_id)
    # May refresh the token over the network, so keep it off the event loop.
    req.headers["Authorization"] = await asyncio.to_thread(
        authorizer.get_authorization_header
    )
    resp = await handler(req)
    # A bad token gets a 401/403; a missing one gets redirected to a login page,
    # which, if followed, would hand back that HTML in place of the file.
    location = resp.headers.get("Location", "")
    if resp.status in (401, 403) or (
        resp.status in (301, 302, 303, 307, 308) and "auth.globus.org" in location
    ):
        resp.release()
        raise GlobusAccessError(
            f"Globus refused access to {req.url} for collection {collection_id}. "
            "Check that your identity has access, or run `globus-xr logout` and retry."
        )
    return resp


async def _shared(cache: dict, key, fetch):
    """Await ``fetch()`` once per key, sharing the result with concurrent and
    later callers. A failure isn't kept, so the next call retries."""
    if (pending := cache.get(key)) is None:
        pending = cache[key] = asyncio.ensure_future(fetch())
    try:
        # shield: one cancelled caller mustn't cancel the fetch for the rest.
        return await asyncio.shield(pending)
    except BaseException:
        if pending.done() and cache.get(key) is pending:
            del cache[key]
        raise


def _merge_ranges(reads):
    """Group (start, end, future) reads into runs to fetch as one range each."""
    groups = []
    for read in sorted(reads, key=lambda r: r[0]):
        if groups:
            group = groups[-1]
            lo, hi = group[0][0], max(r[1] for r in group)
            near = read[0] - hi <= COALESCE_MAX_GAP
            if near and max(hi, read[1]) - lo <= COALESCE_MAX_BYTES:
                group.append(read)
                continue
        groups.append([read])
    return groups


class GlobusFileSystem(HTTPFileSystem):
    """Read-only access to ``globus://<collection_id>/<path>``."""

    protocol = "globus"

    def __init__(
        self, *args, max_connections=MAX_CONNECTIONS, client_kwargs=None, **kwargs
    ):
        client_kwargs = dict(client_kwargs or {})
        client_kwargs["middlewares"] = (
            *client_kwargs.get("middlewares", ()),
            _auth_middleware,
        )
        kwargs.setdefault(
            "get_client", functools.partial(_get_client, limit_per_host=max_connections)
        )
        super().__init__(*args, client_kwargs=client_kwargs, **kwargs)
        self._sizes: dict[str, asyncio.Future] = {}
        self._tails: dict[tuple[str, int], asyncio.Future] = {}
        self._batches: dict[str, list] = {}  # url -> reads waiting to be merged
        self._flushes: set[asyncio.Task] = set()  # keeps flush tasks referenced

    @classmethod
    def _strip_protocol(cls, path):
        if isinstance(path, list):
            return [cls._strip_protocol(p) for p in path]
        if path.startswith("globus://"):
            collection_id, _, rest = path.removeprefix("globus://").partition("/")
            return f"{auth.https_server(collection_id)}/{rest}".rstrip("/")
        return path

    def encode_url(self, url):
        # Every request URL passes through here, so methods called directly
        # with globus:// URLs (fs.cat_file, fs.open, ...) work too.
        return super().encode_url(self._strip_protocol(url))

    async def _info(self, url, **kwargs):
        try:
            return await super()._info(url, **kwargs)
        except FileNotFoundError as e:
            # fsspec reports any failed HEAD/GET as a missing file. For an access
            # error that's wrong, and zarr would read a "missing" shard as fill
            # values instead of failing.
            if (denied := _access_error(e)) is not None:
                raise denied from None
            raise

    async def _process_limits(self, url, start, end):
        # GCS answers suffix ranges ("bytes=-N") with 416, and zarr uses them to
        # read shard indexes, so make them absolute using the size from HEAD.
        if start is not None and start < 0 and end is None:
            size = await self._size(url)
            start, end = max(size + start, 0), size
        return await super()._process_limits(url, start, end)

    async def _cat_file(self, url, start=None, end=None, **kwargs):
        # zarr reads a shard's index once per chunk it reads from the shard,
        # often all at once from dask, so fetch each file's tail only once.
        tail = start is not None and -MAX_CACHED_TAIL <= start < 0 and end is None
        if tail and not kwargs:
            fetch = super()._cat_file
            return await _shared(self._tails, (url, start), lambda: fetch(url, start))
        ranged = start is not None and end is not None and 0 <= start < end
        if ranged and not kwargs:
            return await self._coalesced(url, start, end)
        return await super()._cat_file(url, start, end, **kwargs)

    async def _coalesced(self, url, start, end):
        # zarr reads each chunk of a shard as its own range; with dask, dozens
        # arrive together. Queue the read so a flush can merge its neighbours.
        future = asyncio.get_running_loop().create_future()
        if url not in self._batches:
            self._batches[url] = []
            flush = asyncio.ensure_future(self._flush(url))
            self._flushes.add(flush)
            flush.add_done_callback(self._flushes.discard)
        self._batches[url].append((start, end, future))
        return await future

    async def _flush(self, url):
        # A task of its own, so a cancelled read can't strand the others.
        await asyncio.sleep(COALESCE_WINDOW)
        reads = self._batches.pop(url)
        try:
            await asyncio.gather(
                *(self._read_group(url, group) for group in _merge_ranges(reads))
            )
        except BaseException as e:
            for _, _, future in reads:
                if future.done():
                    continue
                if isinstance(e, asyncio.CancelledError):
                    future.cancel()
                else:
                    future.set_exception(e)
            raise

    async def _read_group(self, url, group):
        lo, hi = group[0][0], max(end for _, end, _ in group)
        try:
            data = await super()._cat_file(url, lo, hi)
        except Exception as e:
            for _, _, future in group:
                if not future.done():
                    future.set_exception(e)
            return
        for start, end, future in group:
            if future.done():  # its caller was cancelled
                continue
            if start > lo and start - lo >= len(data):
                # Past the end of the file: let the server answer it alone.
                await self._read_group(url, [(start, end, future)])
            else:
                future.set_result(data[start - lo : end - lo])

    async def _size(self, url):
        return await _shared(self._sizes, url, lambda: self._head_size(url))

    async def _head_size(self, url):
        size = (await self._info(url))["size"]
        if size is None:
            raise OSError(f"The server didn't report the size of {url}")
        return size

    def invalidate_cache(self, path=None):
        self._sizes.clear()
        self._tails.clear()
        super().invalidate_cache(path)

    async def _ls(self, url, detail=True, **kwargs):
        url = self._strip_protocol(url).rstrip("/")
        target = auth.split_url(url)
        if target is None:
            return await super()._ls(url, detail=detail, **kwargs)
        try:
            listing = await asyncio.to_thread(auth.list_dir, *target)
        except NotADirectoryError:
            info = await self._info(url)
            return [info] if detail else [info["name"]]
        entries = [
            {
                "name": posixpath.join(url, item["name"]),
                "size": item["size"] if item["type"] == "file" else None,
                "type": "directory" if item["type"] == "dir" else "file",
            }
            for item in listing
        ]
        return entries if detail else [e["name"] for e in entries]

    # HTTPFileSystem binds its sync ls to its own _ls, bypassing the override.
    ls = sync_wrapper(_ls)

    async def _isdir(self, path):
        url = self._strip_protocol(path).rstrip("/")
        target = auth.split_url(url)
        if target is None:
            return await super()._isdir(url)
        try:
            await asyncio.to_thread(auth.list_dir, *target)
        except (FileNotFoundError, NotADirectoryError):
            return False
        return True
