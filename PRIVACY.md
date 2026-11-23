# Privacy

`mail-sender-audit` is a local program. It has no server, no account system, no analytics and no
telemetry, and the author never receives any of your data.

## What it reads

When you run `scan` or `unsubscribe`, it reads from your inbox, through the Gmail API or over
IMAP depending on the account:

- the `From`, `Subject`, `List-Unsubscribe` and `List-Unsubscribe-Post` headers of recent messages;
- for senders with no `List-Unsubscribe` header, the body of **one** recent message per sender
  (at most its first 512 KB over IMAP), to look for an "unsubscribe" link.

Mail is opened **read-only**: nothing is marked as read, moved or deleted. What is read is
processed in memory on your computer and **not saved to disk**. Scan results are discarded when
the program exits.

## What it stores

Small files in your user folders, readable by your user account only:

- the list of connected accounts (`accounts.json`): addresses and mail-server names, **no passwords**;
- for **Gmail** accounts: the OAuth client file you created (`credentials.json`) and your Google
  access/refresh token (`token-<address>.json`);
- for **IMAP** accounts (Yahoo, iCloud, Fastmail, AOL, others): the app password, stored in your
  **operating system's keyring** (macOS Keychain, Windows Credential Manager, Secret Service on
  Linux), not in a file. If no keyring is available it is not stored at all.

`mail-sender-audit accounts remove` deletes the saved password, or revokes the Google token and deletes it.

## Who it talks to

- **Your mail provider**: Google (`accounts.google.com`, `oauth2.googleapis.com`,
  `gmail.googleapis.com`) for Gmail accounts, or your provider's IMAP and SMTP servers for the
  others, to sign in, read headers/bodies and send `mailto:` unsubscribe emails from your address.
- **The unsubscribe addresses senders put in their own emails**, only when you choose to
  unsubscribe. A one-click request contains no data beyond the fixed text
  `List-Unsubscribe=One-Click`, but the receiving server sees your IP address, as with any
  web request, and the URL itself may identify you to that sender (that is how unsubscribing works).
- `mailto:` requests are real emails sent from your address. Gmail files them in Sent; most other
  providers do not when mail is submitted over SMTP.

Nothing else is contacted.

## Permissions

- **Gmail:** `gmail.readonly` and `gmail.send`. The app cannot delete, trash, archive or label mail.
  You can remove its access at any time at <https://myaccount.google.com/permissions>.
- **IMAP providers:** an app password gives the tool IMAP and SMTP access. Create one just for
  this tool and revoke it in your provider's security settings when you no longer need it.
