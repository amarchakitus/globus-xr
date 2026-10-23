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


async def _get_client(**kwargs) -> aiohttp.ClientSession:
    # Verify HTTPS with certifi's CA bundle, like globus-sdk (via requests) does
    # for the Globus APIs. aiohttp otherwise uses the interpreter's OpenSSL
    # defaults, which on some Python builds point at a CA file that doesn't
    # exist, failing every request.
    kwargs.setdefault("connector", aiohttp.TCPConnector(ssl=_ssl_context()))
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


class GlobusFileSystem(HTTPFileSystem):
    """Read-only access to ``globus://<collection_id>/<path>``."""

    protocol = "globus"

    def __init__(self, *args, client_kwargs=None, **kwargs):
        client_kwargs = dict(client_kwargs or {})
        client_kwargs["middlewares"] = (
            *client_kwargs.get("middlewares", ()),
            _auth_middleware,
        )
        kwargs.setdefault("get_client", _get_client)
        super().__init__(*args, client_kwargs=client_kwargs, **kwargs)

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
            size = (await self._info(url))["size"]
            start, end = max(size + start, 0), size
        return await super()._process_limits(url, start, end)

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
