import os
from collections.abc import Callable
from typing import Any

import pytest

from atlas.kernel.config import Settings, build_settings

# Variables Settings/InitSettings read. Cleared for every test so a developer's or CI's
# environment can never leak into a test.
_CONFIG_PREFIXES = ("ATLAS_",)
_CONFIG_NAMES = ("SECRET_KEY", "SECRET_KEY_PREVIOUS", "DATABASE_URL")

STRONG_KEY = "k3Y-" + "".join(chr(c) for c in range(ord("A"), ord("Z") + 1)) + "abcdefghijklmnop"
VALID: dict[str, str] = {
    "ATLAS_ENV": "test",
    "SECRET_KEY": STRONG_KEY,
    "DATABASE_URL": "postgresql+psycopg://atlas_app:Qm4tW9zLr2Xv@postgres:5432/atlas",
    "ATLAS_PUBLIC_URL": "https://atlas.home.arpa",
    "ATLAS_OIDC_ISSUER": "https://id.home.arpa/oauth2/openid/atlas",
    "ATLAS_OIDC_CLIENT_SECRET": "oidc-client-Zr8pQ2vN6",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(_CONFIG_PREFIXES) or name in _CONFIG_NAMES:
            monkeypatch.delenv(name)


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Valid test Settings; pass overrides by variable name, None to drop one."""

    def make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {**VALID, **overrides}
        values = {k: v for k, v in values.items() if v is not None}
        return build_settings(_env_file=None, **values)

    return make
