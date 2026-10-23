"""Tests for globus_xr.fs against a local server that behaves like GCS HTTPS."""

import asyncio
import functools
import re
import threading
from dataclasses import dataclass

import aiohttp
import fsspec
import pytest
import yarl
from aiohttp import web
from fsspec.implementations.http import HTTPFileSystem

from globus_xr import auth
from globus_xr.fs import GlobusAccessError, GlobusFileSystem

COLLECTION = "11111111-2222-3333-4444-555555555555"
TOKEN = "test-token"
LOGIN_URL = "https://auth.globus.org/v2/oauth2/authorize?client_id=x&scope=y"
PAYLOAD = bytes(range(256)) * 4  # 1 KiB, every offset distinguishable mod 256


# --- Fake GCS HTTPS server ---------------------------------------------------


@dataclass
class Seen:
    method: str
    path: str
    headers: dict
    status: int


class FakeGCS:
    """Serves files from ``root`` the way a GCS HTTPS server does.

    ``mode`` is "token" (no Authorization -> 307 to Globus Auth, wrong token ->
    401), "login" (every request redirected to Globus Auth) or "open" (no auth
    check, for hosts that aren't collections).
    """

    def __init__(self):
        self.root = None
        self.mode = "token"
        self.port = None
        self.log: list[Seen] = []

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    async def handle(self, request):
        resp = self.respond(request)
        self.log.append(
            Seen(request.method, request.path, dict(request.headers), resp.status)
        )
        return resp

    def respond(self, request):
        header = request.headers.get("Authorization")
        if self.mode == "login" or (self.mode == "token" and header is None):
            return web.Response(status=307, headers={"Location": LOGIN_URL})
        if self.mode == "token" and header != f"Bearer {TOKEN}":
            return web.Response(status=401, text="Unauthorized")

        path = self.root / request.path.lstrip("/")
        if path.is_dir():
            # Only hosts that aren't collections list directories (as HTML);
            # GCS itself doesn't, which is why fs.py lists through Transfer.
            if self.mode != "open":
                return web.Response(status=403, text="Forbidden")
            links = "".join(f'<a href="{p.name}">{p.name}</a>' for p in path.iterdir())
            return web.Response(text=f"<html>{links}</html>", content_type="text/html")
        if not path.is_file():
            return web.Response(status=404, text="Not Found")

        data = path.read_bytes()
        headers = {"Accept-Ranges": "bytes"}
        rng = request.headers.get("Range")
        if rng is None:
            return web.Response(body=data, headers=headers)
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
        # GCS rejects suffix ranges ("bytes=-N").
        if m is None or int(m[1]) >= len(data):
            return web.Response(
                status=416, headers={"Content-Range": f"bytes */{len(data)}"}
            )
        start = int(m[1])
        end = min(int(m[2]) if m[2] else len(data) - 1, len(data) - 1)
        headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        return web.Response(status=206, body=data[start : end + 1], headers=headers)

    def requests_to(self, path):
        return [s for s in self.log if s.path == path]


@pytest.fixture(scope="session")
def gcs_server():
    server = FakeGCS()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", server.handle)
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", 0).start())
    server.port = runner.addresses[0][1]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield server
    asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=10)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=10)
    loop.close()


# --- Fake Globus side --------------------------------------------------------


class FakeAuthorizer:
    def __init__(self, app):
        self.app = app

    def get_authorization_header(self):
        return f"Bearer {self.app.token}"


class FakeApp:
    def __init__(self):
        self.token = TOKEN
        self.authorized = []

    def get_authorizer(self, collection_id):
        self.authorized.append(collection_id)
        return FakeAuthorizer(self)


def fake_list_dir(root, collection_id, path):
    """Transfer-style listing of the served directory."""
    assert collection_id == COLLECTION
    target = root / path.lstrip("/")
    if target.is_file():
        raise NotADirectoryError(path)
    if not target.is_dir():
        raise FileNotFoundError(path)
    return [
        {
            "name": p.name,
            "type": "dir" if p.is_dir() else "file",
            "size": 4096 if p.is_dir() else p.stat().st_size,
        }
        for p in sorted(target.iterdir())
    ]


@pytest.fixture
def app(monkeypatch):
    fake = FakeApp()
    # functools.cache gives the fakes the cache_clear() conftest calls.
    monkeypatch.setattr(auth, "get_app", functools.cache(lambda: fake))
    return fake


