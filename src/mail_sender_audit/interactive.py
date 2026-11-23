"""Interactive prompts (questionary). Every prompt raises on Ctrl-C instead of returning ``None``."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import questionary

from mail_sender_audit.accounts import Account
from mail_sender_audit.errors import UserCancelledError
from mail_sender_audit.models import SenderGroup
from mail_sender_audit.unsubscriber import best_method

T = TypeVar("T")


class Action(StrEnum):
    RUN = "run"
    DRY_RUN = "dry-run"
    CANCEL = "cancel"


def _ask(question: questionary.Question) -> Any:
    answer = question.ask()
    if answer is None:  # Ctrl-C / Esc
        raise UserCancelledError("Cancelled.")
    return answer


def _title(group: SenderGroup) -> str:
    method = best_method(group)
    how = method.value if method is not None else "n/a"
    name = group.label if len(group.label) <= 34 else group.label[:33] + "…"
    return f"{name:<34} {group.count:>5} emails   {how}"


def choose_groups(groups: Sequence[SenderGroup]) -> list[SenderGroup]:
    """Let the user tick the senders to unsubscribe from."""
    choices = [questionary.Choice(title=_title(g), value=i) for i, g in enumerate(groups)]
    picked: list[int] = _ask(
        questionary.checkbox(
            "Which senders do you want to unsubscribe from?",
            choices=choices,
            instruction="(space: select, a: all, i: invert, enter: confirm)",
        )
    )
    return [groups[i] for i in picked]


def choose_action(summary: str) -> Action:
    """Ask whether to proceed, simulate, or stop."""
    result: Action = _ask(
        questionary.select(
            summary,
            choices=[
                questionary.Choice("Unsubscribe now", value=Action.RUN),
                questionary.Choice(
                    "Dry run (show what would happen, send nothing)", value=Action.DRY_RUN
                ),
                questionary.Choice("Cancel", value=Action.CANCEL),
            ],
        )
    )
    return result


def confirm(message: str, *, default: bool = False) -> bool:
    answer: bool = _ask(questionary.confirm(message, default=default))
    return answer


def ask_client_secrets_path(candidates: Sequence[Path]) -> Path:
    """Pick one of the detected downloads, or type a path."""
    other = object()
    if candidates:
        choices = [questionary.Choice(str(p), value=p) for p in candidates]
        choices.append(questionary.Choice("Enter a different path...", value=other))
        picked = _ask(questionary.select("Found these downloaded client files:", choices=choices))
        if picked is not other:
            return Path(picked)
    text: str = _ask(questionary.path("Path to the downloaded client_secret JSON file:"))
    return Path(text.strip().strip("\"'")).expanduser()


def ask_text(message: str, default: str = "") -> str:
    answer: str = _ask(questionary.text(message, default=default))
    return answer.strip()


def ask_password(message: str) -> str:
    answer: str = _ask(questionary.password(message))
    return answer


def choose_option(message: str, options: Sequence[tuple[str, T]]) -> T:
    """Pick one of ``(label, value)`` options."""
    picked: T = _ask(
        questionary.select(
            message, choices=[questionary.Choice(label, value=v) for label, v in options]
        )
    )
    return picked


def choose_account(accounts: Sequence[Account]) -> Account:
    return choose_option("Which account?", [(f"{a.email}  ({a.description})", a) for a in accounts])
