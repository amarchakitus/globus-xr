"""Tests for globus_xr.auth, with Globus stubbed out."""

import functools
import json
import time

import globus_sdk
import pytest
import requests
from globus_sdk.globus_app import GlobusAppConfig
from globus_sdk.login_flows import LocalServerEnvironmentalLoginError
from globus_sdk.scopes import GCSCollectionScopes
from globus_sdk.token_storage import TokenStorageData

from globus_xr import auth

COLL_A = "11111111-2222-3333-4444-555555555555"
COLL_B = "66666666-7777-8888-9999-000000000000"
COLL_C = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
COLL_D = "dddddddd-0000-0000-0000-000000000001"
COLL_E = "eeeeeeee-0000-0000-0000-000000000002"
OTHER_ID = "12345678-1234-1234-1234-123456789abc"
TRANSFER = globus_sdk.TransferClient.resource_server


def cached(value):
    """Stand-in for a functools.cache'd getter (conftest calls cache_clear on it)."""
    return functools.cache(lambda: value)


def token(resource_server, refresh=None):
    return TokenStorageData(
        resource_server=resource_server,
        identity_id=None,
        scope="",
        access_token=f"access-{resource_server}",
        refresh_token=refresh,
        expires_at_seconds=int(time.time()) + 3600,
        token_type="Bearer",
    )


def store(app, *tokens):
    app.token_storage.token_storage.store_token_data_by_resource_server(
        {t.resource_server: t for t in tokens}
    )


def api_error(status, code):
    """A real TransferAPIError, built from an offline requests.Response."""
    r = requests.Response()
    r.status_code = status
    r.reason = "Error"
    r.url = "https://transfer.example.org/operation/ls"
    r.request = requests.Request("GET", r.url).prepare()
    r.headers["Content-Type"] = "application/json"
    r._content = json.dumps({"code": code, "message": "stub"}).encode()
    return globus_sdk.TransferAPIError(r)


def doc(cid, **fields):
    return {
        "id": cid,
        "display_name": f"Collection {cid[:4]}",
        "entity_type": "GCSv5_mapped_collection",
        "https_server": f"https://g-{cid[:4]}.example.org",
        **fields,
    }


@pytest.fixture
def app(monkeypatch):
    """An in-memory Globus app standing in for auth.get_app()."""
    app = globus_sdk.UserApp(
        "globus-xr-test",
        client_id=auth.CLIENT_ID,
        config=GlobusAppConfig(token_storage="memory", request_refresh_tokens=True),
    )
    monkeypatch.setattr(auth, "get_app", cached(app))
    return app


@pytest.fixture
def revoked(monkeypatch):
    """Tokens passed to oauth2_revoke_token, which makes no HTTP call."""
    calls = []
    monkeypatch.setattr(
        globus_sdk.NativeAppAuthClient,
        "oauth2_revoke_token",
        lambda self, tok, **kw: calls.append(tok),
    )
    return calls


class FakeTransfer:
    def __init__(self, scopes=None, bookmarks=(), docs=None, ls=None):
        self.scopes = scopes or {}
        self.bookmarks = [{"endpoint_id": b} for b in bookmarks]
        self.docs = docs or {}
        self.ls = ls  # callable(collection_id, path)
        self.ls_calls = 0
        self.lookups = []
        self.paginated = self

    def endpoint_search(self, filter_scope):
        docs = self.scopes.get(filter_scope, [])
        return type("Paginator", (), {"items": lambda self: iter(docs)})()

    def bookmark_list(self):
        return self.bookmarks

    def get_endpoint(self, cid):
        self.lookups.append(cid)
        if cid not in self.docs:
            raise api_error(404, "EndpointNotFound")
        return type("Response", (), {"data": self.docs[cid]})()

    def operation_ls(self, cid, path):
        self.ls_calls += 1
        return self.ls(cid, path)


# --- Client ID ---------------------------------------------------------------


