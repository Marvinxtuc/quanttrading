"""Kraken trade keys from the environment or `.env`. Never from the repo."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# Full-string matches only. Short fragments are not searched inside the key:
# a real Kraken secret is long base64 and can contain arbitrary substrings.
_EXACT_PLACEHOLDERS = frozenset(
    {
        "changeme",
        "change-me",
        "change_me",
        "placeholder",
        "todo",
        "example",
        "secret",
        "password",
        "xxx",
        "xxxx",
        "xxxxx",
        "redacted",
        "dummy",
        "fake",
        "none",
        "null",
        "test",
        "testing",
        "sample",
        "default",
        "insert",
        "kraken_api_key",
        "kraken_api_secret",
        "your_api_key",
        "your_api_secret",
        "your-api-key",
        "your-api-secret",
        "api_key",
        "api_secret",
        "apikey",
        "apisecret",
    }
)

# Long markers that documentation uses and that real base64 keys do not contain.
_CONTAINS_PLACEHOLDERS = (
    "placeholder",
    "changeme",
    "your_api",
    "your-api",
    "paste-here",
    "paste_here",
    "insert-key",
    "insert_key",
    "api_key_here",
    "api-key-here",
    "api_secret_here",
    "example_key",
    "example-key",
    "not-a-real",
)

_MIN_SECRET_LENGTH = 20


class KrakenCredentialsError(RuntimeError):
    """Keys are missing or not safe to send. The message never includes the values."""


def looks_like_placeholder(value: str) -> bool:
    text = value.strip()
    if not text or len(text) < _MIN_SECRET_LENGTH:
        return True
    if any(ch.isspace() for ch in text):
        return True
    folded = text.casefold()
    if folded in _EXACT_PLACEHOLDERS:
        return True
    return any(marker in folded for marker in _CONTAINS_PLACEHOLDERS)


def load_kraken_credentials(
    environ: Mapping[str, str] | None = None,
    env_file: Path | str | None = Path(".env"),
) -> tuple[str, str]:
    """Return ``(KRAKEN_API_KEY, KRAKEN_API_SECRET)``.

    Process environment wins over ``env_file``. Empty variables fall through to
    the file. Raises ``KrakenCredentialsError`` when either value is missing or
    looks like a placeholder. The exception text does not include the values.
    """
    source = os.environ if environ is None else environ
    file_values = _read_env_file(env_file)
    key = _pick(source, file_values, "KRAKEN_API_KEY")
    secret = _pick(source, file_values, "KRAKEN_API_SECRET")
    bad: list[str] = []
    if looks_like_placeholder(key):
        bad.append("KRAKEN_API_KEY")
    if looks_like_placeholder(secret):
        bad.append("KRAKEN_API_SECRET")
    if not bad and key == secret:
        bad.extend(("KRAKEN_API_KEY", "KRAKEN_API_SECRET"))
    if bad:
        names = ", ".join(dict.fromkeys(bad))
        raise KrakenCredentialsError(
            f"refusing to start live: {names} missing or look like placeholders. "
            "Set trade-only Kraken keys in the environment or .env (gitignored). "
            "Do not commit them. Leave withdraw permission disabled on the key."
        )
    return key, secret


def _pick(environ: Mapping[str, str], file_values: Mapping[str, str], name: str) -> str:
    raw = environ.get(name)
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    file_raw = file_values.get(name)
    if file_raw is not None and str(file_raw).strip():
        return str(file_raw).strip()
    return "" if raw is None else str(raw).strip()


def _read_env_file(env_file: Path | str | None) -> dict[str, str]:
    if env_file is None:
        return {}
    path = Path(env_file)
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    loaded = dotenv_values(path)
    values: dict[str, str] = {}
    for key, value in loaded.items():
        if key and value is not None and str(value).strip():
            values[str(key)] = str(value).strip()
    return values
