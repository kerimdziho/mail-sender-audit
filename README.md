# mail-sender-audit

[![CI](https://github.com/kerimdziho/mail-sender-audit/actions/workflows/ci.yml/badge.svg)](https://github.com/kerimdziho/mail-sender-audit/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mail-sender-audit)](https://pypi.org/project/mail-sender-audit/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Clean up your inbox from the terminal.** `mail-sender-audit` scans your inbox, groups mailing lists
by sender with email counts, and unsubscribes you from the ones you pick, or from all of them,
using the unsubscribe mechanisms the senders themselves provide.

**Works with Gmail, Yahoo, iCloud, Fastmail, AOL and any other IMAP provider.**

- Runs **entirely on your computer**. No server, no account, no telemetry ([privacy](PRIVACY.md)).
- Understands `List-Unsubscribe` headers, **one-click unsubscribe (RFC 8058)** and `mailto:` links,
  and falls back to scanning the email body for an "unsubscribe" link.
- **Preview first:** `--dry-run` shows exactly what would happen and sends nothing.
- **Read-only access to your mail:** it never deletes, moves, labels or marks anything as read.
- Several accounts can be connected at once.

```text
$ mail-sender-audit unsubscribe --dry-run
                    Mailing lists in your inbox
 #  Sender                          Emails  Unread  Unsubscribe via
 1  Daily Deals <deals@shop.example>   142      97  one-click
 2  The Weekly <news@weekly.example>    58      12  email
 3  Promo <hi@promo.example>            31      31  manual link
```

*(Illustrative output.)*

## Supported providers

| Provider | How you connect | One-time setup |
| --- | --- | --- |
| **Gmail** / Google Workspace | Sign in with Google in your browser | About five minutes in Google Cloud Console ([Gmail setup](#gmail-setup)) |
| **Yahoo**, **iCloud**, **Fastmail**, **AOL** | An app password | None: server settings are built in |
| **Any other provider with IMAP** (GMX, Zoho, your own domain, ...) | An app password and the server names | None, you enter the server names once |
| **Outlook.com, Hotmail, Live** | Not supported yet | Microsoft no longer accepts passwords from third-party mail apps and requires its own OAuth sign-in, which is not implemented yet |

Some providers require IMAP access to be switched on in their mail settings first.

## Install

Choose whichever option suits you.

**Option 1: pipx (recommended).** Needs Python 3.11+.

```bash
pipx install mail-sender-audit
```

**Option 2: standalone program, no Python needed.** Download the file for your system from the
[latest release](https://github.com/kerimdziho/mail-sender-audit/releases/latest)
(Linux, macOS on Apple Silicon, Windows), then run it from a terminal. Verify the download
against `SHA256SUMS` from the same page. The programs are not code-signed, so macOS and Windows
may show an "unidentified developer" / SmartScreen warning the first time; on macOS run
`chmod +x mail-sender-audit-macos-arm64` and open it once via right-click, Open.

**Option 3: pip or from source.**

```bash
pip install mail-sender-audit                  # from PyPI
pip install "git+https://github.com/kerimdziho/mail-sender-audit"   # latest main
```

## Quick start

**Yahoo, iCloud, Fastmail, AOL, or any IMAP provider:**

```bash
mail-sender-audit accounts add you@yahoo.com   # asks for an app password, checks it, saves it
mail-sender-audit scan                         # look around, changes nothing
mail-sender-audit unsubscribe                  # pick senders, preview or go
```

**Gmail** needs one extra, one-time step first ([details below](#gmail-setup)):

```bash
mail-sender-audit setup                        # connect the Google credentials you created
mail-sender-audit accounts add you@gmail.com   # signs you in with Google in the browser
mail-sender-audit scan
mail-sender-audit unsubscribe
```

`unsubscribe` shows your senders as a checklist. Press **space** to select, **a** to select
all, **enter** to continue, then choose **Unsubscribe now** or **Dry run**.

## Connecting an account

### App-password accounts (Yahoo, iCloud, Fastmail, AOL, other IMAP)

These providers do not let third-party apps use your normal password. Instead you create an
**app password**: a separate password that works only for mail apps and that you can revoke
at any time.

1. In your provider's account security settings, create an app password
   (`mail-sender-audit accounts add` prints where to find it for the built-in providers).
2. Run `mail-sender-audit accounts add you@example.com` and paste the app password when asked.
3. The tool checks that the login works and stores the password in your **operating system's
   keyring** (macOS Keychain, Windows Credential Manager, Secret Service on Linux). It is never
   written to a plain file.

For providers that are not built in, you are asked once for the IMAP server, the SMTP server
and the encryption type; both are listed in your provider's help pages.

Without a keyring (some headless Linux setups) the password is not saved: you are asked for it
on each run, or you can set the `MAIL_SENDER_AUDIT_PASSWORD` environment variable (handy for scripts).

### Gmail setup

#### Why do I need to do this?

Gmail's API only lets an app read mail if it belongs to a Google Cloud project. Google
requires a paid security audit before a *public* app may do that for arbitrary users, which is
not realistic for a free open-source tool. So instead you create a free, private "app" that
only you can use. It takes about five minutes, once, and nothing about it is shared with
anyone, including the author of this tool. (Only Gmail needs this; other providers do not.)

#### Steps

`mail-sender-audit setup` prints these and then installs the file for you. Google occasionally
renames console menus, so names below may differ slightly.

1. **Create a project:** <https://console.cloud.google.com/projectcreate> (any name).
2. **Enable the Gmail API:** <https://console.cloud.google.com/apis/library/gmail.googleapis.com> and click *Enable*.
3. **Set up the consent screen** (*Google Auth Platform*, <https://console.cloud.google.com/auth/overview>):
   fill in an app name and your email, choose **External** as the audience, and add **your own
   Gmail address as a test user**.
4. **Add scopes** (*Data Access*): `https://www.googleapis.com/auth/gmail.readonly` and
   `https://www.googleapis.com/auth/gmail.send`.
5. **Create the credentials** (*Clients*): create a client of type **Desktop app** and
   **download the JSON** file.
6. Run `mail-sender-audit setup`. It finds the download in your Downloads folder (or asks for the
   path) and stores a private copy in the right place for your system.
   Prefer to manage the file yourself? Use `--client-secrets PATH` or set `MAIL_SENDER_AUDIT_CLIENT_SECRETS`.

#### What you will see when signing in

- **"Google hasn't verified this app"**: expected, because the app is yours. Click
  *Advanced*, then *Go to … (unsafe)*. You are trusting your own project.
- While the consent screen is in **Testing** status, Google expires sign-ins after **7 days**
  and you are simply sent through the browser sign-in again. To avoid that, set the publishing
  status to **In production** (Audience page). It stays unverified and private to you. Do not
  submit it for verification.

## Commands

| Command | What it does |
| --- | --- |
| `accounts add [ADDRESS]` | Connect an account (Gmail, Yahoo, iCloud, Fastmail, AOL, or any IMAP provider). |
| `accounts list` | Show the connected accounts. |
| `accounts remove ADDRESS` | Disconnect: forget the saved password, or revoke Google access. |
| `setup [FILE]` | Gmail only: install your Google OAuth client file (guided). |
| `scan` | List senders, email counts and the best unsubscribe method. Read-only. |
| `unsubscribe` | Scan, choose senders, and unsubscribe (or preview with `--dry-run`). |

Useful options for `scan` and `unsubscribe`:

| Option | Meaning |
| --- | --- |
| `-a, --account X` | Which connected account to use (full address or a unique part of it). Optional when only one is connected. |
| `-n, --max N` | Inspect at most N inbox messages (default 500, max 5000). |
| `--unread` | Only unread mail. |
| `--days N` | Only mail from the last N days. |
| `--by address\|domain` | Group by exact sender or by domain. |
| `--no-body-scan` | Skip the slower email-body fallback. |
| `--workers N`, `--rate R` | Concurrency and requests/second ceiling (defaults are safe). |

Only for `unsubscribe`:

| Option | Meaning |
| --- | --- |
| `--all` | Select every sender that offers a way out. |
| `-s, --sender X` | Select a specific address or domain (repeat for several). |
| `--dry-run` | Show what would happen; send nothing. |
| `-y, --yes` | Skip the confirmation prompt (needed when there is no terminal). |
| `--open-links` | Open links that need manual steps in your browser. |

Examples:

```bash
mail-sender-audit scan --unread --days 30 --limit 20
mail-sender-audit unsubscribe --dry-run                       # interactive preview
mail-sender-audit unsubscribe --all --yes                     # bulk, no questions
mail-sender-audit unsubscribe -a you@yahoo.com -s news@weekly.example -s promo.example --yes
```

Global options: `--client-secrets`, `--token-file`, `--accounts-file`, `-v/--verbose`, `--version`.

## How unsubscribing works

For each mailing list the best method is tried first:

1. **One-click (RFC 8058):** an HTTPS `POST` of `List-Unsubscribe=One-Click` to the URL in the
   email's `List-Unsubscribe` header. This is the method Gmail's own "Unsubscribe" button uses.
2. **Email:** if the sender offers a `mailto:` address, the request is sent from your account:
   through the Gmail API for Gmail (it appears in your Sent folder), or over SMTP for other
   providers (which usually do not file it in Sent).
3. **Manual:** if neither works, or the sender only offers a plain link, the link is listed and
   can be opened in your browser (`--open-links`, or answer yes when asked). Plain links are
   never requested automatically, and links found in an email body are never POSTed to.

If one method fails, the next is tried. The results table shows what happened for every sender.
A "requested" result means the request was delivered; senders then process it on their side,
so mail can keep arriving for a few days. Your existing emails are never touched.

## Safety and privacy

- Everything runs locally; the only network traffic goes to your mail provider, and to the
  unsubscribe addresses the senders themselves put in their emails. See [PRIVACY.md](PRIVACY.md).
- Mail is opened read-only. Gmail uses `gmail.readonly` and `gmail.send` and nothing more; IMAP
  accounts use `BODY.PEEK`, so nothing is marked as read.
- Passwords live in your OS keyring and Google tokens in private (`0600`) files.
  `mail-sender-audit accounts remove` deletes them (and revokes the Google token).
- IMAP and SMTP connections always use TLS with certificate verification.
- Unsubscribe links come from whoever emailed you, so they are treated as untrusted: only HTTPS
  addresses that resolve to public internet hosts are ever contacted, redirects are not
  followed, no cookies are sent, and `mailto:` fields are sanitised. Details in [SECURITY.md](SECURITY.md).

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "The mail server rejected the login" | Use an **app password**, not your normal password. iCloud logins use the part before `@icloud.com`; the tool tries both forms. |
| IMAP login fails even with an app password | Some providers need IMAP switched on in their mail settings first. |
| `OAuth client file not found` (Gmail) | Run `mail-sender-audit setup`. |
| `access_denied` or "app not verified" with no *Advanced* link | Add your Gmail address under *Audience → Test users* in Google Cloud Console. |
| Asked to sign in to Google again every week | Your consent screen is in *Testing*; switch it to *In production* (see above). |
| `Required Gmail permissions were not granted` | Run `mail-sender-audit accounts remove` then `accounts add` again, ticking every permission box. |
| Sign-in on a server / over SSH (Gmail) | The sign-in URL is printed; open it on any device. |
| Outlook.com / Hotmail / Live address refused | Not supported yet: Microsoft requires its own OAuth sign-in for third-party apps. |
| "403 rateLimitExceeded" in verbose logs | Normal under heavy use; requests are retried with backoff. Lower `--rate` if it persists. |
| A sender keeps emailing | Some senders ignore requests. Create a filter for them, or mark as spam. |

Still stuck? [Open an issue](https://github.com/kerimdziho/mail-sender-audit/issues/new/choose)
and include the output of the command with `-v`. Never paste passwords, tokens or credential files.

## Development

```bash
git clone https://github.com/kerimdziho/mail-sender-audit && cd mail-sender-audit
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

```
src/mail_sender_audit/
├── cli.py            Typer app: setup, accounts, scan, unsubscribe
├── session.py        open an account as a MailSource + MessageSender
├── source.py         MailSource protocol (what every provider implements)
├── gmail.py          Gmail API: client with rate limit/retries, GmailSource, GmailSender
├── imap.py           IMAP source + SMTP sender (standard library only)
├── providers.py      presets: Yahoo, iCloud, Fastmail, AOL; provider detection
├── accounts.py       connected-accounts list and account selection
├── secrets_store.py  OS keyring for IMAP passwords
├── auth.py           Google OAuth desktop flow, token store, revoke
├── onboarding.py     validate + install the Google OAuth client file
├── scanner.py        provider-neutral concurrent scan, body fallback
├── parsing.py        List-Unsubscribe, mailto, MIME and HTML parsing (pure)
├── grouping.py       group by sender / domain (pure)
├── unsubscriber.py   one-click / email / manual execution, dry run (no I/O libs)
├── netsafety.py      SSRF guard for unsubscribe URLs
├── httpclient.py     httpx transport for one-click POSTs
├── backoff.py        RateLimiter and retry-with-backoff (stdlib only)
├── interactive.py    questionary prompts
├── ui.py             Rich progress bars and tables
├── config.py, errors.py, models.py, progress.py, fsutil.py
```

Design notes: every provider implements one small protocol (`MailSource`), so the scanner and
unsubscriber are provider-neutral; network and mail are reached through protocols, so all
logic is unit-tested against fake Gmail/IMAP/SMTP servers; threads rather than asyncio because
the client libraries are synchronous; `mypy --strict` throughout. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

Not affiliated with or endorsed by Google, Yahoo, Apple, Microsoft or any other provider. Product
names are trademarks of their owners. Use at your own risk; review the dry run before
unsubscribing in bulk.

## License

[MIT](LICENSE)
