"""Tests for mail_sender_audit.accounts and mail_sender_audit.providers."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from mail_sender_audit.accounts import Account, AccountKind, AccountStore, select_account
from mail_sender_audit.errors import AccountsFileError, UsageError
from mail_sender_audit.imap import ImapSettings, Security, SmtpSettings
from mail_sender_audit.providers import is_gmail, is_microsoft_personal, preset_for

GMAIL = Account(email="me@gmail.com", kind=AccountKind.GMAIL)
YAHOO = Account(
    email="me@yahoo.com",
    kind=AccountKind.IMAP,
    imap=ImapSettings(host="imap.mail.yahoo.com", username="me@yahoo.com"),
    smtp=SmtpSettings(
        host="smtp.mail.yahoo.com", username="me@yahoo.com", port=465, security=Security.SSL
    ),
)


@pytest.fixture
def store(tmp_path: Path) -> AccountStore:
    return AccountStore(tmp_path / "config" / "accounts.json")


# ------------------------------------------------------------------------ store


def test_accounts_round_trip_and_the_file_holds_no_secrets(store: AccountStore) -> None:
    store.add(GMAIL)
    store.add(YAHOO)

    assert store.load() == [GMAIL, YAHOO]
    text = store.path.read_text(encoding="utf-8")
    assert "password" not in text.lower()
    assert json.loads(text)["version"] == 1
    if sys.platform != "win32":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_missing_file_means_no_accounts(store: AccountStore) -> None:
    assert store.load() == []


def test_adding_the_same_address_twice_is_refused(store: AccountStore) -> None:
    store.add(YAHOO)

    with pytest.raises(UsageError, match="already connected"):
        store.add(YAHOO)


def test_remove_returns_the_account_and_persists(store: AccountStore) -> None:
    store.add(GMAIL)
    store.add(YAHOO)

    assert store.remove("ME@yahoo.com") == YAHOO
    assert store.load() == [GMAIL]
    with pytest.raises(UsageError):
        store.remove("nobody@example.com")


def test_corrupt_file_has_a_clear_error(store: AccountStore) -> None:
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{broken", encoding="utf-8")

    with pytest.raises(AccountsFileError) as excinfo:
        store.load()

    assert excinfo.value.hint is not None


def test_imap_accounts_require_server_settings() -> None:
    with pytest.raises(ValueError, match="IMAP"):
        Account(email="a@example.com", kind=AccountKind.IMAP)


# -------------------------------------------------------------------- selection


def test_no_accounts_points_to_the_add_command() -> None:
    with pytest.raises(UsageError) as excinfo:
        select_account([], None)

    assert "accounts add" in (excinfo.value.hint or "")


def test_single_account_is_used_automatically() -> None:
    assert select_account([YAHOO], None) == YAHOO


def test_selector_matches_exactly_or_by_unique_fragment() -> None:
    accounts = [GMAIL, YAHOO]

    assert select_account(accounts, "me@gmail.com") == GMAIL
    assert select_account(accounts, "YAHOO") == YAHOO


def test_ambiguous_or_unknown_selectors_are_explained() -> None:
    accounts = [GMAIL, YAHOO]

    with pytest.raises(UsageError, match="more than one"):
        select_account(accounts, "me@")
    with pytest.raises(UsageError, match="No connected account matches"):
        select_account(accounts, "icloud")


def test_several_accounts_need_a_choice() -> None:
    with pytest.raises(UsageError, match="More than one"):
        select_account([GMAIL, YAHOO], None)

    assert select_account([GMAIL, YAHOO], None, choose=lambda accounts: accounts[1]) == YAHOO


# -------------------------------------------------------------------- providers


@pytest.mark.parametrize("address", ["a@gmail.com", "a@GoogleMail.com"])
def test_gmail_addresses_are_detected(address: str) -> None:
    assert is_gmail(address)


@pytest.mark.parametrize(
    "address",
    [
        "a@outlook.com",
        "a@hotmail.com",
        "a@hotmail.co.uk",
        "a@live.fr",
        "a@msn.com",
        "a@outlook.com.au",
    ],
)
def test_microsoft_personal_addresses_are_detected(address: str) -> None:
    assert is_microsoft_personal(address)


@pytest.mark.parametrize(
    "address", ["a@gmail.com", "a@live.example.com", "a@outlook-fan.org", "a@yahoo.com"]
)
def test_other_addresses_are_not_mistaken_for_microsoft(address: str) -> None:
    assert not is_microsoft_personal(address)


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("a@yahoo.com", "yahoo"),
        ("a@yahoo.co.uk", "yahoo"),
        ("a@ymail.com", "yahoo"),
        ("a@icloud.com", "icloud"),
        ("a@me.com", "icloud"),
        ("a@fastmail.com", "fastmail"),
        ("a@aol.com", "aol"),
    ],
)
def test_presets_are_detected_from_the_domain(address: str, key: str) -> None:
    preset = preset_for(address)

    assert preset is not None
    assert preset.key == key


def test_unknown_domains_have_no_preset() -> None:
    assert preset_for("a@example.org") is None
    assert preset_for("a@yahoo.example.com") is None


def test_icloud_tries_the_short_username_before_the_full_address() -> None:
    preset = preset_for("john@icloud.com")
    assert preset is not None

    logins = preset.imap_settings("john@icloud.com")
    smtp = preset.smtp_settings("john@icloud.com")

    assert [s.username for s in logins] == ["john", "john@icloud.com"]
    assert logins[0].host == "imap.mail.me.com"
    assert (logins[0].port, logins[0].security) == (993, Security.SSL)
    assert (smtp.host, smtp.port, smtp.security) == ("smtp.mail.me.com", 587, Security.STARTTLS)
    assert smtp.username == "john@icloud.com"


def test_yahoo_uses_the_full_address_and_implicit_tls() -> None:
    preset = preset_for("me@yahoo.com")
    assert preset is not None

    [login] = preset.imap_settings("me@yahoo.com")
    smtp = preset.smtp_settings("me@yahoo.com")

    assert login.username == "me@yahoo.com"
    assert login.host == "imap.mail.yahoo.com"
    assert (smtp.host, smtp.port, smtp.security) == ("smtp.mail.yahoo.com", 465, Security.SSL)
