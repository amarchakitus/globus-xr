"""Terminal UI for choosing collections: an arrow-key checklist when there's a
terminal, or a numbered table with a typed answer when there isn't."""

from __future__ import annotations

import re
import shutil

import questionary
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from rich import box
from rich.console import Console
from rich.table import Table

from globus_xr import auth

console = Console(highlight=False)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TABLE_KIND_STYLE = {"guest": "cyan", "mapped": "magenta"}
_CHECKLIST_KIND_STYLE = {"guest": "fg:ansicyan", "mapped": "fg:ansimagenta"}
# "selected" styles only the ● marker here, because the row titles are styled.
# noreverse: prompt_toolkit's built-in style draws "selected" in reverse video,
# which turns the ● into a solid block. Otherwise it keeps the terminal's own
# text color, so it shows on light and dark themes.
_CHECKLIST_STYLE = questionary.Style(
    [("selected", "noreverse bold"), ("pointer", "fg:ansiyellow bold")]
)


def pick_collections(select_all: bool, interactive: bool) -> tuple[list[str], list[str]]:
    """List the user's collections and ask which to authorize.

    Listing needs Transfer, so the very first run logs in for that before
    showing the list. Returns (collections to authorize, collections to
    revoke); only the interactive checklist can revoke.
    """
    if not auth.has_transfer_token():
        print("Log in so globus-xr can list your collections.")
        auth.login()
    docs = sorted(
        auth.discover_collections().values(),
        key=lambda d: ((d.get("display_name") or "").lower(), d["id"]),
    )
    if not docs:
        console.print("No collections found in your Globus account.")
        return [], []
    if interactive and not select_all:
        return _pick_interactive(docs)
    return _pick_typed(docs, select_all), []


# --- Shared helpers ----------------------------------------------------------


def _kind(doc: dict) -> str:
    return doc["entity_type"].removeprefix("GCSv5_").removesuffix("_collection")


def _host(doc: dict) -> str:
    # Guest collections live on a mapped collection; mapped ones belong to an
    # organization. Either tells apart collections with the same name.
    return doc.get("host_endpoint_display_name") or doc.get("organization") or ""


def _owner(doc: dict) -> str:
    """Username part of the owner, or blank for service clients (GCS endpoints)."""
    owner = doc.get("owner_string") or ""
    return "" if owner.endswith("@clients.auth.globus.org") else owner.split("@")[0]


def _last_used(doc: dict) -> str:
    return (doc.get("last_accessed_time") or "")[:10]


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


# --- Interactive checklist ---------------------------------------------------


