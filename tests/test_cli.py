"""Tests for the ``globus-xr`` command and its collection picker, with Globus stubbed out."""

from __future__ import annotations

import contextlib
import io
import signal
import subprocess
import sys
import textwrap

import pytest
import questionary
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from globus_xr import auth, cli, fs, picker

ALPHA = "11111111-2222-3333-4444-555555555555"
BETA = "22222222-3333-4444-5555-666666666666"
GAMMA = "33333333-4444-5555-6666-777777777777"
SERVICE = "44444444-5555-6666-7777-888888888888"

# Listed in this order once sorted by name: Alpha, Beta, Gamma.
DOCS = {
    GAMMA: {
        "id": GAMMA,
        "display_name": "Gamma Store",
        "entity_type": "GCSv5_mapped_collection",
        "organization": "Example University",
        "owner_string": f"{SERVICE}@clients.auth.globus.org",
    },
    ALPHA: {
        "id": ALPHA,
        "display_name": "Alpha Archive",
        "entity_type": "GCSv5_guest_collection",
        "host_endpoint_display_name": "Example Cluster",
        "organization": "Example Org",
        "owner_string": "alice@example.org",
        "last_accessed_time": "2026-01-02T03:04:05+00:00",
    },
    BETA: {
        "id": BETA,
        "display_name": "Beta Results",
        "entity_type": "GCSv5_guest_collection",
        "host_endpoint_display_name": "Example Cluster",
        "owner_string": "bob@example.org",
    },
}


class FakeAuth:
    """Stand-in for the Globus side of ``globus_xr.auth``; records what was asked of it."""

    def __init__(self, transfer: bool, authorized=(), docs=DOCS):
        self.transfer = transfer
        self.authorized = set(authorized)
        self.docs = docs
        self.logins: list[tuple[list[str], bool]] = []
        self.revoked: list[str] = []
        self.logouts = 0
        self.client_ids: list[str] = []
        self.discovered = 0

    def login(self, collection_ids=(), *, client_id=None, force=False):
        self.logins.append((list(collection_ids), force))
        self.transfer = True
        self.authorized |= set(collection_ids)
        return list(collection_ids)

    def revoke(self, collection_ids):
        hit = [c for c in collection_ids if c in self.authorized]
        self.authorized -= set(hit)
        self.revoked += hit
        return hit

    def logout(self):
        self.logouts += 1
        self.transfer = False
        self.authorized.clear()

    def discover_collections(self):
        assert self.transfer, "listed collections before Transfer was authorized"
        self.discovered += 1
        return dict(self.docs)

    def collection_name(self, collection_id):
        return self.docs.get(collection_id, {}).get("display_name") or ""


@pytest.fixture
def fake_auth(monkeypatch):
    """Install a FakeAuth; call it with (transfer, authorized) to set the starting state."""
    fake = FakeAuth(transfer=False)

    monkeypatch.setattr(auth, "login", fake.login)
    monkeypatch.setattr(auth, "revoke", fake.revoke)
    monkeypatch.setattr(auth, "logout", fake.logout)
    monkeypatch.setattr(auth, "discover_collections", fake.discover_collections)
    monkeypatch.setattr(auth, "collection_name", fake.collection_name)
    monkeypatch.setattr(auth, "has_transfer_token", lambda: fake.transfer)
    monkeypatch.setattr(auth, "is_authorized", lambda c: c in fake.authorized)
    monkeypatch.setattr(auth, "set_client_id", fake.client_ids.append)

    def setup(transfer=True, authorized=()):
        fake.transfer = transfer
        fake.authorized = set(authorized)
        return fake

    return setup


