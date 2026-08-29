"""Settings resolution, and the promise that a token never leaves the process.

The precedence tests are ordinary. The redaction tests are not: `config.py` is
the first module in the project that holds a secret, and step 0.4 writes four
files into a directory with a long retention period. A token that leaked into one
of them would sit there indefinitely, which is the failure practices §3.4 guards
the evidence store against, arriving a phase earlier.
"""

from pathlib import Path

import pytest

from shelfwarden.config import (
    DEFAULT_EXPORT_DIR,
    REDACTED,
    ConfigError,
    Settings,
    iter_secret_hits,
    load_settings,
    redact,
    require_plex,
)

TOKEN = "xxTOKENxx"


def write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


class TestPrecedence:
    """Environment > config file > defaults (practices §9)."""

    def test_the_environment_wins_over_the_config_file(self, tmp_path):
        path = write_toml(tmp_path, '[plex]\nurl = "http://from-file"\ntoken = "filed"\n')
        settings = load_settings(
            env={"SHELFWARDEN_PLEX_URL": "http://from-env", "SHELFWARDEN_PLEX_TOKEN": "envtok"},
            config_path=path,
        )
        assert settings.plex_url == "http://from-env"
        assert settings.plex_token == "envtok"

    def test_the_config_file_wins_over_the_defaults(self, tmp_path):
        path = write_toml(tmp_path, '[plex]\nurl = "http://from-file"\ntoken = "filed"\n')
        settings = load_settings(env={}, config_path=path)
        assert settings.plex_url == "http://from-file"
        assert settings.plex_token == "filed"

    def test_a_missing_config_file_is_not_an_error(self, tmp_path):
        settings = load_settings(env={}, config_path=tmp_path / "absent.toml")
        assert settings.plex_url is None
        assert settings.export_dir == DEFAULT_EXPORT_DIR

    def test_each_value_resolves_independently(self, tmp_path):
        """A url in the environment must not suppress a token in the file."""
        path = write_toml(tmp_path, '[plex]\ntoken = "filed"\n')
        settings = load_settings(env={"PLEX_URL": "http://from-env"}, config_path=path)
        assert settings.plex_url == "http://from-env"
        assert settings.plex_token == "filed"

    def test_the_prefixed_name_wins_over_the_bare_alias(self, tmp_path):
        """Both are accepted because capture_fixtures.py established the bare
        pair; where they disagree the project's own name is the specific one."""
        settings = load_settings(
            env={"SHELFWARDEN_PLEX_TOKEN": "ours", "PLEX_TOKEN": "theirs"},
            config_path=tmp_path / "absent.toml",
        )
        assert settings.plex_token == "ours"

    def test_an_empty_value_does_not_shadow_a_real_one(self, tmp_path):
        """An exported-but-blank variable is the shell's idea of unset, not ours."""
        path = write_toml(tmp_path, '[plex]\ntoken = "filed"\n')
        settings = load_settings(env={"SHELFWARDEN_PLEX_TOKEN": ""}, config_path=path)
        assert settings.plex_token == "filed"

    def test_the_export_directory_is_configurable(self, tmp_path):
        path = write_toml(tmp_path, '[plex]\nexport_dir = "/data/exports"\n')
        assert load_settings(env={}, config_path=path).export_dir == Path("/data/exports")
        assert load_settings(
            env={"SHELFWARDEN_EXPORT_DIR": "/other"}, config_path=path
        ).export_dir == Path("/other")

    def test_a_non_string_toml_value_falls_through_to_the_default(self, tmp_path):
        """A hand-edited config with `token = 12345` must not produce an int token."""
        path = write_toml(tmp_path, "[plex]\ntoken = 12345\n")
        assert load_settings(env={}, config_path=path).plex_token is None

    def test_broken_toml_names_the_file(self, tmp_path):
        path = write_toml(tmp_path, "[plex\nurl = ")
        with pytest.raises(ConfigError, match=str(path)):
            load_settings(env={}, config_path=path)


class TestTheTokenNeverRenders:
    def test_repr_does_not_carry_the_token(self):
        settings = Settings(plex_url="http://plex", plex_token=TOKEN)
        assert TOKEN not in repr(settings)
        assert "<set>" in repr(settings)

    def test_repr_distinguishes_set_from_unset(self):
        assert "<unset>" in repr(Settings(plex_url="http://plex"))

    def test_the_url_still_renders_because_it_is_not_a_secret(self):
        assert "http://plex" in repr(Settings(plex_url="http://plex", plex_token=TOKEN))

    def test_an_interpolated_settings_object_is_also_safe(self):
        """`f"{settings}"` takes __str__, which a dataclass routes to __repr__.
        Worth pinning: the leak-shaped mistake is a log line, not a debugger."""
        assert TOKEN not in f"{Settings(plex_token=TOKEN)}"


class TestSecrets:
    def test_secrets_lists_the_token_when_set(self):
        assert Settings(plex_token=TOKEN).secrets == (TOKEN,)

    def test_secrets_is_empty_when_nothing_is_configured(self):
        assert Settings().secrets == ()

    def test_redact_replaces_every_occurrence(self):
        text = f"GET http://plex/?X-Plex-Token={TOKEN} then {TOKEN}"
        assert redact(text, (TOKEN,)) == f"GET http://plex/?X-Plex-Token={REDACTED} then {REDACTED}"

    def test_a_secret_containing_another_leaves_no_fragment(self):
        """Replacing the short one first would leave `<redacted>tail` behind, which
        still carries half the secret and reads as though it were handled."""
        assert redact("abc123", ("abc", "abc123")) == REDACTED

    def test_redact_is_a_no_op_when_nothing_is_configured(self):
        assert redact("plain text", ()) == "plain text"

    def test_iter_secret_hits_finds_a_token_in_bytes(self):
        assert list(iter_secret_hits(f'{{"t":"{TOKEN}"}}'.encode(), (TOKEN,))) == [TOKEN]

    def test_iter_secret_hits_is_empty_on_clean_output(self):
        assert list(iter_secret_hits(b'{"t":"redacted"}', (TOKEN,))) == []

    def test_a_non_ascii_secret_is_matched_as_utf8(self):
        assert list(iter_secret_hits("Amélie".encode(), ("Amélie",))) == ["Amélie"]


class TestRequirePlex:
    def test_it_returns_both_halves(self):
        assert require_plex(Settings(plex_url="http://plex", plex_token=TOKEN)) == (
            "http://plex",
            TOKEN,
        )

    @pytest.mark.parametrize(
        ("settings", "expected"),
        [
            (Settings(), ("PLEX_URL", "PLEX_TOKEN")),
            (Settings(plex_url="http://plex"), ("PLEX_TOKEN",)),
            (Settings(plex_token=TOKEN), ("PLEX_URL",)),
        ],
    )
    def test_it_names_exactly_what_is_missing(self, settings, expected):
        """A correctable error that does not name a next action is a bug
        (CLAUDE.md working rules). "Configuration missing" is a dead end."""
        with pytest.raises(ConfigError) as caught:
            require_plex(settings)
        message = str(caught.value)
        for name in expected:
            assert name in message
        assert "SHELFWARDEN_PLEX_URL" in message

    def test_the_error_says_not_to_pass_a_token_on_the_command_line(self):
        """argv lands in shell history and in every process listing on the box."""
        with pytest.raises(ConfigError, match="command-line argument"):
            require_plex(Settings())

    def test_the_error_does_not_echo_the_half_that_was_set(self):
        with pytest.raises(ConfigError) as caught:
            require_plex(Settings(plex_token=TOKEN))
        assert TOKEN not in str(caught.value)
