"""Tests for the .env reader/writer that preserves comments."""
from __future__ import annotations

from pathlib import Path

from bibi_signal.env_io import read_env, update_env


def test_read_env_returns_empty_for_missing_file(tmp_path: Path):
    assert read_env(tmp_path / "nope.env") == {}


def test_read_env_strips_quotes_and_whitespace(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        '# comment\n'
        'KEY1=value1\n'
        'KEY2 = "quoted value"\n'
        "KEY3='single quoted'\n"
        '\n'
        'KEY4=  spaced  \n'
    )
    env = read_env(p)
    assert env["KEY1"] == "value1"
    assert env["KEY2"] == "quoted value"
    assert env["KEY3"] == "single quoted"
    assert env["KEY4"] == "spaced"


def test_update_env_preserves_comments_and_order(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# Header comment\n"
        "FOO=oldfoo\n"
        "\n"
        "# Another comment\n"
        "BAR=oldbar\n"
    )
    update_env(p, {"FOO": "newfoo"})
    text = p.read_text()
    assert "# Header comment" in text
    assert "# Another comment" in text
    assert "FOO=newfoo" in text
    assert "BAR=oldbar" in text  # untouched


def test_update_env_appends_new_keys(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("EXISTING=1\n")
    update_env(p, {"EXISTING": "2", "NEW_KEY": "abc"})
    env = read_env(p)
    assert env["EXISTING"] == "2"
    assert env["NEW_KEY"] == "abc"


def test_update_env_creates_file_if_missing(tmp_path: Path):
    p = tmp_path / ".env"
    update_env(p, {"FOO": "bar"})
    assert p.exists()
    assert read_env(p)["FOO"] == "bar"


def test_update_env_handles_empty_value(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("FOO=oldval\n")
    update_env(p, {"FOO": ""})
    # Empty value should still be readable as empty string
    text = p.read_text()
    assert "FOO=" in text
