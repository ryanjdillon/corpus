"""Which folders the IMAP fetcher scans: the folder policy, with the connection injected."""

from __future__ import annotations

import json
import logging
from unittest.mock import create_autospec

import msgspec
import pytest
from imapclient import IMAPClient

from corpus.fetchers.imap import EXCLUDED_FOLDER_NAMES, FolderPolicy, ImapFetcher
from corpus.sources import ImapOptions, ImapSource

#: A listing like a SPECIAL-USE server's, with a junk folder under a local name.
LISTING = [
    ((b"\\HasNoChildren",), b"/", "INBOX"),
    ((b"\\HasNoChildren", b"\\Sent"), b"/", "Sent"),
    ((b"\\HasNoChildren",), b"/", "Sent Items"),
    ((b"\\HasNoChildren", b"\\Junk"), b"/", "Quarantine"),
    ((b"\\HasChildren", b"\\Trash"), b"/", "Bin"),
    ((b"\\HasNoChildren",), b"/", "Bin/Old project"),
    ((b"\\HasNoChildren", b"\\drafts"), b"/", "Unsent"),
    ((b"\\HasNoChildren",), b"/", "Junk Mail"),
    ((b"\\HasNoChildren",), b"/", "Archive/spam"),
    ((b"\\HasNoChildren",), b"/", "Spam reports"),
    ((b"\\Noselect", b"\\HasChildren"), b"/", "Archive"),
    ((b"\\HasNoChildren",), b"/", "Archive/2025"),
]


def test_a_special_use_server_is_judged_by_its_flags_alone():
    scan, skipped = FolderPolicy.of(None).resolve(LISTING)

    # The flags decide, so an unflagged Junk Mail or Archive/spam is kept.
    assert scan == [
        "INBOX", "Sent", "Sent Items", "Junk Mail", "Archive/spam", "Spam reports",
        "Archive/2025",
    ]
    assert skipped == ["Quarantine", "Bin", "Bin/Old project", "Unsent"]


def test_an_explicit_selection_is_fetched_as_given():
    policy = FolderPolicy.of(["INBOX", "Junk Mail"])

    assert policy.resolve(LISTING) == (["INBOX", "Junk Mail"], [])


def test_exclude_folders_adds_whole_names_and_their_descendants_to_the_defaults():
    scan, skipped = FolderPolicy.of(None, ["archive", "Spam Reports", "spam"]).resolve(LISTING)

    # "spam" names no whole folder, so Archive/spam stays; Archive takes its children.
    assert scan == ["INBOX", "Sent", "Sent Items", "Junk Mail"]
    assert skipped == [
        "Quarantine", "Bin", "Bin/Old project", "Unsent", "Archive/spam", "Spam reports",
        "Archive/2025",
    ]


@pytest.mark.parametrize("name", sorted(EXCLUDED_FOLDER_NAMES))
def test_each_default_name_is_skipped_whatever_its_case(name):
    listing = [((), b".", f"INBOX.{name.upper()}"), ((), b".", name.title())]

    assert FolderPolicy.of(None).resolve(listing) == (
        [], [f"INBOX.{name.upper()}", name.title()],
    )


def test_a_flat_namespace_matches_the_whole_name():
    listing = [((), None, "Trash"), ((), None, "Trash/kept")]

    assert FolderPolicy.of(None).resolve(listing) == (["Trash/kept"], ["Trash"])


def test_stored_folders_are_judged_by_the_current_listing():
    stored = ["INBOX", "Sent", "Quarantine", "Junk Mail", "Gone/Trash", "Bin/Gone", "Trash"]

    # Gone from the listing, Bin/Gone is still beneath a \Trash folder; on a
    # special-use server an unlisted Trash carries no flag, so it is kept.
    assert FolderPolicy.of(None).excluded(stored, LISTING) == {"Quarantine", "Bin/Gone"}


