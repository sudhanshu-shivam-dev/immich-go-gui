"""Shared fixtures keeping the test suite hermetic.

Every test runs against a per-session temporary home directory and an
in-memory keyring backend, so the suite never reads or writes the real
user config (config.toml, secrets.toml), run locks, or the OS keyring.

core/config_manager.py resolves the config directory from
IMMICH_GO_GUI_CONFIG, then XDG_CONFIG_HOME / APPDATA / HOME (see
default_config_dir), and core/process_tracker.py derives the lock
directory from it — so redirecting those environment variables keeps all
on-disk state inside the temporary directory.
"""

import keyring
import keyring.backend
import keyring.errors
import pytest


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """Volatile keyring backend so tests never touch the OS keyring."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture(scope="session")
def isolated_home(tmp_path_factory):
    """Per-session temporary directory standing in for the user's home."""
    return tmp_path_factory.mktemp("isolated-home")


@pytest.fixture(scope="session", autouse=True)
def _isolated_environment(isolated_home):
    """Redirects HOME/XDG/config paths and installs the in-memory keyring."""
    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(isolated_home))
    mp.setenv("USERPROFILE", str(isolated_home))
    mp.setenv("XDG_CONFIG_HOME", str(isolated_home / ".config"))
    mp.setenv("XDG_DATA_HOME", str(isolated_home / ".local" / "share"))
    mp.setenv("APPDATA", str(isolated_home / "AppData" / "Roaming"))
    # Ambient overrides on the developer machine must not leak into tests.
    mp.delenv("IMMICH_GO_GUI_CONFIG", raising=False)
    mp.delenv("IMMICH_GO_GUI_API_KEY", raising=False)
    mp.delenv("IMMICH_GO_GUI_ADMIN_API_KEY", raising=False)

    previous_backend = keyring.get_keyring()
    keyring.set_keyring(InMemoryKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous_backend)
        mp.undo()


@pytest.fixture
def fake_keyring(_isolated_environment):
    """Returns the active in-memory keyring backend."""
    backend = keyring.get_keyring()
    assert isinstance(backend, InMemoryKeyring)
    return backend


@pytest.fixture(autouse=True)
def _fresh_keyring_store():
    """Clears the in-memory keyring before each test."""
    backend = keyring.get_keyring()
    if isinstance(backend, InMemoryKeyring):
        backend.store.clear()
    yield
