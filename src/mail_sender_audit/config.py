"""Static configuration: OAuth scopes and on-disk locations."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from platformdirs import user_config_path, user_data_path

APP_NAME: Final = "mail-sender-audit"

#: Minimum scopes for the feature set.
#:
#: * ``gmail.readonly`` - list messages and read headers/bodies to find
#:   ``List-Unsubscribe`` data. Cannot modify, delete or send anything.
#: * ``gmail.send``     - send the ``mailto:`` unsubscribe requests. Cannot read mail.
#:
#: ``gmail.modify`` would also work but grants far more than we need (labelling,
#: trashing, ...), so we deliberately do not request it.
SCOPES: Final[tuple[str, ...]] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Where the OAuth client file, Gmail tokens and the account list live."""

    client_secrets: Path
    token_file: Path
    accounts_file: Path | None = None

    @property
    def accounts_path(self) -> Path:
        return self.accounts_file or user_config_path(APP_NAME) / "accounts.json"

    def for_account(self, email: str) -> AppPaths:
        """Paths for one Gmail account: the same client file, but its own token file."""
        slug = re.sub(r"[^a-z0-9._-]+", "-", email.lower())
        return replace(self, token_file=self.token_file.with_name(f"token-{slug}.json"))

    @classmethod
    def default(
        cls,
        *,
        client_secrets: Path | None = None,
        token_file: Path | None = None,
        accounts_file: Path | None = None,
    ) -> AppPaths:
        """Resolve per-OS defaults, honouring explicit overrides.

        Defaults (Linux): ``~/.config/mail-sender-audit/credentials.json`` and
        ``~/.local/share/mail-sender-audit/token.json``. platformdirs picks the
        equivalent locations on macOS and Windows.
        """
        return cls(
            client_secrets=(client_secrets or user_config_path(APP_NAME) / "credentials.json")
            .expanduser()
            .resolve(),
            token_file=(token_file or user_data_path(APP_NAME) / "token.json")
            .expanduser()
            .resolve(),
            accounts_file=accounts_file.expanduser().resolve() if accounts_file else None,
        )