def test_a_selected_folder_is_never_excluded_and_an_unselected_one_only_when_junk():
    policy = FolderPolicy.of(["INBOX", "Junk Mail"])

    assert policy.excluded(["INBOX", "Junk Mail", "Quarantine", "Archive/2025"], LISTING) == {
        "Quarantine",
    }


def test_without_a_listing_a_stored_folder_is_judged_by_its_whole_name():
    stored = ["Spam", "Lists/Spam", "Old/Spam", "INBOX/Junk"]

    # No delimiter is known, so no name splits into a parent and a child.
    assert FolderPolicy.of(None).excluded(stored, []) == {"Spam"}


# --------------------------------------------------------------------------- #
# the rule, and the purge agreeing with the fetcher
# --------------------------------------------------------------------------- #
def _entry(name: str, *flags: bytes, delimiter: bytes | None = b"/"):
    return ((b"\\HasNoChildren", *flags), delimiter, name)


#: A server that advertises special-use attributes, "/" as its delimiter.
SPECIAL_SLASH = [
    _entry("INBOX"),
    _entry("Sent", b"\\Sent"),
    _entry("Trash", b"\\Sent"),
    _entry("Archive", b"\\Archive"),
    _entry("Quarantine", b"\\Junk"),
    _entry("Quarantine/Old"),
    _entry("Bin", b"\\Trash"),
    _entry("Bin/Old project"),
    _entry("Bin/Old project/Notes"),
    _entry("Bin/Sent copy", b"\\Sent"),
    _entry("Unsent", b"\\Drafts"),
    _entry("INBOX/Junk"),
    _entry("INBOX/Clients"),
    _entry("INBOX/Clients/Drafts"),
    _entry("Work/Trash"),
    _entry("Junk Mail"),
    ((b"\\Noselect", b"\\HasChildren"), b"/", "Work"),
]

#: The same server with "." as its delimiter and lower-case attributes.
SPECIAL_DOT = [
    _entry("INBOX", delimiter=b"."),
    _entry("INBOX.Sent", b"\\sent", delimiter=b"."),
    _entry("INBOX.Spam", b"\\junk", delimiter=b"."),
    _entry("INBOX.Spam.Sub", delimiter=b"."),
    _entry("INBOX.Trash", delimiter=b"."),
    _entry("INBOX.Clients.Drafts", delimiter=b"."),
]

#: A server without special-use attributes, "/" as its delimiter.
NAMES_SLASH = [
    _entry("INBOX"),
    _entry("Sent"),
    _entry("Sent Items"),
    _entry("INBOX/Junk"),
    _entry("inbox/spam"),
    _entry("INBOX/Clients"),
    _entry("INBOX/Clients/Drafts"),
    _entry("Work/Trash"),
    _entry("Archive/Spam"),
    _entry("Junk Mail"),
    _entry("Spam reports"),
    _entry("Trash"),
    _entry("Trash/Old project"),
    _entry("Trash/Old project/Notes"),
    _entry("Drafts"),
    ((b"\\Noselect", b"\\HasChildren"), b"/", "Work"),
]

#: A server without special-use attributes, "." as its delimiter.
NAMES_DOT = [
    _entry("INBOX", delimiter=b"."),
    _entry("INBOX.Spam", delimiter=b"."),
    _entry("INBOX.Spam.Sub", delimiter=b"."),
    _entry("Inbox.Utkast", delimiter=b"."),
    _entry("INBOX.Clients.Drafts", delimiter=b"."),
    _entry("Deleted Items", delimiter=b"."),
    _entry("Work.Trash", delimiter=b"."),
    _entry("Søppelpost", delimiter=b"."),
]

#: A flat namespace: no delimiter, so every name is top-level.
FLAT = [
    _entry("INBOX", delimiter=None),
    _entry("Spam", delimiter=None),
    _entry("INBOX/Spam", delimiter=None),
]

