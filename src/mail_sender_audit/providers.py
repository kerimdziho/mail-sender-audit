"""Known mail providers: detect them from an address and supply server settings.

Settings for iCloud and Yahoo were checked against the providers' own
documentation; Fastmail and AOL are their long-standing public settings.
Any other provider can be added with custom server details.

Pure data and string handling, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from mail_sender_audit.imap import ImapSettings, Security, SmtpSettings

_GMAIL_DOMAINS: Final = frozenset({"gmail.com", "googlemail.com"})
# outlook.com, hotmail.fr, live.com, hotmail.co.uk ... but not live.example.com
_MICROSOFT_RE: Final = re.compile(r"^(outlook|hotmail|live)\.[a-z]{2,3}(\.[a-z]{2})?$")
_MICROSOFT_DOMAINS: Final = frozenset({"msn.com", "passport.com"})
_YAHOO_RE: Final = re.compile(
    r"^yahoo\.[a-z]{2,3}(\.[a-z]{2})?$"
)  # yahoo.com, yahoo.co.uk, yahoo.fr


def domain_of(address: str) -> str:
    return address.rpartition("@")[2].strip().lower()


def is_gmail(address: str) -> bool:
    return domain_of(address) in _GMAIL_DOMAINS


def is_microsoft_personal(address: str) -> bool:
    """Outlook.com, Hotmail, Live and MSN addresses (OAuth-only for third-party apps)."""
    domain = domain_of(address)
    return domain in _MICROSOFT_DOMAINS or _MICROSOFT_RE.match(domain) is not None


@dataclass(frozen=True, slots=True)
class Preset:
    key: str
    display_name: str
    imap_host: str
    smtp_host: str
    smtp_port: int
    smtp_security: Security
    domains: tuple[str, ...]
    #: Some providers log in with the part before the ``@`` (tried first) or the full address.
    imap_local_part_first: bool = False
    app_password_help: str = ""

    def imap_settings(self, address: str) -> list[ImapSettings]:
        """Candidate IMAP logins, most likely first."""
        names = [address]
        if self.imap_local_part_first:
            names.insert(0, address.partition("@")[0])
        return [ImapSettings(host=self.imap_host, username=name) for name in names]

    def smtp_settings(self, address: str) -> SmtpSettings:
        return SmtpSettings(
            host=self.smtp_host,
            username=address,
            port=self.smtp_port,
            security=self.smtp_security,
        )


PRESETS: Final[tuple[Preset, ...]] = (
    Preset(
        key="yahoo",
        display_name="Yahoo Mail",
        imap_host="imap.mail.yahoo.com",
        smtp_host="smtp.mail.yahoo.com",
        smtp_port=465,
        smtp_security=Security.SSL,
        domains=("yahoo.com", "ymail.com", "rocketmail.com"),
        app_password_help=(
            "Yahoo needs an app password: open your Yahoo account security settings and "
            "generate one for 'Other app'."
        ),
    ),
    Preset(
        key="icloud",
        display_name="iCloud Mail",
        imap_host="imap.mail.me.com",
        smtp_host="smtp.mail.me.com",
        smtp_port=587,
        smtp_security=Security.STARTTLS,
        domains=("icloud.com", "me.com", "mac.com"),
        imap_local_part_first=True,
        app_password_help=(
            "iCloud needs an app-specific password: sign in to your Apple Account, open "
            "Sign-In and Security, and create one under App-Specific Passwords."
        ),
    ),
    Preset(
        key="fastmail",
        display_name="Fastmail",
        imap_host="imap.fastmail.com",
        smtp_host="smtp.fastmail.com",
        smtp_port=465,
        smtp_security=Security.SSL,
        domains=("fastmail.com", "fastmail.fm"),
        app_password_help=(
            "Fastmail needs an app password with IMAP and SMTP access: create one in "
            "Settings, under Privacy & Security."
        ),
    ),
    Preset(
        key="aol",
        display_name="AOL Mail",
        imap_host="imap.aol.com",
        smtp_host="smtp.aol.com",
        smtp_port=465,
        smtp_security=Security.SSL,
        domains=("aol.com",),
        app_password_help=(
            "AOL needs an app password: generate one in your AOL account security settings."
        ),
    ),
)


def preset_for(address: str) -> Preset | None:
    """The built-in preset for ``address``'s domain, if any."""
    domain = domain_of(address)
    for preset in PRESETS:
        if domain in preset.domains or (preset.key == "yahoo" and _YAHOO_RE.match(domain)):
            return preset
    return None
