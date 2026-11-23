# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub's
[private vulnerability reporting](https://github.com/kerimdziho/mail-sender-audit/security/advisories/new)
rather than a public issue. You will get a reply as soon as I can manage, and credit in the
release notes if you want it.

## Threat model

This tool processes **untrusted input**: every email header and body, including the
unsubscribe URLs, is controlled by whoever sent you the message. It also holds a token that
can read and send your mail. The defences are:

| Risk | Mitigation |
| --- | --- |
| A crafted `List-Unsubscribe` URL makes the tool contact internal services (SSRF), such as `localhost`, a router, or a cloud metadata endpoint | Automatic requests require `https` and a host that resolves **only** to public addresses (`netsafety.py`). Private, loopback, link-local, carrier-grade NAT and IPv4-mapped addresses are blocked. |
| Redirect chains used to bypass the check or bounce the request | Redirects are never followed; a redirect response is reported as a failure. |
| Hostile servers streaming huge responses | Only the HTTP status is read; the response body is never downloaded. |
| Header injection through `mailto:` fields (`%0D%0ABcc:...`) | Addresses are validated, subjects collapsed to one line, and the `email` package refuses malformed headers. |
| Cross-site-request style attacks through plain links | Plain web links are never requested automatically, only listed for a human. Links scraped from message bodies are never POSTed to. |
| Token theft from disk | Written atomically with owner-only (`0600`) permissions; `accounts remove` revokes it at Google. |
| Over-broad access (Gmail) | Only `gmail.readonly` and `gmail.send` are requested. |
| Mail is changed or marked read by scanning (IMAP) | The mailbox is opened read-only and bodies are fetched with `BODY.PEEK`. |
| Man-in-the-middle on IMAP/SMTP | TLS is mandatory and certificates are always verified; there is no "insecure" option. |
| IMAP command injection through message ids | Ids are validated as numeric UIDs before being used in a command. |
| IMAP password theft from disk | Passwords are kept in the operating system keyring, never in the accounts file or any plain file. The accounts file holds only addresses and server names (`0600`). |
| Malicious HTML in email bodies | Parsed as data with BeautifulSoup; never rendered, executed or fetched. |

## Known limitations

- **DNS rebinding.** The public-address check resolves a hostname separately from the HTTP
  client's own lookup. A malicious DNS server that answers differently the second time could
  bypass it. The impact is limited to one fixed-body POST request that carries no cookies or credentials.
- **The OAuth client secret is not secret.** For installed ("Desktop app") applications Google
  documents that the client secret cannot be kept confidential. Your credentials file only
  identifies *your* private Google Cloud app; the token is what grants mailbox access.
- **Unsigned binaries.** Standalone executables on the releases page are built by GitHub
  Actions but not code-signed. Verify them against `SHA256SUMS`, or install with `pipx`.
- **No keyring, no storage.** If the system has no keyring (some headless Linux setups) the
  password is not saved; you are asked each run, or you supply `MAIL_SENDER_AUDIT_PASSWORD`, which
  is then only as safe as your shell environment.
- **App passwords are broader than needed.** Providers issue them for IMAP *and* SMTP and cannot
  restrict them to read-only. Create one just for this tool and revoke it when you are done.

## Supported versions

Only the latest release receives security fixes.
