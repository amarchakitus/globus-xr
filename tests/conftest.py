"""Shared test setup: keep every test away from real tokens and the network."""

import socket

import pytest

from globus_xr import auth


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point HOME and XDG_CONFIG_HOME at a temporary directory.

    globus-sdk stores tokens under ~/.globus/app/ and globus-xr saves its client
    ID under $XDG_CONFIG_HOME, so this keeps tests from reading, writing or
    revoking the real ones.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv(auth.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(auth.LOGIN_MODE_ENV, raising=False)
    yield tmp_path
    # Process-wide caches would otherwise leak between tests.
    for cached in (auth.get_app, auth._transfer, auth.https_server):
        cached.cache_clear()
    auth._HOSTS.clear()
    auth._DOCS.clear()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Refuse connections to anything but localhost, so no test reaches Globus."""
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost") and not isinstance(address, str):
            raise RuntimeError(f"test tried to reach the network: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
