# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First public release.

### Added

- `setup`, `accounts add|list|remove`, `scan` and `unsubscribe` commands.
- **Works with any mail provider:** Gmail (Google sign-in) plus Yahoo, iCloud, Fastmail, AOL and any
  IMAP server using app passwords, read-only over IMAP and sent through SMTP. Passwords are kept
  in the OS keyring. Several accounts can be connected and chosen with `--account`.
- Outlook.com, Hotmail and Live are not supported yet (Microsoft requires OAuth for third-party apps).
- OAuth 2.0 desktop sign-in with minimal scopes (`gmail.readonly`, `gmail.send`), automatic
  token refresh and private on-disk token storage.
- Concurrent inbox scanning with rate limiting and exponential backoff.
- `List-Unsubscribe` / `List-Unsubscribe-Post` parsing, with an HTML/plain-text body fallback.
- Grouping by sender address or domain with email and unread counts.
- Interactive sender selection (select all, pick some), confirmation, and `--dry-run`.
- RFC 8058 one-click unsubscribe, `mailto:` requests through the Gmail API, and manual links
  with automatic fallback between methods.
- SSRF protection for unsubscribe URLs (public HTTPS hosts only, no redirects).
- CI on Linux/macOS/Windows and a release workflow producing PyPI packages and standalone binaries.