RULE = [
    pytest.param(
        SPECIAL_SLASH, (),
        {"Quarantine", "Quarantine/Old", "Bin", "Bin/Old project", "Bin/Old project/Notes",
         "Unsent"},
        id="special-use-slash",
    ),
    pytest.param(
        SPECIAL_SLASH, ("work", "INBOX/Clients", "Sent"),
        {"Quarantine", "Quarantine/Old", "Bin", "Bin/Old project", "Bin/Old project/Notes",
         "Unsent", "Work/Trash", "INBOX/Clients", "INBOX/Clients/Drafts", "Sent"},
        id="special-use-slash-user-excludes",
    ),
    pytest.param(SPECIAL_DOT, (), {"INBOX.Spam", "INBOX.Spam.Sub"}, id="special-use-dot"),
    pytest.param(
        NAMES_SLASH, (),
        {"INBOX/Junk", "inbox/spam", "Junk Mail", "Trash", "Trash/Old project",
         "Trash/Old project/Notes", "Drafts"},
        id="names-slash",
    ),
    pytest.param(
        NAMES_SLASH, ("clients", "Archive/spam", "sent items"),
        {"INBOX/Junk", "inbox/spam", "Junk Mail", "Trash", "Trash/Old project",
         "Trash/Old project/Notes", "Drafts", "Archive/Spam", "Sent Items"},
        id="names-slash-user-excludes",
    ),
    pytest.param(
        NAMES_DOT, (),
        {"INBOX.Spam", "INBOX.Spam.Sub", "Inbox.Utkast", "Deleted Items", "Søppelpost"},
        id="names-dot",
    ),
    pytest.param(FLAT, (), {"Spam"}, id="flat"),
]


@pytest.mark.parametrize(("listing", "exclude", "skipped"), RULE)
def test_the_rule_skips_excluded_roots_and_their_descendants(listing, exclude, skipped):
    scan, found = FolderPolicy.of(None, exclude).resolve(listing)

    assert set(found) == skipped
    assert "INBOX" in scan


@pytest.mark.parametrize(("listing", "exclude", "skipped"), RULE)
def test_the_purge_excludes_exactly_the_folders_discovery_skips(listing, exclude, skipped):
    policy = FolderPolicy.of(None, exclude)
    scan, found = policy.resolve(listing)

    for flags, _delimiter, folder in listing:
        if b"\\Noselect" in flags:
            continue
        assert (folder in policy.excluded([folder], listing)) == (folder in found), folder
    assert policy.excluded(scan + found, listing) == set(found)


@pytest.mark.parametrize(
    ("listing", "folder", "kept"),
    [
        pytest.param(SPECIAL_SLASH, "INBOX/Clients/Drafts", True, id="nested-drafts-special"),
        pytest.param(NAMES_SLASH, "INBOX/Clients/Drafts", True, id="nested-drafts-names"),
        pytest.param(SPECIAL_SLASH, "Work/Trash", True, id="nested-trash-special"),
        pytest.param(NAMES_SLASH, "Work/Trash", True, id="nested-trash-names"),
        pytest.param(SPECIAL_SLASH, "Trash", True, id="sent-named-trash"),
        pytest.param(NAMES_SLASH, "INBOX/Junk", False, id="inbox-junk"),
        pytest.param(NAMES_SLASH, "Junk Mail", False, id="top-level-junk-mail"),
        pytest.param(NAMES_SLASH, "Trash/Old project", False, id="beneath-trash"),
        pytest.param(SPECIAL_SLASH, "Bin/Old project", False, id="beneath-flagged-trash"),
    ],
)
def test_regressions(listing, folder, kept):
    policy = FolderPolicy.of(None)
    scan, skipped = policy.resolve(listing)

    assert (folder in scan) is kept
    assert (folder in skipped) is not kept
    assert (folder in policy.excluded([folder], listing)) is not kept


def test_inbox_and_sent_are_never_skipped_by_default():
    listing = [_entry("Inbox"), _entry("Trash", b"\\Trash"), _entry("Trash/Sent", b"\\Sent"),
               _entry("Trash/Mail", b"\\Inbox")]

    assert FolderPolicy.of(None).resolve(listing) == (["Inbox", "Trash/Sent", "Trash/Mail"],
                                                      ["Trash"])