def test_client_id_env_wins(monkeypatch):
    auth.set_client_id(OTHER_ID)
    monkeypatch.setenv(auth.CLIENT_ID_ENV, COLL_C)
    assert auth._client_id() == COLL_C


def test_client_id_config_beats_builtin():
    auth.set_client_id(OTHER_ID)
    assert auth._client_id() == OTHER_ID


def test_client_id_builtin_default():
    assert auth._client_id() == auth.CLIENT_ID


def test_client_id_missing_raises(monkeypatch):
    monkeypatch.setattr(auth, "CLIENT_ID", None)
    with pytest.raises(RuntimeError, match="--client-id"):
        auth._client_id()


def test_set_client_id_writes_and_keeps_other_keys(isolated_home):
    path = isolated_home / ".config" / "globus-xr" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"other": 1, "client_id": "old"}))
    auth.set_client_id(OTHER_ID)
    assert json.loads(path.read_text()) == {"other": 1, "client_id": OTHER_ID}


def test_set_client_id_clears_caches(monkeypatch):
    fakes = {}
    for name in ("get_app", "_transfer", "https_server"):
        fakes[name] = functools.cache(lambda *args: object())
        fakes[name]()
        monkeypatch.setattr(auth, name, fakes[name])
    auth.set_client_id(OTHER_ID)
    assert all(f.cache_info().currsize == 0 for f in fakes.values())


# --- Login flow --------------------------------------------------------------


class FakeFlow:
    def __init__(self, result=None, error=None):
        self.login_client = globus_sdk.NativeAppAuthClient(auth.CLIENT_ID)
        self.result, self.error, self.calls = result, error, []

    def run_login_flow(self, params):
        self.calls.append(params)
        if self.error:
            raise self.error
        return self.result


def test_login_flow_uses_browser():
    browser, paste = FakeFlow("browser"), FakeFlow("paste")
    assert auth._BrowserOrPaste(browser, paste).run_login_flow("p") == "browser"
    assert paste.calls == []


def test_login_flow_falls_back_to_paste():
    browser = FakeFlow(error=LocalServerEnvironmentalLoginError("no browser"))
    paste = FakeFlow("paste")
    assert auth._BrowserOrPaste(browser, paste).run_login_flow("p") == "paste"
    assert browser.calls == ["p"] and paste.calls == ["p"]


def test_login_flow_paste_mode_skips_browser(monkeypatch):
    monkeypatch.setenv(auth.LOGIN_MODE_ENV, "paste")
    browser, paste = FakeFlow("browser"), FakeFlow("paste")
    assert auth._BrowserOrPaste(browser, paste).run_login_flow("p") == "paste"
    assert browser.calls == []


def test_login_flow_for_globus_app():
    client = globus_sdk.NativeAppAuthClient(auth.CLIENT_ID)
    flow = auth._BrowserOrPaste.for_globus_app("t", client, GlobusAppConfig())
    assert flow.login_client is client


# --- Tokens ------------------------------------------------------------------


def test_tokens_absent(app):
    assert not auth.has_transfer_token()
    assert not auth.is_authorized(COLL_A)


def test_tokens_present(app):
    # The app asks for refresh tokens, so stored tokens need one to count.
    store(app, token(TRANSFER, "r-t"), token(COLL_A, "r-a"))
    assert auth.has_transfer_token()
    assert auth.is_authorized(COLL_A)
    assert not auth.is_authorized(COLL_B)


def test_login_requests_all_scopes_at_once(app, monkeypatch):
    logins = []
    monkeypatch.setattr(
        app,
        "login",
        lambda force=False: logins.append(
            (force, app._auth_params_with_required_scopes().required_scopes)
        ),
    )
    looked_up = []
    monkeypatch.setattr(auth, "_endpoint_doc", looked_up.append)

    assert auth.login([COLL_A, COLL_B]) == [COLL_A, COLL_B]
    assert len(logins) == 1
    force, scopes = logins[0]
    assert not force
    assert "openid" in scopes
    assert str(globus_sdk.TransferClient.scopes.all) in scopes
    for cid in (COLL_A, COLL_B):
        assert f"https://auth.globus.org/scopes/{cid}/https" in scopes
    assert looked_up == [COLL_A, COLL_B]


