"""Settings resolution, and the first place a secret enters the process.

Precedence is environment > `~/.config/shelfwarden/config.toml` > defaults
(practices §9). Secrets come from the environment or that file and **never** from
a CLI argument: argv lands in shell history and in every process listing on the
machine.

The token is the reason this module has a custom `__repr__` and a `redact()`
helper. An export writes three files and a census; a token that leaked into any
of them would sit in a dataset directory with a long retention period, which is
the same failure practices §3.4 guards the evidence store against, arriving a
phase earlier.
"""

import os
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

REDACTED = "<redacted>"

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "shelfwarden" / "config.toml"
DEFAULT_EXPORT_DIR = Path("datasets/exports")

# `PLEX_URL` / `PLEX_TOKEN` are accepted because scripts/capture_fixtures.py
# already established them and asking for a second pair of names for the same
# server would be gratuitous. The prefixed forms win where both are set.
_PLEX_URL_KEYS = ("SHELFWARDEN_PLEX_URL", "PLEX_URL")
_PLEX_TOKEN_KEYS = ("SHELFWARDEN_PLEX_TOKEN", "PLEX_TOKEN")
_EXPORT_DIR_KEYS = ("SHELFWARDEN_EXPORT_DIR",)


class ConfigError(Exception):
    """Configuration is missing or unusable. Always names what to set."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the CLI needs that is not a command-line flag."""

    plex_url: str | None = None
    plex_token: str | None = field(default=None, repr=False)
    export_dir: Path = DEFAULT_EXPORT_DIR

    def __repr__(self) -> str:
        """Render without the token.

        `field(repr=False)` already hides it, but a dataclass repr is one
        refactor away from being regenerated with the default. Spelling it out
        makes the omission the visible intent rather than a flag someone has to
        notice.
        """
        held = "set" if self.plex_token else "unset"
        return (
            f"Settings(plex_url={self.plex_url!r}, plex_token=<{held}>, "
            f"export_dir={self.export_dir!r})"
        )

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every value that must never appear in output. Fed to `redact`."""
        return tuple(value for value in (self.plex_token,) if value)


def _first(env: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = env.get(key)
        if value:
            return value
    return None


def _read_toml(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    plex = data.get("plex")
    return plex if isinstance(plex, Mapping) else {}


def load_settings(
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> Settings:
    """Resolve settings. Environment first, then the config file, then defaults."""
    env = os.environ if env is None else env
    path = DEFAULT_CONFIG_PATH if config_path is None else config_path
    filed = _read_toml(path)

    def pick(keys: tuple[str, ...], toml_key: str) -> str | None:
        value = _first(env, keys)
        if value:
            return value
        filed_value = filed.get(toml_key)
        return filed_value if isinstance(filed_value, str) and filed_value else None

    export_dir = pick(_EXPORT_DIR_KEYS, "export_dir")
    return Settings(
        plex_url=pick(_PLEX_URL_KEYS, "url"),
        plex_token=pick(_PLEX_TOKEN_KEYS, "token"),
        export_dir=Path(export_dir) if export_dir else DEFAULT_EXPORT_DIR,
    )


def require_plex(settings: Settings) -> tuple[str, str]:
    """Both halves of the Plex connection, or an error naming what to set.

    Modelled on the `LibraryError` rule: a failure the caller can fix must say
    how. "Configuration missing" is a dead end; naming the two variables is not.
    """
    missing = [
        name
        for name, value in (("PLEX_URL", settings.plex_url), ("PLEX_TOKEN", settings.plex_token))
        if not value
    ]
    if missing:
        raise ConfigError(
            f"{' and '.join(missing)} not set. Export SHELFWARDEN_PLEX_URL and "
            "SHELFWARDEN_PLEX_TOKEN (or PLEX_URL / PLEX_TOKEN) in the environment, "
            f"or put url/token under [plex] in {DEFAULT_CONFIG_PATH}. Never pass a "
            "token as a command-line argument."
        )
    assert settings.plex_url is not None and settings.plex_token is not None
    return settings.plex_url, settings.plex_token


def redact(text: str, secrets: tuple[str, ...]) -> str:
    """Replace every configured secret with `<redacted>`.

    Longest first, so a secret that contains another does not leave a fragment
    behind after the shorter one is replaced.
    """
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def iter_secret_hits(data: bytes, secrets: tuple[str, ...]) -> Iterator[str]:
    """Every configured secret present in `data`. Empty means clean.

    Used by the export's own self-check and by the test that asserts no output
    file carries a token -- practices §9 says assert it rather than trust review.
    """
    for secret in secrets:
        if secret and secret.encode("utf-8") in data:
            yield secret
