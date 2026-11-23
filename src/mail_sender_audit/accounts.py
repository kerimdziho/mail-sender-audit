"""The list of connected mail accounts, saved as JSON next to the other config.

Only non-secret facts are stored (address, kind, server names). Passwords live
in the operating system's keyring (see ``secrets_store``), Gmail tokens in
their own private files.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from mail_sender_audit.errors import AccountsFileError, UsageError
from mail_sender_audit.fsutil import write_private_text
from mail_sender_audit.imap import ImapSettings, Security, SmtpSettings

_FORMAT_VERSION: Final = 1


class AccountKind(StrEnum):
    GMAIL = "gmail"  # Gmail API with OAuth sign-in
    IMAP = "imap"  # IMAP + SMTP with an (app) password


@dataclass(frozen=True, slots=True)
class Account:
    email: str  # lower-case; also the unique id
    kind: AccountKind
    imap: ImapSettings | None = None
    smtp: SmtpSettings | None = None

    def __post_init__(self) -> None:
        if self.kind is AccountKind.IMAP and (self.imap is None or self.smtp is None):
            raise ValueError("IMAP accounts need IMAP and SMTP settings")

    @property
    def description(self) -> str:
        if self.kind is AccountKind.GMAIL:
            return "Gmail (Google sign-in)"
        assert self.imap is not None
        return f"IMAP ({self.imap.host})"

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"email": self.email, "kind": self.kind.value}
        if self.imap is not None:
            data["imap"] = {
                "host": self.imap.host,
                "port": self.imap.port,
                "security": self.imap.security.value,
                "username": self.imap.username,
            }
        if self.smtp is not None:
            data["smtp"] = {
                "host": self.smtp.host,
                "port": self.smtp.port,
                "security": self.smtp.security.value,
                "username": self.smtp.username,
            }
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Account:
        imap = data.get("imap")
        smtp = data.get("smtp")
        return cls(
            email=str(data["email"]).lower(),
            kind=AccountKind(data["kind"]),
            imap=(
                ImapSettings(
                    host=str(imap["host"]),
                    username=str(imap["username"]),
                    port=int(imap["port"]),
                    security=Security(imap["security"]),
                )
                if imap
                else None
            ),
            smtp=(
                SmtpSettings(
                    host=str(smtp["host"]),
                    username=str(smtp["username"]),
                    port=int(smtp["port"]),
                    security=Security(smtp["security"]),
                )
                if smtp
                else None
            ),
        )


class AccountStore:
    """Reads and writes the accounts file. Methods re-read the file, so they are always current."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[Account]:
        if not self._path.is_file():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            return [Account.from_json(item) for item in payload["accounts"]]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AccountsFileError(
                f"Could not read the account list {self._path}: {exc}",
                hint="Fix or delete that file, then add your accounts again.",
            ) from exc

    def add(self, account: Account) -> None:
        accounts = self.load()
        if any(existing.email == account.email for existing in accounts):
            raise UsageError(
                f"{account.email} is already connected.",
                hint=f"Remove it first with: mail-sender-audit accounts remove {account.email}",
            )
        self._save([*accounts, account])

    def remove(self, email: str) -> Account:
        accounts = self.load()
        removed = next((a for a in accounts if a.email == email.lower()), None)
        if removed is None:
            raise UsageError(f"No connected account named {email}.")
        self._save([a for a in accounts if a is not removed])
        return removed

    def _save(self, accounts: Sequence[Account]) -> None:
        payload = {"version": _FORMAT_VERSION, "accounts": [a.to_json() for a in accounts]}
        try:
            write_private_text(self._path, json.dumps(payload, indent=2) + "\n")
        except OSError as exc:
            raise AccountsFileError(f"Could not save the account list {self._path}: {exc}") from exc


def select_account(
    accounts: Sequence[Account],
    selector: str | None,
    *,
    choose: Callable[[Sequence[Account]], Account] | None = None,
) -> Account:
    """Pick the account to work on.

    * ``selector`` given: an exact address, or any text that matches exactly one account.
    * only one account: that one.
    * several accounts: ask via ``choose`` if possible, otherwise explain what to do.
    """
    if not accounts:
        raise UsageError(
            "No mail account is connected yet.",
            hint="Connect one with: mail-sender-audit accounts add",
        )

    if selector:
        wanted = selector.strip().lower()
        exact = [a for a in accounts if a.email == wanted]
        matches = exact or [a for a in accounts if wanted in a.email]
        if len(matches) == 1:
            return matches[0]
        names = ", ".join(a.email for a in accounts)
        if not matches:
            raise UsageError(
                f"No connected account matches '{selector}'.", hint=f"Connected: {names}"
            )
        raise UsageError(
            f"'{selector}' matches more than one account.",
            hint="Use the full address: " + ", ".join(a.email for a in matches),
        )

    if len(accounts) == 1:
        return accounts[0]
    if choose is None:
        raise UsageError(
            "More than one account is connected.",
            hint="Choose one with --account ADDRESS. Connected: "
            + ", ".join(a.email for a in accounts),
        )
    return choose(accounts)
