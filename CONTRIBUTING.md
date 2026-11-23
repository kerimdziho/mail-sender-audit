# Contributing

Thanks for helping! Bug reports, fixes and ideas are all welcome.

## Setup

```bash
git clone https://github.com/kerimdziho/mail-sender-audit && cd mail-sender-audit
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Before opening a pull request

```bash
ruff check . --fix
ruff format .
mypy
pytest
```

CI runs the same checks on Linux, macOS and Windows with Python 3.11 to 3.13.

## Guidelines

- **No network in tests.** Reach mail servers and HTTP through the existing fakes and protocols
  (`MailSource`, `OneClickPoster`, `MessageSender`; see the fake Gmail service in
  `tests/test_scanner.py` and the fake IMAP/SMTP servers in `tests/test_imap.py`).
- **Adding a provider.** Implement `MailSource` (read) and `MessageSender` (send) in a new module
  and wire it into `session.open_session`. Providers that speak IMAP only need a preset in
  `providers.py`.
- **Keep logic pure where you can.** `parsing.py`, `grouping.py`, `unsubscriber.py` and
  `netsafety.py` import no network libraries on purpose.
- **Typed code.** The project is checked with `mypy --strict`.
- **Errors:** raise a subclass of `MailSenderAuditError` for anything a user can cause or fix; give
  it a clear message and, where useful, a `hint`.
- **Treat email content as hostile.** Anything coming from headers or bodies needs validation
  before it is requested, sent or displayed. See [SECURITY.md](SECURITY.md).
- **Never commit** `credentials.json`, `client_secret*.json` or `token.json`.

## Releasing (maintainers)

1. Update `__version__` in `src/mail_sender_audit/__init__.py` and `CHANGELOG.md`.
2. Commit, then tag and push: `git tag v0.1.0 && git push origin v0.1.0`.
3. The *Release* workflow builds the package and the Linux/macOS/Windows executables, creates the
   GitHub Release with checksums, and publishes to PyPI (configure
   [trusted publishing](https://docs.pypi.org/trusted-publishers/) for the `pypi` environment first).
