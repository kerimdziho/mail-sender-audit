"""Command-line interface (Typer)."""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape

from mail_sender_audit import __version__, auth, interactive
from mail_sender_audit.accounts import Account, AccountKind, AccountStore, select_account
from mail_sender_audit.config import AppPaths
from mail_sender_audit.errors import (
    MailSenderAuditError,
    MailServerError,
    UsageError,
    UserCancelledError,
)
from mail_sender_audit.httpclient import HttpxPoster
from mail_sender_audit.imap import ImapSettings, ImapSource, Security, SmtpSender, SmtpSettings
from mail_sender_audit.models import GroupBy, ScanResult, SenderGroup, Status, UnsubscribeOutcome
from mail_sender_audit.onboarding import (
    SETUP_STEPS,
    find_downloaded_client_secrets,
    install_client_secrets,
)
from mail_sender_audit.providers import domain_of, is_gmail, is_microsoft_personal, preset_for
from mail_sender_audit.scanner import InboxScanner, ScanOptions
from mail_sender_audit.secrets_store import PASSWORD_ENV_VAR, PasswordStore
from mail_sender_audit.session import MailSession, open_session
from mail_sender_audit.ui import (
    RichProgress,
    render_accounts,
    render_outcomes,
    render_scan_table,
)
from mail_sender_audit.unsubscriber import Unsubscriber, plan_actions, summarize_plan

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="mail-sender-audit",
    help="Scan your inbox (Gmail, Yahoo, iCloud, Fastmail, any IMAP provider) and unsubscribe from mailing lists.",
    no_args_is_help=True,
    add_completion=False,
    # Expected failures are rendered by main(); real bugs get a plain traceback.
    pretty_exceptions_enable=False,
)
accounts_app = typer.Typer(
    help="Connect and manage mail accounts.", no_args_is_help=True, add_completion=False
)
app.add_typer(accounts_app, name="accounts")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Options shared by `scan` and `unsubscribe`.
AccountOpt = Annotated[
    str | None,
    typer.Option("--account", "-a", help="Which connected account to use (address or part of it)."),
]
MaxMessagesOpt = Annotated[
    int, typer.Option("--max", "-n", min=1, max=5000, help="Most inbox messages to inspect.")
]
UnreadOpt = Annotated[bool, typer.Option("--unread", help="Only look at unread mail.")]
DaysOpt = Annotated[
    int | None, typer.Option("--days", min=1, help="Only mail newer than this many days.")
]
GroupByOpt = Annotated[
    GroupBy, typer.Option("--by", help="Group by exact sender address or by domain.")
]
NoBodyScanOpt = Annotated[
    bool, typer.Option("--no-body-scan", help="Skip the email-body fallback (faster).")
]
WorkersOpt = Annotated[
    int, typer.Option("--workers", min=1, max=32, help="Concurrent requests while scanning.")
]
RateOpt = Annotated[
    float, typer.Option("--rate", min=0.5, help="Maximum requests per second (Gmail API).")
]


@dataclass(frozen=True, slots=True)
class AppState:
    """Shared, immutable state handed to every subcommand through ``ctx.obj``."""

    paths: AppPaths