@pytest.fixture
def gcs(gcs_server, app, tmp_path, monkeypatch):
    """The fake server, serving tmp_path/"collection" as COLLECTION."""
    gcs_server.root = tmp_path / "collection"
    gcs_server.root.mkdir()
    gcs_server.mode = "token"
    gcs_server.log.clear()

    @functools.cache
    def https_server(collection_id):
        if collection_id != COLLECTION:
            raise ValueError(f"unknown collection {collection_id}")
        return gcs_server.base

    monkeypatch.setattr(auth, "https_server", https_server)
    monkeypatch.setattr(
        auth, "list_dir", functools.partial(fake_list_dir, gcs_server.root)
    )
    # The middleware looks hosts up by req.url.host (no port); split_url by
    # netloc (with port). Real GCS hosts have no port, so both keys agree there.
    auth._HOSTS["127.0.0.1"] = COLLECTION
    auth._HOSTS[f"127.0.0.1:{gcs_server.port}"] = COLLECTION
    yield gcs_server
    GlobusFileSystem.clear_instance_cache()


@pytest.fixture
def fs(gcs):
    return GlobusFileSystem(skip_instance_cache=True)


def put(gcs, path, data=PAYLOAD):
    target = gcs.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return f"globus://{COLLECTION}/{path}"


# --- Registry and URL translation --------------------------------------------


def test_registered_with_fsspec():
    assert fsspec.get_filesystem_class("globus") is GlobusFileSystem


def test_strip_protocol_maps_to_https_server(gcs):
    strip = GlobusFileSystem._strip_protocol
    assert strip(f"globus://{COLLECTION}/a/b.nc") == f"{gcs.base}/a/b.nc"
    assert strip(f"globus://{COLLECTION}/a/dir/") == f"{gcs.base}/a/dir"
    assert strip(f"globus://{COLLECTION}/") == gcs.base
    assert strip(f"globus://{COLLECTION}") == gcs.base


def test_strip_protocol_list(gcs):
    paths = [f"globus://{COLLECTION}/x", "https://example.org/y/"]
    assert GlobusFileSystem._strip_protocol(paths) == [
        f"{gcs.base}/x",
        "https://example.org/y/",
    ]


def test_strip_protocol_passes_other_urls_through():
    for url in ("https://example.org/data/", "http://example.org/f.nc", "/local/path"):
        assert GlobusFileSystem._strip_protocol(url) == url


def test_encode_url(fs, gcs):
    assert fs.encode_url(f"globus://{COLLECTION}/a/b") == yarl.URL(f"{gcs.base}/a/b")
    assert fs.encode_url("https://example.org/a") == yarl.URL("https://example.org/a")


# --- Auth middleware ---------------------------------------------------------


def test_cat_file_sends_bearer_token(fs, gcs, app):
    url = put(gcs, "data/file.bin")
    assert fs.cat_file(url) == PAYLOAD
    (seen,) = gcs.requests_to("/data/file.bin")
    assert seen.headers["Authorization"] == f"Bearer {TOKEN}"
    assert app.authorized == [COLLECTION]


def test_unknown_host_gets_no_token(fs, gcs, app):
    put(gcs, "public.bin")
    gcs.mode = "open"
    auth._HOSTS.clear()  # same server, but no longer a known collection host
    assert fs.cat_file(f"{gcs.base}/public.bin") == PAYLOAD
    (seen,) = gcs.requests_to("/public.bin")
    assert "Authorization" not in seen.headers
    assert app.authorized == []


def test_login_redirect_raises(fs, gcs):
    url = put(gcs, "file.bin")
    gcs.mode = "login"
    with pytest.raises(OSError, match="Globus refused access"):
        fs.cat_file(url)
    # The redirect to auth.globus.org must not be followed (conftest would
    # block it anyway); only the one request reached the server.
    assert [s.status for s in gcs.log] == [307]


def test_bad_token_raises(fs, gcs, app):
    url = put(gcs, "file.bin")
    app.token = "expired-token"
    with pytest.raises(OSError, match="Globus refused access"):
        fs.cat_file(url)
    assert [s.status for s in gcs.log] == [401]


def test_bad_token_suffix_read_raises_access_error(fs, gcs, app):
    url = put(gcs, "file.bin")
    app.token = "expired-token"
    with pytest.raises(GlobusAccessError, match="Globus refused access"):
        fs.cat_file(url, start=-16)


def test_real_https_server_with_port_gets_token(gcs_server, app, tmp_path, monkeypatch):
    gcs_server.root = tmp_path / "collection"
    gcs_server.root.mkdir()
    gcs_server.log.clear()
    # "open" so a missing token doesn't trigger a redirect to auth.globus.org.
    gcs_server.mode = "open"
    (gcs_server.root / "f.bin").write_bytes(PAYLOAD)
    app.add_scope_requirements = lambda scopes: None
    app.login_required = lambda: False
    auth._DOCS[COLLECTION] = {"https_server": gcs_server.base + "/"}
    fs = GlobusFileSystem(skip_instance_cache=True)
    try:
        assert fs.cat_file(f"globus://{COLLECTION}/f.bin") == PAYLOAD
        (seen,) = gcs_server.requests_to("/f.bin")
        assert seen.headers.get("Authorization") == f"Bearer {TOKEN}"
    finally:
        GlobusFileSystem.clear_instance_cache()


