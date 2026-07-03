# Contributing to Dais

Thanks for your interest. Dais is a small, dependency-light tool — a Bash CLI
over a pure-Python-stdlib harness and a SQLite board.

## Development

```bash
git clone https://github.com/eigensoftware/dais
cd dais
python3 -m pytest harness/tests/     # the full suite (no third-party deps)
```

Run the CLI from the checkout, or symlink it onto your PATH:

```bash
ln -s "$PWD/dais" ~/.local/bin/dais
dais init ~/my-company               # bootstrap a workspace to operate on
```

## Ground rules

- **Stdlib only** in the harness. Keep it installable with nothing but Python 3
  and a POSIX shell.
- **Every state change fires a machine edge** — never poke `status` directly.
  See `design/machine-model.md`.
- Add or update tests for behavior changes; keep the suite green.
- Keep the diff focused; explain the "why" in the PR description.

## Reporting bugs / requesting features

Open an issue with steps to reproduce (for bugs) or the problem you're trying to
solve (for features). For security issues, see [SECURITY.md](SECURITY.md).