# --------------------------------------------------------------------------- #
# the fetcher
# --------------------------------------------------------------------------- #
ENV = {
    "CORPUS_IMAP_UNIT_HOST": "mail.example.org",
    "CORPUS_IMAP_UNIT_USER": "inbox-a@example.org",
    "CORPUS_IMAP_UNIT_PASSWORD": "x",
}


@pytest.fixture
def client():
    m = create_autospec(IMAPClient, instance=True)
    m.__enter__.return_value = m
    m.list_folders.return_value = LISTING
    m.select_folder.return_value = {b"UIDVALIDITY": 7}
    m.search.return_value = []
    m.fetch.return_value = {}
    return m


def _fetcher(client, options: ImapOptions | None = None, environ=ENV) -> ImapFetcher:
    source = ImapSource(name="unit", enrich=True, options=options or ImapOptions())
    return ImapFetcher(source, environ=environ, connect=lambda *_a, **_k: client)


def _scanned(client) -> list[str]:
    return [c.args[0] for c in client.select_folder.call_args_list]


def test_the_fetcher_scans_only_the_folders_the_policy_keeps(client, caplog):
    fetcher = _fetcher(client)

    with caplog.at_level(logging.INFO, logger="corpus.fetchers.imap"):
        assert list(fetcher.fetch(None)) == []

    assert _scanned(client) == [
        "INBOX", "Sent", "Sent Items", "Junk Mail", "Archive/spam", "Spam reports",
        "Archive/2025",
    ]
    skipped = [r.getMessage() for r in caplog.records if "skipping excluded" in r.getMessage()]
    assert skipped == [
        "imap: imap:unit skipping excluded folders: Quarantine, Bin, Bin/Old project, Unsent"
    ]


def test_an_excluded_folders_cursor_entry_is_kept_but_not_scanned(client):
    cursor = json.dumps({"INBOX": "7:3", "Quarantine": "5:40"})
    fetcher = _fetcher(client)

    list(fetcher.fetch(cursor))

    assert "Quarantine" not in _scanned(client)
    state = json.loads(fetcher.next_cursor() or "{}")
    assert (state["INBOX"], state["Quarantine"]) == ("7:3", "5:40")


def test_explicit_folders_are_scanned_as_given_without_listing(client):
    list(_fetcher(client, ImapOptions(folders=["INBOX", "Junk Mail"])).fetch(None))

    assert _scanned(client) == ["INBOX", "Junk Mail"]
    client.list_folders.assert_not_called()


def test_exclude_folders_from_the_options_reaches_the_policy(client):
    list(_fetcher(client, ImapOptions(exclude_folders=["Sent Items"])).fetch(None))

    assert "Sent Items" not in _scanned(client)
    assert "Sent" in _scanned(client)


def test_exclude_folders_with_an_explicit_folders_variable_warns_and_has_no_effect(client, caplog):
    env = ENV | {"CORPUS_IMAP_UNIT_FOLDERS": "INBOX"}

    with caplog.at_level(logging.WARNING, logger="corpus.fetchers.imap"):
        fetcher = _fetcher(client, ImapOptions(exclude_folders=["INBOX"]), env)

    assert fetcher.policy == FolderPolicy.of(["INBOX"], ["INBOX"])
    assert "exclude_folders has no effect" in caplog.text
    list(fetcher.fetch(None))
    assert _scanned(client) == ["INBOX"]


def test_list_folders_logs_in_and_returns_the_listing(client):
    assert _fetcher(client).list_folders() == LISTING
    client.login.assert_called_once_with("inbox-a@example.org", "x")


def test_folders_and_exclude_folders_together_are_refused():
    with pytest.raises(msgspec.ValidationError, match="not both"):
        msgspec.convert(
            {"folders": ["INBOX"], "exclude_folders": ["Junk"]}, ImapOptions
        )


def test_exclude_folders_must_list_a_name():
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"exclude_folders": []}, ImapOptions)
