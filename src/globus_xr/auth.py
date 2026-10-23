"""Globus side of globus-xr: login, tokens, and collection lookups.

Tokens live in globus-sdk's token storage under ``~/.globus/app/``. Each
collection's HTTPS access is its own scope and token, so collections are
authorized individually; Transfer is always included, since looking up and
listing collections goes through it.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import globus_sdk
from globus_sdk.globus_app import GlobusAppConfig
from globus_sdk.login_flows import (
    CommandLineLoginFlowManager,
    LocalServerEnvironmentalLoginError,
    LocalServerLoginFlowManager,
    LoginFlowManager,
)
from globus_sdk.scopes import GCSCollectionScopes
from globus_sdk.token_storage import TokenValidationError

# Client ID of the globus-xr native app, registered once by the maintainer at
# https://app.globus.org/settings/developers. It identifies the software, not a
# user, and isn't secret: everyone logs in as themselves through it.
CLIENT_ID = "b1629e83-00e8-4036-a3bd-8a21dc2f9dce"
# Overrides, e.g. to use your own app registration.
CLIENT_ID_ENV = "GLOBUS_XR_CLIENT_ID"
# Set to "paste" to always paste the authorization code instead of having a
# local browser hand it back automatically.
LOGIN_MODE_ENV = "GLOBUS_XR_LOGIN"

# Where to look for the user's collections: Transfer endpoint search scopes,
# plus the user's bookmarks.
DISCOVERY_SCOPES = ("recently-used", "my-endpoints", "shared-with-me", "administered-by-me")

# HTTPS hostname -> collection ID, filled in as collections are resolved.
_HOSTS: dict[str, str] = {}
# Collection ID -> Transfer endpoint document, from lookups and discovery.
_DOCS: dict[str, dict] = {}


# --- Client ID and app -------------------------------------------------------


def _config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "globus-xr" / "config.json"


def set_client_id(client_id: str) -> None:
    """Save the native-app client ID so later sessions pick it up."""
    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads(path.read_text()) if path.exists() else {}
    config["client_id"] = client_id
    path.write_text(json.dumps(config, indent=2) + "\n")
    # Anything built from the previous client ID is stale now.
    for cached in (get_app, _transfer, https_server):
        cached.cache_clear()


def _client_id() -> str:
    if client_id := os.environ.get(CLIENT_ID_ENV):
        return client_id
    path = _config_file()
    if path.exists() and (client_id := json.loads(path.read_text()).get("client_id")):
        return client_id
    if CLIENT_ID:
        return CLIENT_ID
    raise RuntimeError(
        "No Globus client ID configured. Register a native app at "
        "https://app.globus.org/settings/developers, then run "
        f"`globus-xr login --client-id <id>` (or set {CLIENT_ID_ENV})."
    )


class _BrowserOrPaste(LoginFlowManager):
    """Log in through a local browser, which hands the code back automatically,
    or fall back to pasting it (over SSH, or with no usable browser)."""

    def __init__(self, browser: LoginFlowManager, paste: LoginFlowManager):
        super().__init__(paste.login_client)
        self._browser = browser
        self._paste = paste

    @classmethod
    def for_globus_app(cls, app_name, login_client, config):
        return cls(
            LocalServerLoginFlowManager.for_globus_app(app_name, login_client, config),
            CommandLineLoginFlowManager.for_globus_app(app_name, login_client, config),
        )

    def run_login_flow(self, auth_parameters):
        if os.environ.get(LOGIN_MODE_ENV) != "paste":
            try:
                return self._browser.run_login_flow(auth_parameters)
            except LocalServerEnvironmentalLoginError:
                pass
        return self._paste.run_login_flow(auth_parameters)


@functools.cache
def get_app() -> globus_sdk.UserApp:
    """The process-wide Globus app; tokens are cached under ~/.globus/app/."""
    return globus_sdk.UserApp(
        "globus-xr",
        client_id=_client_id(),
        config=GlobusAppConfig(
            request_refresh_tokens=True, login_flow_manager=_BrowserOrPaste
        ),
    )


@functools.cache
def _transfer() -> globus_sdk.TransferClient:
    return globus_sdk.TransferClient(app=get_app())


# --- Tokens ------------------------------------------------------------------


def _has_token(resource_server: str) -> bool:
    try:
        return get_app().token_storage.get_token_data(resource_server) is not None
    except TokenValidationError:
        return False


def has_transfer_token() -> bool:
    """Whether Transfer is authorized, so collections can be looked up and listed."""
    return _has_token(globus_sdk.TransferClient.resource_server)


def is_authorized(collection_id: str) -> bool:
    """Whether a usable HTTPS token for the collection is already stored."""
    return _has_token(collection_id)


def _require(collection_ids) -> None:
    """Add the HTTPS scope for each collection to the app's requirements."""
    get_app().add_scope_requirements(
        {cid: [GCSCollectionScopes(cid).https] for cid in collection_ids}
    )


def _require_data_access(collection_id: str) -> None:
    # Mapped collections need data_access consent for Transfer listings. It's
    # only requested when a listing asks for it, to keep consent pages simple.
    get_app().add_scope_requirements(
        {
            globus_sdk.TransferClient.resource_server: [
                globus_sdk.TransferClient.scopes.all.with_dependency(
                    GCSCollectionScopes(collection_id).data_access.with_optional(True)
                )
            ]
        }
    )