def test_login_saves_client_id(app, monkeypatch, isolated_home):
    monkeypatch.setattr(app, "login", lambda force=False: None)
    auth.login(client_id=OTHER_ID)
    path = isolated_home / ".config" / "globus-xr" / "config.json"
    assert json.loads(path.read_text())["client_id"] == OTHER_ID


def test_revoke_only_given_collections(app, revoked):
    store(app, token(TRANSFER, "r-t"), token(COLL_A, "r-a"), token(COLL_B))
    assert auth.revoke([COLL_A, COLL_B, COLL_C]) == [COLL_A, COLL_B]
    assert revoked == [f"access-{COLL_A}", "r-a", f"access-{COLL_B}"]
    left = app.token_storage.token_storage.get_token_data_by_resource_server()
    assert set(left) == {TRANSFER}


def test_logout_clears_all_tokens(app, revoked):
    # No scopes registered for these, so the SDK's own app.logout() would keep them.
    store(app, token(TRANSFER), token(COLL_A))
    auth.logout()
    assert app.token_storage.token_storage.get_token_data_by_resource_server() == {}
    assert sorted(revoked) == sorted([f"access-{TRANSFER}", f"access-{COLL_A}"])


# --- Collections -------------------------------------------------------------


def test_discover_merges_scopes_and_bookmarks(monkeypatch):
    ids = (COLL_A, COLL_C, COLL_D, COLL_E)  # one per discovery scope
    scopes = {s: [doc(cid)] for s, cid in zip(auth.DISCOVERY_SCOPES, ids)}
    # A bookmark to an already-found collection isn't looked up again.
    tc = FakeTransfer(scopes=scopes, bookmarks=[COLL_A, COLL_B], docs={COLL_B: doc(COLL_B)})
    monkeypatch.setattr(auth, "_transfer", cached(tc))
    assert set(auth.discover_collections()) == {*ids, COLL_B}
    assert tc.lookups == [COLL_B]
    assert auth.collection_name(COLL_B) == doc(COLL_B)["display_name"]


def test_discover_filters_unusable(monkeypatch):
    docs = [
        doc(COLL_A, entity_type="GCSv5_guest_collection"),
        doc(COLL_B, entity_type="GCSv5_endpoint"),
        doc(COLL_C, https_server=None),
        doc(COLL_D, high_assurance=True),
        doc(COLL_E, non_functional=True),
    ]
    tc = FakeTransfer(scopes={"my-endpoints": docs})
    monkeypatch.setattr(auth, "_transfer", cached(tc))
    assert list(auth.discover_collections()) == [COLL_A]
    # Unusable ones are still remembered for their names.
    assert auth.collection_name(COLL_B)


def test_discover_skips_stale_bookmark(monkeypatch):
    tc = FakeTransfer(bookmarks=[COLL_C], docs={})
    monkeypatch.setattr(auth, "_transfer", cached(tc))
    assert auth.discover_collections() == {}
    assert auth.collection_name(COLL_C) == ""


def test_https_server_no_login_when_authorized(app, monkeypatch):
    monkeypatch.setattr(app, "login_required", lambda: False)
    monkeypatch.setattr(auth, "login", lambda ids: pytest.fail("logged in"))
    auth._DOCS[COLL_A] = doc(COLL_A, https_server="https://g-abc.example.org/")
    assert auth.https_server(COLL_A) == "https://g-abc.example.org"


def test_https_server_logs_in_when_required(app, monkeypatch):
    logins = []
    monkeypatch.setattr(app, "login_required", lambda: True)
    monkeypatch.setattr(auth, "login", logins.append)
    auth._DOCS[COLL_A] = doc(COLL_A)
    auth.https_server(COLL_A)
    assert logins == [[COLL_A]]


