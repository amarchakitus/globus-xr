"""Open Globus collections with xarray/zarr/fsspec via ``globus://<collection_id>/<path>``.

Installing this package registers the ``globus`` protocol with fsspec, so e.g.
``xr.open_zarr("globus://<collection_id>/path/to/store.zarr")`` just works.

Modules:

- :mod:`globus_xr.auth`: login, tokens and collection lookups (Globus side)
- :mod:`globus_xr.fs`: the fsspec filesystem
- :mod:`globus_xr.cli` and :mod:`globus_xr.picker`: the ``globus-xr`` command
"""

from globus_xr.auth import (
    discover_collections,
    get_app,
    is_authorized,
    login,
    logout,
    revoke,
    set_client_id,
)
from globus_xr.fs import GlobusAccessError, GlobusFileSystem

__all__ = [
    "GlobusAccessError",
    "GlobusFileSystem",
    "discover_collections",
    "get_app",
    "is_authorized",
    "login",
    "logout",
    "revoke",
    "set_client_id",
]
