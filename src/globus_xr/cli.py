"""The ``globus-xr`` command: login, logout and ls."""

from __future__ import annotations

import argparse
import sys

from globus_xr import auth


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="globus-xr")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser(
        "login", help="choose collections to authorize (or authorize the IDs given)"
    )
    login.add_argument("collection_ids", nargs="*")
    login.add_argument(
        "--all", action="store_true", help="authorize every collection listed, no prompt"
    )
    login.add_argument("--force", action="store_true", help="log in even if not needed")
    login.add_argument(
        "--no-interactive",
        action="store_true",
        help="numbered table and typed answer instead of the arrow-key checklist",
    )
    login.add_argument("--client-id", help="native app client ID; saved for later sessions")

    logout = sub.add_parser(
        "logout", help="revoke tokens for the given collections, or everything"
    )
    logout.add_argument("collection_ids", nargs="*")

    ls = sub.add_parser("ls", help="list a globus://<collection_id>/<path> URL")
    ls.add_argument("url")
    return parser


def _login(args: argparse.Namespace) -> None:
    if args.client_id:
        auth.set_client_id(args.client_id)
    if args.collection_ids:
        picked, to_revoke = args.collection_ids, []
    else:
        from globus_xr import picker  # the UI libraries are only needed here

        # The checklist needs a real terminal on both ends; otherwise read a
        # typed answer, which also works with piped input.
        interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.no_interactive
        picked, to_revoke = picker.pick_collections(args.all, interactive)
    for collection_id in auth.revoke(to_revoke):
        print(f"revoked: {collection_id} {auth.collection_name(collection_id)}")
    already = {c for c in picked if auth.is_authorized(c)}
    new = [c for c in picked if args.force or c not in already]
    # One login covers Transfer too, so the next run can list the user's own
    # collections.
    if new or args.force or not auth.has_transfer_token():
        auth.login(new, force=args.force)
    for collection_id in picked:
        status = "already authorized" if collection_id in already and not args.force else "authorized"
        print(f"{status}: {collection_id} {auth.collection_name(collection_id)}")


def _logout(args: argparse.Namespace) -> None:
    if not args.collection_ids:
        auth.logout()
        print("Logged out: all globus-xr tokens revoked and deleted.")
        return
    revoked = auth.revoke(args.collection_ids)
    for collection_id in args.collection_ids:
        print(f"{'revoked' if collection_id in revoked else 'not authorized'}: {collection_id}")


def _ls(args: argparse.Namespace) -> None:
    from globus_xr.fs import GlobusFileSystem

    for entry in GlobusFileSystem().ls(args.url, detail=True):
        suffix = "/" if entry["type"] == "directory" else ""
        print(entry["name"].rsplit("/", 1)[-1] + suffix)


def main() -> None:
    args = _parser().parse_args()
    {"login": _login, "logout": _logout, "ls": _ls}[args.command](args)
