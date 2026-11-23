"""Terminal presentation: progress bars and result tables (Rich)."""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from mail_sender_audit.accounts import Account
from mail_sender_audit.models import Method, ScanResult, Status, UnsubscribeOutcome
from mail_sender_audit.unsubscriber import best_method

_METHOD_STYLE = {Method.ONE_CLICK: "green", Method.EMAIL: "cyan", Method.MANUAL: "yellow"}
_STATUS_STYLE = {
    Status.REQUESTED: "green",
    Status.MANUAL: "yellow",
    Status.FAILED: "red",
    Status.DRY_RUN: "blue",
}


class RichProgress:
    """Adapts Rich's progress bar to the ``Progress`` protocol in ``mail_sender_audit.progress``.

    Use as a context manager so the live display is always torn down, even on errors.
    """

    def __init__(self, console: Console) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )
        self._task: TaskID | None = None

    def __enter__(self) -> RichProgress:
        self._progress.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._progress.stop()

    def start(self, description: str, total: int | None) -> None:
        if self._task is not None:
            self._progress.remove_task(self._task)
        self._task = self._progress.add_task(description, total=total)

    def advance(self, step: int = 1) -> None:
        if self._task is not None:
            self._progress.advance(self._task, step)


def _styled_method(method: Method | None) -> str:
    if method is None:
        return "[red]none found[/]"
    return f"[{_METHOD_STYLE[method]}]{method.value}[/]"


def render_scan_table(result: ScanResult, console: Console, *, limit: int | None = None) -> None:
    """Print the grouped results, largest sender first."""
    table = Table(title="Mailing lists in your inbox", title_justify="left")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Sender")
    table.add_column("Emails", justify="right")
    table.add_column("Unread", justify="right")
    table.add_column("Unsubscribe via")

    groups = result.groups if limit is None else result.groups[:limit]
    for index, group in enumerate(groups, start=1):
        label = escape(group.label)
        key = escape(group.key)
        sender = label if label == key else f"{label} [dim]<{key}>[/]"
        table.add_row(
            str(index),
            sender,
            str(group.count),
            str(group.unread_count),
            _styled_method(best_method(group)),
        )
    console.print(table)

    hidden = len(result.groups) - len(groups)
    if hidden > 0:
        console.print(f"[dim]... and {hidden} more sender(s). Use --limit to show more.[/]")

    summary = (
        f"{result.messages_scanned} messages scanned, {len(result.groups)} senders, "
        f"{len(result.unsubscribable)} with an unsubscribe option"
    )
    if result.messages_skipped:
        summary += f", {result.messages_skipped} skipped"
    console.print(f"[dim]{summary}.[/]")


def render_outcomes(outcomes: Sequence[UnsubscribeOutcome], console: Console) -> None:
    """Print what happened (or, for a dry run, what would happen) for each mailing list."""
    table = Table(title="Results", title_justify="left")
    table.add_column("Sender")
    table.add_column("Result")
    table.add_column("Method")
    table.add_column("Details", overflow="fold")

    for outcome in outcomes:
        sender = escape(outcome.label)
        if outcome.address not in {outcome.label, "unknown"}:
            sender += f" [dim]<{escape(outcome.address)}>[/]"
        detail = escape(outcome.detail)
        if outcome.manual_url:
            detail = (detail + " " if detail else "") + escape(outcome.manual_url)
        table.add_row(
            sender,
            f"[{_STATUS_STYLE[outcome.status]}]{outcome.status.value}[/]",
            _styled_method(outcome.method) if outcome.method else "[dim]-[/]",
            detail,
        )
    console.print(table)

    counts = {status: sum(1 for o in outcomes if o.status is status) for status in Status}
    parts = [f"{n} {status.value}" for status, n in counts.items() if n]
    console.print(f"[dim]{', '.join(parts) if parts else 'Nothing to do'}.[/]")


def render_accounts(accounts: Sequence[Account], console: Console) -> None:
    table = Table(title="Connected accounts", title_justify="left")
    table.add_column("Address")
    table.add_column("Type")
    for account in accounts:
        table.add_row(escape(account.email), escape(account.description))
    console.print(table)