# --- Range reads -------------------------------------------------------------


def test_server_rejects_suffix_ranges(fs, gcs):
    # Sanity check on the fake: a raw suffix range gets 416, as on GCS.
    url = put(gcs, "file.bin")
    with pytest.raises(aiohttp.ClientResponseError):
        fs.cat_file(url, headers={"Range": "bytes=-16"})
    assert gcs.log[-1].status == 416


def test_suffix_range_read(fs, gcs):
    url = put(gcs, "file.bin")
    assert fs.cat_file(url, start=-16) == PAYLOAD[-16:]
    ranges = [s.headers.get("Range") for s in gcs.log if s.method == "GET"]
    assert ranges == [f"bytes={len(PAYLOAD) - 16}-{len(PAYLOAD) - 1}"]
    assert all(s.status != 416 for s in gcs.log)


def test_suffix_range_longer_than_file(fs, gcs):
    url = put(gcs, "small.bin", b"abc")
    assert fs.cat_file(url, start=-100) == b"abc"


def test_absolute_ranges(fs, gcs):
    url = put(gcs, "file.bin")
    assert fs.cat_file(url, start=10, end=20) == PAYLOAD[10:20]
    assert fs.cat_file(url, start=1000) == PAYLOAD[1000:]
    assert fs.cat_file(url, start=-30, end=-10) == PAYLOAD[-30:-10]
    assert all(s.status == 206 for s in gcs.log if s.method == "GET")


def test_fsspec_open_seek_from_end(gcs):
    url = put(gcs, "file.bin")
    with fsspec.open(url, "rb", block_size=64) as f:
        f.seek(-10, 2)
        assert f.read() == PAYLOAD[-10:]
        f.seek(100)
        assert f.read(5) == PAYLOAD[100:105]


def test_info_reports_size(fs, gcs):
    url = put(gcs, "file.bin")
    info = fs.info(url)
    assert info["size"] == len(PAYLOAD)
    assert info["type"] == "file"


def test_missing_file_is_file_not_found(fs, gcs):
    with pytest.raises(FileNotFoundError):
        fs.cat_file(f"globus://{COLLECTION}/nope.bin")


# --- Listings ----------------------------------------------------------------


def test_sync_ls_uses_transfer_listing(fs, gcs, monkeypatch):
    # Regression: HTTPFileSystem.ls is bound to HTTPFileSystem._ls, which would
    # GET the URL and parse HTML instead of calling auth.list_dir.
    calls = []

    def list_dir(collection_id, path):
        calls.append((collection_id, path))
        return [
            {"name": "sub", "type": "dir", "size": 4096},
            {"name": "f.nc", "type": "file", "size": 123},
        ]

    monkeypatch.setattr(auth, "list_dir", list_dir)
    assert GlobusFileSystem.ls is not HTTPFileSystem.ls
    entries = fs.ls(f"globus://{COLLECTION}/some/dir/")
    assert calls == [(COLLECTION, "/some/dir")]
    assert entries == [
        {"name": f"{gcs.base}/some/dir/sub", "size": None, "type": "directory"},
        {"name": f"{gcs.base}/some/dir/f.nc", "size": 123, "type": "file"},
    ]
    assert gcs.log == []  # nothing went to the HTTPS server


def test_ls_names_only(fs, gcs):
    put(gcs, "store/a.bin")
    put(gcs, "store/nested/b.bin")
    assert fs.ls(f"globus://{COLLECTION}/store", detail=False) == [
        f"{gcs.base}/store/a.bin",
        f"{gcs.base}/store/nested",
    ]


def test_ls_collection_root(fs, gcs):
    put(gcs, "top.bin")
    (entry,) = fs.ls(f"globus://{COLLECTION}/")
    assert entry == {"name": f"{gcs.base}/top.bin", "size": len(PAYLOAD), "type": "file"}


def test_ls_on_file_returns_its_info(fs, gcs):
    url = put(gcs, "store/a.bin")
    (info,) = fs.ls(url)
    assert info["name"] == f"{gcs.base}/store/a.bin"
    assert info["size"] == len(PAYLOAD)
    assert info["type"] == "file"
    assert fs.ls(url, detail=False) == [f"{gcs.base}/store/a.bin"]


def test_isdir(fs, gcs):
    url = put(gcs, "store/a.bin")
    assert fs.isdir(f"globus://{COLLECTION}/store")
    assert fs.isdir(f"globus://{COLLECTION}/store/")
    assert not fs.isdir(url)  # NotADirectoryError
    assert not fs.isdir(f"globus://{COLLECTION}/missing")  # FileNotFoundError


