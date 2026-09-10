"""Test isolation for the user-level account state (plan 5.4). accounts.py defaults to
~/.dais/accounts.yaml and ~/.dais/accounts/<name>.cooldown; a run-agent test that caps a fake
provider wrote a real marker there (found 2026-09-09). Point both at a session temp dir before
any test or subprocess runs; tests that need their own file set the variables explicitly."""
import os
import tempfile

_ISO = tempfile.mkdtemp(prefix="dais-accounts-iso-")
os.environ.setdefault("DAIS_ACCOUNTS_FILE", os.path.join(_ISO, "accounts.yaml"))
os.environ.setdefault("DAIS_ACCOUNTS_DIR", os.path.join(_ISO, "markers"))