def _pick_interactive(docs: list[dict]) -> tuple[list[str], list[str]]:
    """Arrow-key checklist of the desired state: authorized collections start
    checked, and unchecking one revokes it (after confirmation).

    Returns (collections to authorize, collections to revoke).
    """
    names = [d.get("display_name") or "(unnamed)" for d in docs]
    hosts = [_host(d) for d in docs]
    columns = shutil.get_terminal_size().columns
    name_w = min(max(map(len, names)), max(20, columns // 3))
    host_w = min(max(map(len, hosts)), max(12, columns // 4))
    authorized = {d["id"] for d in docs if auth.is_authorized(d["id"])}
    choices = []
    for doc, name, host in zip(docs, names, hosts):
        kind = _kind(doc)
        # A styled title keeps its own colors; with a plain string, questionary
        # recolors the whole row when it's checked or under the cursor.
        title = [
            ("bold", f"{_clip(name, name_w):<{name_w}}  "),
            (_CHECKLIST_KIND_STYLE.get(kind, ""), f"{kind:<7}"),
            ("", f"{_clip(host, host_w):<{host_w}}  "),
            ("fg:ansibrightblack", _last_used(doc)),
        ]
        owner = _owner(doc)
        description = "  ·  ".join(filter(None, [doc["id"], host, owner and f"owner {owner}"]))
        choices.append(
            questionary.Choice(
                title,
                value=doc["id"],
                checked=doc["id"] in authorized,
                description=description,
            )
        )
    question = questionary.checkbox(
        "Collections globus-xr can access (checked = authorized)",
        choices=choices,
        # No use_search_filter: in questionary 2.1 it lets space select disabled
        # or filtered-out rows, and arrows don't move within the results.
        instruction="(↑/↓ move, space check/uncheck, a all, i invert, enter confirm, q quit)",
        style=_CHECKLIST_STYLE,
    )
    quit_keys = KeyBindings()

    @quit_keys.add("q", eager=True)
    def _quit(event):
        # Same as Ctrl-C, which questionary turns into a None answer.
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    app = question.application
    app.key_bindings = merge_key_bindings([app.key_bindings, quit_keys])
    picked = question.ask(kbi_msg="Cancelled.")
    if picked is None:  # q, Ctrl-C or Ctrl-Q
        return [], []
    to_add = [c for c in picked if c not in authorized]
    to_revoke = [d["id"] for d in docs if d["id"] in authorized and d["id"] not in picked]
    if to_revoke:
        names_by_id = {d["id"]: d.get("display_name") or d["id"] for d in docs}
        for collection_id in to_revoke:
            console.print(f"  [red]-[/] {names_by_id[collection_id]}")
        confirmed = questionary.confirm(
            f"Revoke access to {len(to_revoke)} collection(s)?", default=False
        ).ask()
        if not confirmed:
            console.print("Keeping access to those.")
            to_revoke = []
    return to_add, to_revoke


# --- Table and typed answer --------------------------------------------------


def parse_selection(text: str, count: int) -> list[int | str]:
    """Parse "1,3,5-7", "all", or collection IDs / globus:// URLs.

    Numbers become 0-based indexes into the list; IDs are returned as strings.
    Raises ValueError if anything is invalid.
    """
    text = text.strip().lower()
    if text == "all":
        return list(range(count))
    picked: list[int | str] = []
    for part in filter(None, re.split(r"[,\s]+", text)):
        if match := _UUID.search(part):
            item_ids = [match.group()]
        else:
            first, _, last = part.partition("-")
            lo, hi = int(first), int(last or first)
            if not 1 <= lo <= hi <= count:
                raise ValueError(part)
            item_ids = list(range(lo - 1, hi))
        picked += [i for i in item_ids if i not in picked]
    return picked


def collection_table(docs: list[dict], width: int) -> Table:
    full_ids = width >= 150
    table = Table(
        caption="[green]✓[/] = authorized on this machine",
        caption_justify="left",
        header_style="bold",
        box=box.SIMPLE_HEAD,
    )
    table.add_column("#", justify="right", style="bold")
    table.add_column("✓", justify="center")
    table.add_column("Collection", style="bold", min_width=20, ratio=3)
    table.add_column("Type")
    table.add_column("Host", ratio=2)
    table.add_column("Owner", ratio=1)
    table.add_column("Last used", no_wrap=True)
    table.add_column("ID", style="dim", no_wrap=True)
    for n, doc in enumerate(docs, 1):
        authorized = auth.is_authorized(doc["id"])
        kind = _kind(doc)
        table.add_row(
            str(n),
            "[bold green]✓[/]" if authorized else "",
            doc.get("display_name") or "(unnamed)",
            f"[{_TABLE_KIND_STYLE.get(kind, 'white')}]{kind}[/]",
            _host(doc),
            _owner(doc),
            _last_used(doc),
            doc["id"] if full_ids else doc["id"][:8] + "…",
            style="green" if authorized else None,
        )
    return table


def _pick_typed(docs: list[dict], select_all: bool) -> list[str]:
    """Numbered table plus a typed answer; works without a terminal (e.g. piped)."""
    ids = [d["id"] for d in docs]
    console.print(collection_table(docs, console.width))
    if select_all:
        return ids
    while True:
        text = console.input(
            "[bold]Collections to authorize[/] [dim](e.g. 1,3,5-7, 'all', "
            "collection IDs, or Enter for none)[/]: "
        )
        try:
            return [ids[i] if isinstance(i, int) else i for i in parse_selection(text, len(ids))]
        except ValueError:
            console.print(
                f"[red]Enter numbers between 1 and {len(ids)}, ranges like 2-4, 'all', "
                "or collection IDs.[/]"
            )