def test_non_collection_url_uses_http_listing(fs, gcs, monkeypatch):
    put(gcs, "pub/a.bin")
    gcs.mode = "open"
    monkeypatch.setattr(auth, "list_dir", pytest.fail)
    auth._HOSTS.clear()
    url = f"{gcs.base}/pub"
    assert fs.ls(url, detail=False) == [f"{url}/a.bin"]
    assert fs.isdir(url)


# --- Zarr / xarray integration -----------------------------------------------


def _zarr3():
    zarr = pytest.importorskip("zarr")
    if int(zarr.__version__.split(".")[0]) < 3:
        pytest.skip("needs zarr 3")
    return zarr


def _write_sharded(zarr, path):
    import numpy as np

    data = np.arange(64, dtype="int32").reshape(8, 8)
    # 2x2 shards, each holding 2x2 chunks, so reading one chunk means reading
    # the shard index from the end of the shard (a suffix range) first.
    arr = zarr.create_array(
        store=str(path), shape=data.shape, chunks=(2, 2), shards=(4, 4), dtype="int32"
    )
    arr[:] = data
    return data


def test_zarr_partial_shard_read(gcs):
    zarr = _zarr3()
    import numpy as np

    data = _write_sharded(zarr, gcs.root / "arr.zarr")
    arr = zarr.open_array(f"globus://{COLLECTION}/arr.zarr", mode="r")
    np.testing.assert_array_equal(arr[0:2, 2:4], data[0:2, 2:4])

    shard = "/arr.zarr/c/0/0"
    gets = [s for s in gcs.requests_to(shard) if s.method == "GET"]
    assert gets and all(s.status == 206 for s in gets)
    size = (gcs.root / shard.lstrip("/")).stat().st_size
    # The shard index was read with an absolute range ending at the last byte.
    assert any(s.headers["Range"].endswith(f"-{size - 1}") for s in gets)
    assert all(s.status != 416 for s in gcs.log)
    assert all(s.headers.get("Authorization") == f"Bearer {TOKEN}" for s in gcs.log)

    np.testing.assert_array_equal(arr[:], data)


def test_zarr_bad_token_raises_instead_of_fill_values(gcs, app):
    zarr = _zarr3()
    _write_sharded(zarr, gcs.root / "arr.zarr")
    arr = zarr.open_array(f"globus://{COLLECTION}/arr.zarr", mode="r")
    app.token = "revoked-token"
    with pytest.raises(GlobusAccessError, match="Globus refused access"):
        arr[0:2, 0:2]  # used to return zeros silently


def test_xarray_open_zarr(gcs):
    _zarr3()
    xr = pytest.importorskip("xarray")
    import numpy as np

    data = np.arange(64, dtype="float32").reshape(8, 8)
    ds = xr.Dataset(
        {"t": (("y", "x"), data)}, coords={"y": np.arange(8), "x": np.arange(8)}
    )
    ds.to_zarr(
        gcs.root / "ds.zarr",
        zarr_format=3,
        consolidated=False,
        encoding={"t": {"chunks": (2, 2), "shards": (4, 4)}},
    )

    # Unconsolidated, so opening lists the group through auth.list_dir.
    opened = xr.open_zarr(f"globus://{COLLECTION}/ds.zarr", consolidated=False)
    one_chunk = opened["t"].isel(y=slice(0, 2), x=slice(4, 6))
    np.testing.assert_array_equal(one_chunk, data[0:2, 4:6])
    xr.testing.assert_equal(opened.load(), ds)
    assert all(s.status != 416 for s in gcs.log)


def test_sessions_verify_https_with_certifi():
    import asyncio

    import aiohttp
    import certifi

    from globus_xr import fs as fs_module

    async def connectors():
        default = await fs_module._get_client()
        own = aiohttp.TCPConnector()
        custom = await fs_module._get_client(connector=own)
        try:
            return default.connector, custom.connector, own
        finally:
            await default.close()
            await custom.close()

    default, custom, own = asyncio.run(connectors())
    assert default._ssl is fs_module._ssl_context()
    # certifi's bundle is loaded, not the interpreter's (possibly empty) defaults.
    loaded = fs_module._ssl_context().cert_store_stats()["x509_ca"]
    with open(certifi.where()) as bundle:
        assert loaded == bundle.read().count("BEGIN CERTIFICATE") > 0
    assert custom is own  # a caller's own connector wins


def test_filesystem_pickles():
    # dask distributed ships the filesystem to workers by pickling it.
    import pickle

    fs = GlobusFileSystem(skip_instance_cache=True)
    clone = pickle.loads(pickle.dumps(fs))
    assert isinstance(clone, GlobusFileSystem)
