# Security Policy

## Reporting a vulnerability

Please report security issues **privately** via GitHub Security Advisories
("Report a vulnerability" under the repository's **Security** tab). We aim to
acknowledge reports within a few business days.

Do not open a public issue for a suspected vulnerability.

## Scope

Dais orchestrates local, headless [Claude Code](https://docs.claude.com/en/docs/claude-code)
sessions against a SQLite board and your own project repositories. It runs with
your local credentials and shell. When reporting, please note:

- Dais executes agents with the permissions of the invoking user. Treat the
  machine and workspace as the trust boundary.
- Secrets belong in your environment / project repos, never in the board or in
  Dais itself.

## Supported versions

Fixes land on the latest released version. Pin a tag if you need stability.