def _state(ctx: typer.Context) -> AppState:
    state = ctx.obj
    if not isinstance(state, AppState):  # pragma: no cover - programming error
        raise RuntimeError("CLI state was not initialised")
    return state


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"mail-sender-audit {__version__}")
        raise typer.Exit()


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=err_console, show_path=False, rich_tracebacks=False)],
        force=True,
    )
    # Keep third-party chatter out of the output unless the user asked for it.
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@app.callback()
def root(
    ctx: typer.Context,
    client_secrets: Annotated[
        Path | None,
        typer.Option(
            "--client-secrets",
            envvar="MAIL_SENDER_AUDIT_CLIENT_SECRETS",
            dir_okay=False,
            help="Google OAuth client JSON (Gmail accounts only).",
        ),
    ] = None,
    token_file: Annotated[
        Path | None,
        typer.Option(
            "--token-file",
            envvar="MAIL_SENDER_AUDIT_TOKEN_FILE",
            dir_okay=False,
            help="Base location for Gmail sign-in tokens (one file per account is derived).",
        ),
    ] = None,
    accounts_file: Annotated[
        Path | None,
        typer.Option(
            "--accounts-file",
            envvar="MAIL_SENDER_AUDIT_ACCOUNTS_FILE",
            dir_okay=False,
            help="Where the list of connected accounts is saved.",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show debug logging.")] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    _configure_logging(verbose)
    ctx.obj = AppState(
        paths=AppPaths.default(
            client_secrets=client_secrets, token_file=token_file, accounts_file=accounts_file
        )
    )


# ------------------------------------------------------------------------------ setup


@app.command()
def setup(
    ctx: typer.Context,
    file: Annotated[
        Path | None,
        typer.Argument(
            dir_okay=False, help="The client_secret JSON downloaded from Google Cloud Console."
        ),
    ] = None,
) -> None:
    """One-time setup for GMAIL accounts only: connect the Google credentials you created.

    Yahoo, iCloud, Fastmail and other IMAP accounts do not need this.
    """
    paths = _state(ctx).paths

    if file is None:
        console.print("[bold]One-time Google setup for Gmail[/] (about five minutes, free):\n")
        for number, step in enumerate(SETUP_STEPS, start=1):
            console.print(f"  {number}. {escape(step)}")
        console.print("\n[dim]Details: README, 'Gmail setup'.[/]\n")
        candidates = find_downloaded_client_secrets()
        if _is_interactive():
            file = interactive.ask_client_secrets_path(candidates)
        elif candidates:
            file = candidates[0]
        else:
            raise UsageError(
                "No client file given and no terminal to ask for one.",
                hint="Run: mail-sender-audit setup /path/to/client_secret.json",
            )

    installed = install_client_secrets(file.expanduser(), paths.client_secrets)
    console.print(f"[green]✓[/] Credentials saved to {escape(str(installed))}")
    console.print(
        "Next: run [bold]mail-sender-audit accounts add[/] to connect your Gmail address."
    )


# --------------------------------------------------------------------------- accounts


def _ask_port(message: str, default: int) -> int:
    while True:
        text = interactive.ask_text(message, default=str(default))
        if text.isdigit() and 0 < int(text) < 65536:
            return int(text)
        err_console.print("[yellow]Enter a port number between 1 and 65535.[/]")


def _custom_servers(address: str) -> tuple[list[ImapSettings], SmtpSettings]:
    if not _is_interactive():
        raise UsageError(
            f"{domain_of(address)} is not a built-in provider, so server details are needed.",
            hint="Run 'mail-sender-audit accounts add' in a terminal to enter them.",
        )
    domain = domain_of(address)
    console.print(
        "[dim]Enter your provider's IMAP and SMTP server details (see its help pages if unsure).[/]"
    )
    imap_host = interactive.ask_text("IMAP server:", default=f"imap.{domain}")
    imap_port = _ask_port("IMAP port (SSL/TLS):", 993)
    smtp_host = interactive.ask_text("SMTP server:", default=f"smtp.{domain}")
    security = interactive.choose_option(
        "SMTP encryption:",
        [
            ("STARTTLS (usually port 587)", Security.STARTTLS),
            ("SSL/TLS (usually port 465)", Security.SSL),
        ],
    )
    smtp_port = _ask_port("SMTP port:", 587 if security is Security.STARTTLS else 465)
    username = interactive.ask_text("Username:", default=address)
    return (
        [ImapSettings(host=imap_host, username=username, port=imap_port)],
        SmtpSettings(host=smtp_host, username=username, port=smtp_port, security=security),
    )


def _obtain_password(address: str) -> str:
    env_password = os.environ.get(PASSWORD_ENV_VAR)
    if env_password:
        return env_password
    if not _is_interactive():
        raise UsageError(
            "A password is needed but there is no terminal to ask for it.",
            hint=f"Set the {PASSWORD_ENV_VAR} environment variable (use an app password).",
        )
    return interactive.ask_password(f"App password for {address}:")


def _verify_imap(candidates: Sequence[ImapSettings], password: str) -> ImapSettings:
    """Return the first login that works; only login failures move on to the next candidate."""
    last_error: MailServerError | None = None
    for settings in candidates:
        source = ImapSource(settings, password, attempts=2)
        try:
            with console.status(f"Checking the connection to {settings.host}..."):
                source.check()
        except MailServerError as exc:
            if not exc.auth_failed:
                raise
            last_error = exc
        else:
            return settings
        finally:
            source.close()
    assert last_error is not None
    raise last_error


def _connect_imap(address: str) -> Account:
    preset = preset_for(address)
    if preset is not None:
        console.print(f"[bold]{preset.display_name}[/]: {escape(preset.app_password_help)}\n")
        logins = preset.imap_settings(address)
        smtp = preset.smtp_settings(address)
    else:
        logins, smtp = _custom_servers(address)

    password = _obtain_password(address)
    imap = _verify_imap(logins, password)

    sender = SmtpSender(smtp, password, address)
    try:
        with console.status(f"Checking the connection to {smtp.host}..."):
            sender.check()
    except MailServerError as exc:
        console.print(
            f"[yellow]![/] Could not log in to the SMTP server ({escape(str(exc))}). "
            "Scanning will work, but unsubscribe requests that need an email will "
            "fall back to manual links."
        )

    if PasswordStore.system().set(address, password):
        console.print("[green]✓[/] Password saved in your system keyring.")
    else:
        console.print(
            "[yellow]![/] No system keyring is available, so the password was not saved. "
            f"You will be asked for it each time, or set {PASSWORD_ENV_VAR}."
        )
    return Account(email=address, kind=AccountKind.IMAP, imap=imap, smtp=smtp)


def _connect_gmail(paths: AppPaths, address: str) -> Account:
    account_paths = paths.for_account(address)
    creds = auth.authenticate(account_paths)
    signed_in = auth.fetch_account_email(auth.build_service(creds)).lower()
    if signed_in != address:
        auth.logout(account_paths)
        raise UsageError(
            f"You signed in as {signed_in}, not {address}.",
            hint=f"Run again with: mail-sender-audit accounts add {signed_in}",
        )
    return Account(email=address, kind=AccountKind.GMAIL)


def _choose_kind(address: str) -> AccountKind:
    if is_gmail(address):
        return AccountKind.GMAIL
    if preset_for(address) is not None or not _is_interactive():
        return AccountKind.IMAP
    return interactive.choose_option(
        f"What kind of account is {address}?",
        [
            ("Another provider (IMAP with an app password)", AccountKind.IMAP),
            ("Google Workspace / Gmail on a custom domain", AccountKind.GMAIL),
        ],
    )


@accounts_app.command("add")
def accounts_add(
    ctx: typer.Context,
    email: Annotated[str | None, typer.Argument(help="The address to connect.")] = None,
) -> None:
    """Connect a mail account (Gmail, Yahoo, iCloud, Fastmail, or any IMAP provider)."""
    paths = _state(ctx).paths
    store = AccountStore(paths.accounts_path)

    if email is None:
        if not _is_interactive():
            raise UsageError(
                "No email address given.",
                hint="Run: mail-sender-audit accounts add you@example.com",
            )
        email = interactive.ask_text("Email address:")
    address = email.strip().lower()
    if not _EMAIL_RE.match(address):
        raise UsageError(f"'{email}' is not a valid email address.")
    if any(existing.email == address for existing in store.load()):
        raise UsageError(
            f"{address} is already connected.",
            hint=f"Remove it first with: mail-sender-audit accounts remove {address}",
        )
    if is_microsoft_personal(address):
        raise UsageError(
            "Outlook.com, Hotmail and Live accounts are not supported yet.",
            hint=(
                "Microsoft no longer accepts passwords from third-party mail apps; it requires "
                "its own OAuth sign-in, which this tool does not implement yet."
            ),
        )

    kind = _choose_kind(address)
    account = (
        _connect_gmail(paths, address) if kind is AccountKind.GMAIL else _connect_imap(address)
    )
    store.add(account)
    console.print(f"[green]✓[/] Connected [bold]{escape(address)}[/]")
    console.print(
        "Next: run [bold]mail-sender-audit scan[/] or [bold]mail-sender-audit unsubscribe[/]."
    )


@accounts_app.command("list")
def accounts_list(ctx: typer.Context) -> None:
    """Show the connected accounts."""
    accounts = AccountStore(_state(ctx).paths.accounts_path).load()
    if not accounts:
        console.print("No accounts connected yet. Run [bold]mail-sender-audit accounts add[/].")
        return
    render_accounts(accounts, console)


@accounts_app.command("remove")
def accounts_remove(
    ctx: typer.Context,
    email: Annotated[str, typer.Argument(help="The address to disconnect.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Disconnect an account: forget its saved password or revoke its Google access."""
    paths = _state(ctx).paths
    store = AccountStore(paths.accounts_path)
    account = select_account(store.load(), email)

    if not yes:
        if not _is_interactive():
            raise UsageError("Refusing to remove without confirmation.", hint="Add --yes.")
        if not interactive.confirm(f"Disconnect {account.email}?"):
            console.print("Cancelled.")
            return

    if account.kind is AccountKind.GMAIL:
        result = auth.logout(paths.for_account(account.email))
        if not result.remote_revoked:
            console.print(
                "[yellow]![/] Could not confirm revocation at Google. You can remove the app at "
                "https://myaccount.google.com/permissions"
            )
    else:
        PasswordStore.system().delete(account.email)
    store.remove(account.email)
    console.print(f"[green]✓[/] Disconnected {escape(account.email)}")


# ------------------------------------------------------------------------------ scan


def _open_session(ctx: typer.Context, selector: str | None, rate: float) -> MailSession:
    paths = _state(ctx).paths
    accounts = AccountStore(paths.accounts_path).load()
    account = select_account(
        accounts, selector, choose=interactive.choose_account if _is_interactive() else None
    )
    console.print(f"Account: [bold]{escape(account.email)}[/]")
    prompt = (
        (lambda: interactive.ask_password(f"App password for {account.email}:"))
        if _is_interactive()
        else None
    )
    return open_session(
        account,
        paths,
        passwords=PasswordStore.system(),
        requests_per_second=rate,
        prompt_password=prompt,
    )


def _scan(session: MailSession, options: ScanOptions) -> ScanResult:
    with RichProgress(console) as progress:
        return InboxScanner(session.source, options, progress).scan()


@app.command()
def scan(
    ctx: typer.Context,
    account: AccountOpt = None,
    max_messages: MaxMessagesOpt = 500,
    unread: UnreadOpt = False,
    days: DaysOpt = None,
    by: GroupByOpt = GroupBy.ADDRESS,
    no_body_scan: NoBodyScanOpt = False,
    workers: WorkersOpt = 8,
    rate: RateOpt = 20.0,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Show only the top N senders.")
    ] = None,
) -> None:
    """Scan the inbox and list senders that offer an unsubscribe option (read-only)."""
    options = ScanOptions(
        unread_only=unread,
        newer_than_days=days,
        max_messages=max_messages,
        group_by=by,
        body_fallback=not no_body_scan,
        max_workers=workers,
    )
    with _open_session(ctx, account, rate) as session:
        result = _scan(session, options)

    if not result.groups:
        console.print("[yellow]No matching messages found.[/]")
        return
    render_scan_table(result, console, limit=limit)


# ------------------------------------------------------------------------ unsubscribe


def _matches(group: SenderGroup, wanted: set[str]) -> bool:
    if group.key in wanted:
        return True
    return any(
        target.address in wanted or target.address.rpartition("@")[2] in wanted
        for target in group.targets
    )


def _select_groups(
    candidates: Sequence[SenderGroup], *, everything: bool, senders: Sequence[str]
) -> list[SenderGroup]:
    if everything:
        return list(candidates)

    if senders:
        wanted = {s.strip().lower() for s in senders}
        for name in sorted(w for w in wanted if not any(_matches(g, {w}) for g in candidates)):
            err_console.print(f"[yellow]No unsubscribable sender matches[/] {escape(name)}")
        return [g for g in candidates if _matches(g, wanted)]

    if not _is_interactive():
        raise UsageError(
            "Cannot choose senders without a terminal.",
            hint="Pass --all or --sender ADDRESS_OR_DOMAIN (repeatable).",
        )
    return interactive.choose_groups(candidates)


def _open_links(outcomes: Sequence[UnsubscribeOutcome]) -> None:
    for outcome in outcomes:
        url = outcome.manual_url
        if outcome.status is not Status.MANUAL or not url:
            continue
        if not webbrowser.open(url, new=2):
            console.print(f"Could not open a browser. Open this yourself: {escape(url)}")
        time.sleep(0.7)  # keep the browser from being flooded with tabs at once


@app.command()
def unsubscribe(
    ctx: typer.Context,
    account: AccountOpt = None,
    max_messages: MaxMessagesOpt = 500,
    unread: UnreadOpt = False,
    days: DaysOpt = None,
    by: GroupByOpt = GroupBy.ADDRESS,
    no_body_scan: NoBodyScanOpt = False,
    workers: WorkersOpt = 8,
    rate: RateOpt = 20.0,
    all_senders: Annotated[
        bool, typer.Option("--all", help="Select every sender that offers an unsubscribe option.")
    ] = False,
    sender: Annotated[
        list[str] | None,
        typer.Option(
            "--sender", "-s", help="Sender address or domain to unsubscribe from (repeatable)."
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would happen; send nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    open_links: Annotated[
        bool,
        typer.Option("--open-links", help="Open links that need manual steps in your browser."),
    ] = False,
) -> None:
    """Scan, choose senders (all, specific, or dry run) and unsubscribe."""
    options = ScanOptions(
        unread_only=unread,
        newer_than_days=days,
        max_messages=max_messages,
        group_by=by,
        body_fallback=not no_body_scan,
        max_workers=workers,
    )
    with _open_session(ctx, account, rate) as session:
        result = _scan(session, options)

        candidates = result.unsubscribable
        if not candidates:
            console.print("[yellow]No senders with an unsubscribe option were found.[/]")
            return
        render_scan_table(result, console)

        selected = _select_groups(candidates, everything=all_senders, senders=sender or [])
        actions = plan_actions(selected)
        if not actions:
            console.print("Nothing selected.")
            return
        summary = summarize_plan(actions)

        simulate = dry_run
        if not dry_run and not yes:
            if not _is_interactive():
                raise UsageError(
                    "Refusing to unsubscribe without confirmation.",
                    hint="Add --yes to proceed, or --dry-run to preview.",
                )
            choice = interactive.choose_action(f"Ready: {summary}")
            if choice is interactive.Action.CANCEL:
                console.print("Cancelled. Nothing was sent.")
                return
            simulate = choice is interactive.Action.DRY_RUN
        else:
            console.print(f"{summary}.")

        with HttpxPoster() as poster, RichProgress(console) as progress:
            outcomes = Unsubscriber(poster, session.sender).run(
                selected, dry_run=simulate, progress=progress
            )

    render_outcomes(outcomes, console)

    if simulate:
        console.print("[blue]Dry run:[/] nothing was sent. Run again without --dry-run to proceed.")
        return

    console.print(
        "[dim]Senders process requests on their side, so mail may keep arriving for a few days. "
        "Existing messages in your inbox were not touched.[/]"
    )
    manual = [o for o in outcomes if o.status is Status.MANUAL and o.manual_url]
    if manual and (
        open_links
        or (
            _is_interactive()
            and interactive.confirm(f"Open {len(manual)} link(s) in your browser to finish?")
        )
    ):
        _open_links(manual)

    if any(o.status is Status.FAILED for o in outcomes):
        raise typer.Exit(1)


def main() -> None:
    """Console-script entry point: turn expected errors into clean exits."""
    try:
        app()
    except (UserCancelledError, KeyboardInterrupt) as exc:
        err_console.print("[dim]Cancelled.[/]")
        raise SystemExit(130) from exc
    except MailSenderAuditError as exc:
        err_console.print(f"[bold red]Error:[/] {escape(str(exc))}")
        if exc.hint:
            err_console.print(f"[dim]Hint:[/] {escape(exc.hint)}")
        raise SystemExit(exc.exit_code) from exc