def test_https_server_missing_raises(app, monkeypatch):
    monkeypatch.setattr(app, "login_required", lambda: False)
    auth._DOCS[COLL_A] = doc(COLL_A, https_server="")
    with pytest.raises(ValueError, match="no HTTPS server"):
        auth.https_server(COLL_A)


def test_https_server_registers_host(app, monkeypatch):
    monkeypatch.setattr(app, "login_required", lambda: False)
    auth._DOCS[COLL_A] = doc(COLL_A, https_server="https://g-abc.example.org/")
    auth.https_server(COLL_A)
    assert auth.collection_for_host("g-abc.example.org") == COLL_A
    assert auth.collection_for_host(None) is None
    assert auth.split_url("https://g-abc.example.org/a/b.zarr") == (COLL_A, "/a/b.zarr")
    assert auth.split_url("https://g-abc.example.org") == (COLL_A, "/")
    assert auth.split_url("https://g-unknown.example.org/a") is None


# --- list_dir ----------------------------------------------------------------


def failing_ls(*errors, then=()):
    """An operation_ls that raises each error in turn, then returns ``then``."""
    errors = list(errors)

    def ls(cid, path):
        if errors:
            raise errors.pop(0)
        return iter(then)

    return ls


def test_list_dir_returns_entries(monkeypatch):
    tc = FakeTransfer(ls=failing_ls(then=[{"name": "a"}]))
    monkeypatch.setattr(auth, "_transfer", cached(tc))
    assert auth.list_dir(COLL_A, "/") == [{"name": "a"}]


@pytest.mark.parametrize(
    "status, code, exc",
    [
        (400, "ExternalError.DirListingFailed.NotDirectory", NotADirectoryError),
        (404, "ClientError.NotFound", FileNotFoundError),
        (404, "SomethingElse", FileNotFoundError),
        (502, "ExternalError.DirListingFailed.NotFound", FileNotFoundError),
    ],
)
def test_list_dir_maps_errors(monkeypatch, status, code, exc):
    tc = FakeTransfer(ls=failing_ls(api_error(status, code)))
    monkeypatch.setattr(auth, "_transfer", cached(tc))
    with pytest.raises(exc):
        auth.list_dir(COLL_A, "/x")


def test_list_dir_reraises_other_errors(monkeypatch):
    tc = FakeTransfer(ls=failing_ls(api_error(500, "ServiceUnavailable")))
    monkeypatch.setattr(auth, "_transfer", cached(tc))
    with pytest.raises(globus_sdk.TransferAPIError):
        auth.list_dir(COLL_A, "/x")


def test_list_dir_consent_adds_data_access_and_retries(app, monkeypatch):
    logins = []
    monkeypatch.setattr(app, "login", lambda force=False: logins.append(force))
    tc = FakeTransfer(ls=failing_ls(api_error(403, "ConsentRequired"), then=[{"name": "a"}]))
    monkeypatch.setattr(auth, "_transfer", cached(tc))

    assert auth.list_dir(COLL_A, "/") == [{"name": "a"}]
    assert logins == [True]
    data_access = GCSCollectionScopes(COLL_A).data_access.scope_string
    assert any(data_access in str(s) for s in app._scope_requirements[TRANSFER])


def test_list_dir_consent_retries_once(app, monkeypatch):
    logins = []
    monkeypatch.setattr(app, "login", lambda force=False: logins.append(force))
    consent = [api_error(403, "ConsentRequired") for _ in range(3)]
    tc = FakeTransfer(ls=failing_ls(*consent))
    monkeypatch.setattr(auth, "_transfer", cached(tc))

    with pytest.raises(globus_sdk.TransferAPIError):
        auth.list_dir(COLL_A, "/")
    assert tc.ls_calls == 2 and logins == [True]
