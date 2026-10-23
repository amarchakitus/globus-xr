# globus-xr

Read data on [Globus](https://www.globus.org/) collections with xarray, zarr and
fsspec, using `globus://<collection_id>/<path>` URLs.

```python
import xarray as xr

ds = xr.open_zarr("globus://<collection_id>/path/to/store.zarr")
```

## Installation

```bash
pip install globus-xr
```

Install it into the environment where you use xarray. In a fresh environment,
`pip install "globus-xr[xarray]"` also installs xarray, zarr and dask.
Requires Python 3.10+. Zarr v3 stores need zarr 3, which needs Python 3.11+.

## Quick start

1. Log in and choose which of your Globus collections to authorize:

   ```bash
   globus-xr login
   ```

2. Open data with `globus://` URLs. Installing the package registers the
   protocol with fsspec, so no import is needed:

   ```python
   import fsspec
   import xarray as xr

   # Zarr
   ds = xr.open_zarr("globus://<collection_id>/path/to/store.zarr")

   # Many stores at once
   ds = xr.open_mfdataset(
       "globus://<collection_id>/path/init_*.zarr",
       engine="zarr",
       combine="by_coords",
       parallel=True,
   )

   # NetCDF / HDF5
   ds = xr.open_dataset(
       fsspec.open("globus://<collection_id>/path/file.nc").open(),
       engine="h5netcdf",
   )
   ```

Opening a collection you haven't authorized starts a login for it.

## CLI

```text
globus-xr login                      choose collections to authorize
globus-xr login <collection_id> ...  authorize specific collections
globus-xr login --all                authorize every listed collection
globus-xr login --no-interactive     numbered list and typed answer, e.g. echo 1,3 | globus-xr login
globus-xr logout <collection_id> ... revoke specific collections
globus-xr logout                     revoke everything
globus-xr ls globus://<collection_id>/<path>
```

In a terminal, `globus-xr login` shows a checklist with authorized collections
already checked. Uncheck one to revoke it.

The list shows collections you've recently used, own, administer, have been
shared, or bookmarked in Globus. The first login authorizes listing them, so
choosing from the list the first time takes two logins. `globus-xr login <id>`
takes one.

Logins open a browser when one is available. Otherwise, for example over SSH,
you paste an authorization code. Tokens refresh automatically, so you stay
logged in until you run `globus-xr logout`.

## Configuration

| Environment variable  | Effect                                                |
| --------------------- | ----------------------------------------------------- |
| `GLOBUS_XR_LOGIN`     | `paste` to always paste the code instead of using a browser |
| `GLOBUS_XR_CLIENT_ID` | use your own Globus app registration instead of the built-in one |

`globus-xr login --client-id <id>` saves a client ID for later sessions.

## Performance

Globus HTTPS servers take about 0.3–0.5 s to answer each request, so open
stores concurrently:

- pass `parallel=True` to `open_mfdataset`
- pass `zarr_format=3, consolidated=True` for consolidated Zarr v3 stores

## Notes

- Access is read-only.
- Works with Globus Connect Server v5 mapped and guest collections that have
  HTTPS enabled. High-assurance collections aren't listed by `globus-xr login`.
- Directory listings (`fs.ls`, globs, Zarr stores without consolidated
  metadata) go through the Globus Transfer API.

## Development

```bash
uv sync         # installs the package plus xarray, zarr, dask and pytest
uv run pytest
```

## License

MIT. See [LICENSE](LICENSE).