@pytest.fixture(autouse=True)
def hang_guard():
    """Fail a test that's still running after 20 s (e.g. a prompt waiting for keys)."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def timeout(signum, frame):
        raise TimeoutError("test took too long; is a prompt waiting for input?")

    previous = signal.signal(signal.SIGALRM, timeout)
    signal.alarm(20)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def console(monkeypatch):
    """Capture what the picker prints with rich; returns the buffer."""

    def install(width=120):
        buffer = io.StringIO()
        monkeypatch.setattr(picker, "console", Console(file=buffer, width=width))
        return buffer

    return install


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def keys(monkeypatch):
    """Feed keystrokes to the checklist and the revoke confirmation.

    Returns a function that sends the keys and records each prompt's arguments
    in ``prompts``. The input is closed after the keys, so a prompt still open
    when they run out raises EOFError instead of waiting forever.
    """
    real_checkbox, real_confirm = questionary.checkbox, questionary.confirm
    prompts = {"checkbox": [], "confirm": [], "questions": []}
    pipes = []

    def wrap(name, real):
        def prompt(*args, **kwargs):
            prompts[name].append((args, kwargs))
            question = real(*args, input=pipes[-1], output=DummyOutput(), **kwargs)
            prompts["questions"].append(question)
            return question

        monkeypatch.setattr(picker.questionary, name, prompt)

    wrap("checkbox", real_checkbox)
    wrap("confirm", real_confirm)

    with contextlib.ExitStack() as stack:

        def send(text):
            pipe = stack.enter_context(create_pipe_input())
            pipe.send_text(text)
            pipe.close()
            pipes.append(pipe)

        send.prompts = prompts
        yield send


def run_cli(monkeypatch, capsys, argv, stdin="", tty=False):
    """Run ``globus-xr <argv>`` and return everything it printed."""
    monkeypatch.setattr(sys, "argv", ["globus-xr", *argv])
    out = FakeTTY() if tty else io.StringIO()
    monkeypatch.setattr(sys, "stdin", FakeTTY(stdin) if tty else io.StringIO(stdin))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(picker, "console", Console(file=out, width=120))
    cli.main()
    return out.getvalue()


# --- parse_selection -----------------------------------------------------------


@pytest.mark.parametrize(
    "text, count, expected",
    [
        ("1,3,5-7", 8, [0, 2, 4, 5, 6]),
        ("2-4", 5, [1, 2, 3]),
        ("1 3", 3, [0, 2]),
        ("1, 3", 3, [0, 2]),
        ("1,1,1-2", 3, [0, 1]),
        ("all", 3, [0, 1, 2]),
        (" ALL ", 3, [0, 1, 2]),
        ("", 3, []),
        ("   ", 3, []),
        (ALPHA, 3, [ALPHA]),
        (ALPHA.upper(), 3, [ALPHA]),
        (f"globus://{ALPHA}/some/path", 3, [ALPHA]),
        (f"GLOBUS://{ALPHA.upper()}/", 3, [ALPHA]),
        (f"2,{BETA},{BETA}", 3, [1, BETA]),
    ],
)
def test_parse_selection(text, count, expected):
    assert picker.parse_selection(text, count) == expected


@pytest.mark.parametrize("text", ["0", "4", "3-1", "0-2", "2-9", "x", "-1", "1-2-3", "all,1", "globus://abc"])
def test_parse_selection_rejects(text):
    with pytest.raises(ValueError):
        picker.parse_selection(text, 3)


# --- Table and typed answer ----------------------------------------------------


def _render_table(width):
    buffer = io.StringIO()
    docs = sorted(DOCS.values(), key=lambda d: d["display_name"])
    Console(file=buffer, width=width).print(picker.collection_table(docs, width))
    return buffer.getvalue()


def test_table_columns_and_cells(fake_auth):
    fake_auth(authorized={BETA})
    table = _render_table(160)
    header = next(line for line in table.splitlines() if "Collection" in line)
    for column in ["#", "✓", "Collection", "Type", "Host", "Owner", "Last used", "ID"]:
        assert column in header
    assert header.index("#") < header.index("Collection") < header.index("Type") < header.index("ID")
    rows = {name: line for line in table.splitlines() for name in ("Alpha", "Beta", "Gamma") if name in line}
    assert "✓" in rows["Beta"] and "✓" not in rows["Alpha"]
    assert "guest" in rows["Alpha"] and "mapped" in rows["Gamma"]
    assert "2026-01-02" in rows["Alpha"] and "03:04" not in rows["Alpha"]


def test_table_host_prefers_host_endpoint_then_organization(fake_auth):
    fake_auth()
    rows = {name: line for line in _render_table(160).splitlines() for name in ("Alpha", "Gamma") if name in line}
    assert "Example Cluster" in rows["Alpha"] and "Example Org" not in rows["Alpha"]
    assert "Example University" in rows["Gamma"]


def test_table_owner_is_username_and_blank_for_service_clients(fake_auth):
    fake_auth()
    table = _render_table(160)
    assert "alice" in table and "bob" in table
    assert "@" not in table
    assert SERVICE[:8] not in table


@pytest.mark.parametrize("width, full", [(160, True), (150, True), (149, False), (100, False)])
def test_table_full_ids_only_when_wide(fake_auth, width, full):
    fake_auth()
    table = _render_table(width)
    for collection_id in DOCS:
        assert (collection_id in table) is full
        assert (collection_id[:8] + "…" in table) is not full


def test_typed_mode_shows_numbered_table(fake_auth, console, monkeypatch):
    fake_auth()
    out = console(width=160)
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    picker.pick_collections(select_all=False, interactive=False)
    lines = out.getvalue().splitlines()
    # Numbered in name order.
    order = [next(i for i, line in enumerate(lines) if name in line) for name in ("Alpha", "Beta", "Gamma")]
    assert order == sorted(order)
    assert lines[order[0]].split()[0] == "1" and lines[order[2]].split()[0] == "3"


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("\n", []),
        ("1\n", [ALPHA]),
        ("1,3\n", [ALPHA, GAMMA]),
        ("2-3\n", [BETA, GAMMA]),
        ("all\n", [ALPHA, BETA, GAMMA]),
        (f"globus://{GAMMA.upper()}/data\n", [GAMMA]),
        ("nope\n9\n2\n", [BETA]),
    ],
)
def test_typed_answer(fake_auth, console, monkeypatch, answer, expected):
    fake_auth()
    console()
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert picker.pick_collections(select_all=False, interactive=False) == (expected, [])


def test_typed_invalid_answer_reprompts_with_error(fake_auth, console, monkeypatch):
    fake_auth()
    out = console()
    monkeypatch.setattr(sys, "stdin", io.StringIO("0\n1\n"))
    assert picker.pick_collections(select_all=False, interactive=False) == ([ALPHA], [])
    text = out.getvalue()
    assert "Enter numbers between 1 and 3" in text
    assert text.count("Collections to authorize") == 2


def test_typed_mode_never_revokes(fake_auth, console, monkeypatch):
    fake_auth(authorized={ALPHA, BETA})
    console()
    monkeypatch.setattr(sys, "stdin", io.StringIO("3\n"))
    assert picker.pick_collections(select_all=False, interactive=False) == ([GAMMA], [])


@pytest.mark.parametrize("interactive", [False, True])
def test_select_all_picks_everything_without_prompting(fake_auth, console, monkeypatch, interactive):
    fake_auth(authorized={BETA})
    console()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # reading it would raise EOFError
    monkeypatch.setattr(picker.questionary, "checkbox", lambda *a, **k: pytest.fail("prompted"))
    assert picker.pick_collections(select_all=True, interactive=interactive) == ([ALPHA, BETA, GAMMA], [])


def test_first_run_logs_in_before_listing(fake_auth, console, monkeypatch, capsys):
    fake = fake_auth(transfer=False)
    console()
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    picker.pick_collections(select_all=False, interactive=False)
    assert fake.logins == [([], False)]
    assert fake.discovered == 1
    assert "Log in so globus-xr can list your collections" in capsys.readouterr().out


def test_returning_user_lists_without_logging_in(fake_auth, console, monkeypatch):
    fake = fake_auth(transfer=True)
    console()
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    picker.pick_collections(select_all=False, interactive=False)
    assert fake.logins == []


def test_no_collections_found(fake_auth, console, monkeypatch):
    fake = fake_auth()
    fake.docs = {}
    out = console()
    monkeypatch.setattr(picker.questionary, "checkbox", lambda *a, **k: pytest.fail("prompted"))
    assert picker.pick_collections(select_all=False, interactive=True) == ([], [])
    assert "No collections found" in out.getvalue()


# --- Interactive checklist -----------------------------------------------------

DOWN, UP, ENTER, CTRL_C, CTRL_Q = "\x1b[B", "\x1b[A", "\r", "\x03", "\x11"


def _checklist(keys, typed):
    keys(typed)
    try:
        add, revoke = picker.pick_collections(select_all=False, interactive=True)
    except EOFError:
        pytest.fail(f"checklist still open when the keys {typed!r} ran out")
    return sorted(add), sorted(revoke)


# Rows: Alpha, Beta (authorized, starts checked), Gamma.
@pytest.mark.parametrize(
    "typed, expected",
    [
        pytest.param(ENTER, ([], []), id="enter-no-change"),
        pytest.param(" " + ENTER, ([ALPHA], []), id="check-first"),
        pytest.param(DOWN + DOWN + " " + ENTER, ([GAMMA], []), id="check-third"),
        pytest.param(DOWN + DOWN + UP + " " + ENTER + "y", ([], [BETA]), id="up-uncheck-confirm"),
        pytest.param(DOWN + " " + ENTER + "y", ([], [BETA]), id="uncheck-yes-revokes"),
        pytest.param(DOWN + " " + ENTER + "n", ([], []), id="uncheck-no-keeps"),
        pytest.param(DOWN + " " + ENTER + ENTER, ([], []), id="uncheck-enter-keeps"),
        pytest.param(DOWN + " " + ENTER + CTRL_C, ([], []), id="uncheck-ctrl-c-keeps"),
        pytest.param(" " + DOWN + " " + ENTER + "y", ([ALPHA], [BETA]), id="add-and-revoke"),
        pytest.param(" q", ([], []), id="q-cancels"),
        pytest.param(DOWN + " q", ([], []), id="q-discards-revoke"),
        pytest.param(" " + CTRL_C, ([], []), id="ctrl-c-cancels"),
        pytest.param(" " + CTRL_Q, ([], []), id="ctrl-q-cancels"),
        pytest.param("a" + ENTER, ([ALPHA, GAMMA], []), id="a-checks-all"),
        pytest.param("aa" + ENTER + "y", ([], [BETA]), id="a-twice-unchecks-all"),
        pytest.param("i" + ENTER + "y", ([ALPHA, GAMMA], [BETA]), id="i-inverts"),
        pytest.param("i" + ENTER + "n", ([ALPHA, GAMMA], []), id="i-inverts-keep"),
    ],
)
def test_checklist_keys(fake_auth, console, keys, typed, expected):
    fake_auth(authorized={BETA})
    console()
    assert _checklist(keys, typed) == expected


def test_checklist_starts_with_authorized_checked(fake_auth, console, keys):
    fake_auth(authorized={BETA, GAMMA})
    console()
    _checklist(keys, ENTER)
    (_, kwargs), = keys.prompts["checkbox"]
    assert [(c.value, c.checked) for c in kwargs["choices"]] == [(ALPHA, False), (BETA, True), (GAMMA, True)]


def test_checklist_revoke_confirmation(fake_auth, console, keys):
    fake_auth(authorized={ALPHA, BETA})
    out = console()
    _checklist(keys, "aa" + ENTER + "n")
    (args, kwargs), = keys.prompts["confirm"]
    assert args[0] == "Revoke access to 2 collection(s)?"
    assert kwargs.get("default") is False
    assert "Alpha Archive" in out.getvalue() and "Beta Results" in out.getvalue()
    assert "Keeping access" in out.getvalue()


def test_checklist_no_confirmation_without_revokes(fake_auth, console, keys):
    fake_auth(authorized={BETA})
    console()
    _checklist(keys, " " + ENTER)
    assert keys.prompts["confirm"] == []


def test_checklist_rows_are_styled_fragments(fake_auth, console, keys):
    fake_auth()
    console()
    _checklist(keys, ENTER)
    (_, kwargs), = keys.prompts["checkbox"]
    for choice in kwargs["choices"]:
        assert isinstance(choice.title, list)
        assert all(isinstance(style, str) and isinstance(text, str) for style, text in choice.title)
    alpha = kwargs["choices"][0]
    assert "Alpha Archive" in "".join(text for _, text in alpha.title)
    assert ALPHA in alpha.description and "owner alice" in alpha.description


def test_checklist_has_no_search_filter(fake_auth, console, keys):
    fake_auth()
    console()
    _checklist(keys, ENTER)
    (_, kwargs), = keys.prompts["checkbox"]
    assert not kwargs.get("use_search_filter")


def test_checklist_selected_style_is_not_reverse(fake_auth, console, keys):
    fake_auth()
    console()
    _checklist(keys, ENTER)
    question = keys.prompts["questions"][0]
    attrs = question.application._merged_style.get_attrs_for_style_str("class:selected")
    assert attrs.reverse is False


# --- CLI: login ----------------------------------------------------------------


def test_login_new_user_logs_in_twice(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=False)
    out = run_cli(monkeypatch, capsys, ["login"], "1\n")
    assert fake.logins == [([], False), ([ALPHA], False)]
    assert f"authorized: {ALPHA} Alpha Archive" in out


def test_login_new_user_picking_nothing_logs_in_once(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=False)
    run_cli(monkeypatch, capsys, ["login"], "\n")
    assert fake.logins == [([], False)]


def test_login_returning_user_only_new_picks_log_in(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True, authorized={BETA})
    out = run_cli(monkeypatch, capsys, ["login"], "1,2\n")
    assert fake.logins == [([ALPHA], False)]
    assert f"already authorized: {BETA} Beta Results" in out
    assert f"authorized: {ALPHA} Alpha Archive" in out
    assert f"already authorized: {ALPHA}" not in out


def test_login_returning_user_nothing_new_no_login(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True, authorized={BETA})
    out = run_cli(monkeypatch, capsys, ["login"], "2\n")
    assert fake.logins == []
    assert f"already authorized: {BETA} Beta Results" in out


def test_login_with_ids_skips_list(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=False)
    out = run_cli(monkeypatch, capsys, ["login", ALPHA, BETA])
    assert fake.logins == [([ALPHA, BETA], False)]
    assert fake.discovered == 0
    assert f"authorized: {ALPHA}" in out and f"authorized: {BETA}" in out


def test_login_with_authorized_ids_still_logs_in_for_transfer(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=False, authorized={ALPHA})
    out = run_cli(monkeypatch, capsys, ["login", ALPHA])
    assert fake.logins == [([], False)]
    assert f"already authorized: {ALPHA}" in out


def test_login_with_authorized_ids_and_transfer_no_login(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True, authorized={ALPHA})
    run_cli(monkeypatch, capsys, ["login", ALPHA])
    assert fake.logins == []


def test_login_all(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True, authorized={BETA})
    out = run_cli(monkeypatch, capsys, ["login", "--all"])  # empty stdin: must not prompt
    assert fake.logins == [([ALPHA, GAMMA], False)]
    assert f"already authorized: {BETA}" in out
    assert f"authorized: {GAMMA} Gamma Store" in out


def test_login_force_with_ids(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True, authorized={BETA})
    out = run_cli(monkeypatch, capsys, ["login", "--force", BETA])
    assert fake.logins == [([BETA], True)]
    assert f"authorized: {BETA}" in out and "already authorized" not in out


def test_login_force_without_picks(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True)
    run_cli(monkeypatch, capsys, ["login", "--force"], "\n")
    assert fake.logins == [([], True)]


def test_login_client_id(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True)
    run_cli(monkeypatch, capsys, ["login", "--client-id", "my-client", ALPHA])
    assert fake.client_ids == ["my-client"]


def test_login_without_client_id_leaves_it(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True)
    run_cli(monkeypatch, capsys, ["login", ALPHA])
    assert fake.client_ids == []


@pytest.mark.parametrize(
    "stdin_tty, stdout_tty, flag, interactive",
    [
        (True, True, False, True),
        (True, True, True, False),
        (False, True, False, False),
        (True, False, False, False),
        (False, False, False, False),
    ],
)
def test_login_interactive_only_with_terminals(fake_auth, monkeypatch, stdin_tty, stdout_tty, flag, interactive):
    fake_auth(transfer=True)
    calls = []
    monkeypatch.setattr(picker, "pick_collections", lambda *args: calls.append(args) or ([], []))
    monkeypatch.setattr(sys, "argv", ["globus-xr", "login", *(["--no-interactive"] if flag else [])])
    monkeypatch.setattr(sys, "stdin", FakeTTY() if stdin_tty else io.StringIO())
    monkeypatch.setattr(sys, "stdout", FakeTTY() if stdout_tty else io.StringIO())
    cli.main()
    assert calls == [(False, interactive)]


def test_login_checklist_revokes_unchecked(fake_auth, keys, monkeypatch, capsys):
    fake = fake_auth(transfer=True, authorized={BETA})
    keys(DOWN + " " + ENTER + "y")
    out = run_cli(monkeypatch, capsys, ["login"], tty=True)
    assert fake.revoked == [BETA]
    assert fake.logins == []
    assert f"revoked: {BETA} Beta Results" in out


def test_login_checklist_adds_and_revokes(fake_auth, keys, monkeypatch, capsys):
    fake = fake_auth(transfer=True, authorized={BETA})
    keys(" " + DOWN + " " + ENTER + "y")
    out = run_cli(monkeypatch, capsys, ["login"], tty=True)
    assert fake.revoked == [BETA]
    assert fake.logins == [([ALPHA], False)]
    assert f"authorized: {ALPHA} Alpha Archive" in out


def test_login_checklist_quit_changes_nothing(fake_auth, keys, monkeypatch, capsys):
    fake = fake_auth(transfer=True, authorized={BETA})
    keys(" " + DOWN + " q")
    run_cli(monkeypatch, capsys, ["login"], tty=True)
    assert fake.revoked == [] and fake.logins == []
    assert fake.authorized == {BETA}


def test_no_interactive_flag_uses_typed_answer_on_a_terminal(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True)
    monkeypatch.setattr(picker.questionary, "checkbox", lambda *a, **k: pytest.fail("checklist shown"))
    run_cli(monkeypatch, capsys, ["login", "--no-interactive"], "3\n", tty=True)
    assert fake.logins == [([GAMMA], False)]


# --- CLI: logout and ls --------------------------------------------------------


def test_logout_everything(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True, authorized={ALPHA})
    out = run_cli(monkeypatch, capsys, ["logout"])
    assert fake.logouts == 1 and fake.revoked == []
    assert "Logged out" in out


def test_logout_ids(fake_auth, monkeypatch, capsys):
    fake = fake_auth(transfer=True, authorized={BETA, GAMMA})
    out = run_cli(monkeypatch, capsys, ["logout", ALPHA, BETA])
    assert fake.logouts == 0
    assert fake.revoked == [BETA] and fake.authorized == {GAMMA}
    assert out.splitlines() == [f"not authorized: {ALPHA}", f"revoked: {BETA}"]


def test_ls(monkeypatch, capsys):
    calls = []

    class FakeFS:
        def ls(self, url, detail=False):
            calls.append((url, detail))
            return [
                {"name": f"{ALPHA}/data/sub", "type": "directory"},
                {"name": f"{ALPHA}/data/file.nc", "type": "file"},
            ]

    monkeypatch.setattr(fs, "GlobusFileSystem", FakeFS)
    out = run_cli(monkeypatch, capsys, ["ls", f"globus://{ALPHA}/data"])
    assert calls == [(f"globus://{ALPHA}/data", True)]
    assert out.splitlines() == ["sub/", "file.nc"]


@pytest.mark.parametrize(
    "argv, setup",
    [
        (["logout"], "auth.logout = lambda: None"),
        (["logout", ALPHA], "auth.revoke = lambda ids: []"),
        (["ls", f"globus://{ALPHA}/"], "fs.GlobusFileSystem = type('F', (), {'ls': lambda self, url, detail: []})"),
        (
            ["login", ALPHA],
            "auth.login = lambda *a, **k: []; auth.is_authorized = lambda c: False; "
            "auth.has_transfer_token = lambda: True; auth.collection_name = lambda c: ''",
        ),
    ],
)
def test_ui_libraries_imported_lazily(argv, setup):
    # A fresh interpreter, since this one has already imported the picker.
    code = textwrap.dedent(
        f"""
        import sys
        from globus_xr import auth, cli, fs
        {setup}
        sys.argv = ["globus-xr", *{argv!r}]
        cli.main()
        loaded = [m for m in ("globus_xr.picker", "questionary", "prompt_toolkit") if m in sys.modules]
        assert not loaded, loaded
        """
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