def login(
    collection_ids=(),
    *,
    client_id: str | None = None,
    force: bool = False,
) -> list[str]:
    """Authorize Transfer and the given collections with one login.

    Collections already authorized don't trigger a login on their own; use
    ``force`` to log in anyway. Returns the collection IDs requested.

    ``client_id``, if given, is saved (see :func:`set_client_id`) so later
    sessions don't need it.
    """
    if client_id:
        set_client_id(client_id)
    collection_ids = list(collection_ids)
    # Creating the Transfer client registers its scope, so this same login
    # covers the collection lookups and listings that follow.
    _transfer()
    _require(collection_ids)
    get_app().login(force=force)
    for collection_id in collection_ids:
        _endpoint_doc(collection_id)
    return collection_ids


def revoke(collection_ids) -> list[str]:
    """Revoke and delete this machine's tokens for the given collections.

    The consent granted to globus-xr stays in the user's Globus account, so
    authorizing a collection again needs a login but usually no new consent.
    Returns the IDs that had tokens.
    """
    app = get_app()
    storage = app.token_storage.token_storage  # unvalidated, so stale tokens count too
    auth_client = globus_sdk.NativeAppAuthClient(app.client_id)
    revoked = []
    for collection_id in collection_ids:
        data = storage.get_token_data(collection_id)
        if data is None:
            continue
        auth_client.oauth2_revoke_token(data.access_token)
        if data.refresh_token:
            auth_client.oauth2_revoke_token(data.refresh_token)
        storage.remove_token_data(collection_id)
        revoked.append(collection_id)
    return revoked


def logout() -> None:
    """Revoke and delete every globus-xr token on this machine."""
    # Not app.logout(): it only clears tokens for scopes registered in this
    # process (unless sweep=True, which needs globus-sdk 4.9+), which would
    # leave Transfer and collection tokens behind.
    storage = get_app().token_storage.token_storage
    revoke(list(storage.get_token_data_by_resource_server()))


# --- Collections -------------------------------------------------------------


def _endpoint_doc(collection_id: str) -> dict:
    if collection_id not in _DOCS:
        _DOCS[collection_id] = _transfer().get_endpoint(collection_id).data
    return _DOCS[collection_id]


def collection_name(collection_id: str) -> str:
    """Display name of a collection already looked up in this session, else ''."""
    return _DOCS.get(collection_id, {}).get("display_name") or ""


def _usable(doc: dict) -> bool:
    return (
        doc["entity_type"] in ("GCSv5_mapped_collection", "GCSv5_guest_collection")
        and bool(doc.get("https_server"))
        and not doc.get("high_assurance")
        and not doc.get("non_functional")
    )


def discover_collections() -> dict[str, dict]:
    """Collections the user has used, owns, administers, was shared or bookmarked."""
    tc = _transfer()
    found: dict[str, dict] = {}
    for scope in DISCOVERY_SCOPES:
        for doc in tc.paginated.endpoint_search(filter_scope=scope).items():
            found[doc["id"]] = doc
    for bookmark in tc.bookmark_list():
        if bookmark["endpoint_id"] not in found:
            try:
                found[bookmark["endpoint_id"]] = _endpoint_doc(bookmark["endpoint_id"])
            except globus_sdk.TransferAPIError:
                continue  # stale bookmark to a deleted collection
    _DOCS.update(found)
    return {cid: doc for cid, doc in found.items() if _usable(doc)}


@functools.cache
def https_server(collection_id: str) -> str:
    """Look up a collection's HTTPS base URL, logging in if it isn't authorized yet."""
    app = get_app()
    _require([collection_id])
    if app.login_required():
        login([collection_id])
    doc = _endpoint_doc(collection_id)
    if not doc.get("https_server"):
        raise ValueError(
            f"Collection {collection_id} ({doc.get('display_name')}) has no HTTPS server"
        )
    server = doc["https_server"].rstrip("/")
    _HOSTS[urlsplit(server).hostname] = collection_id
    return server


def collection_for_host(host: str | None) -> str | None:
    """The collection served from an HTTPS hostname (no port), if it has been resolved."""
    return _HOSTS.get(host or "")


def split_url(url: str) -> tuple[str, str] | None:
    """Map an https URL back to (collection_id, path) if it is a known collection."""
    parts = urlsplit(url)
    collection_id = collection_for_host(parts.hostname)
    if collection_id is None:
        return None
    return collection_id, parts.path or "/"


def list_dir(collection_id: str, path: str, *, retry: bool = True) -> list[dict]:
    """List a directory through Transfer, asking for data_access consent if needed."""
    try:
        return list(_transfer().operation_ls(collection_id, path=path))
    except globus_sdk.TransferAPIError as e:
        code = e.code or ""
        if code == "ConsentRequired" and retry:
            _require_data_access(collection_id)
            get_app().login(force=True)
            return list_dir(collection_id, path, retry=False)
        if "NotDirectory" in code:
            raise NotADirectoryError(path) from e
        if e.http_status == 404 or "NotFound" in code:
            raise FileNotFoundError(path) from e
        raise
