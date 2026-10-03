import pytest

from app import config, envfile


@pytest.mark.parametrize("raw, want", [
    ("plain", "plain"),
    ("  spaced  ", "spaced"),
    ('"two words"', "two words"),
    ('"say \\"hi\\""', 'say "hi"'),
    ('"back\\\\slash"', "back\\slash"),
    ("'single # kept'", "single # kept"),
    ("pa#ss", "pa#ss"),                 # a bare # is part of an unquoted value (passwords)
    ("value # comment", "value"),       # " #" starts a comment
    ('"unterminated', "unterminated"),
    ("", ""),
])
def test_value_parsing(raw, want):
    assert envfile._value(raw) == want


def test_read_handles_bom_comments_export_and_case(tmp_path):
    p = tmp_path / ".env"
    p.write_text("﻿# comment\n\nexport WORKDAY_EMAIL=me@example.com\nworkday_password = 's3cr#t'\n"
                 "not a line\n1BAD=x\n", encoding="utf-8")
    assert envfile.read(p) == {"WORKDAY_EMAIL": "me@example.com", "WORKDAY_PASSWORD": "s3cr#t"}
    assert envfile.read(tmp_path / "missing.env") == {}


def test_tenant_prefix():
    assert envfile.tenant_prefix("nvidia") == "NVIDIA"
    assert envfile.tenant_prefix("my-co.x") == "MY_CO_X"
    assert envfile.tenant_prefix(None) == ""


def test_account_for_prefers_company_override():
    config.ENV_FILE.write_text("WORKDAY_EMAIL=me@example.com\nWORKDAY_PASSWORD=general\n"
                               "NVIDIA_WORKDAY_PASSWORD=special\n", encoding="utf-8")
    assert envfile.account_for("nvidia") == {"email": "me@example.com", "password": "special"}
    assert envfile.account_for("intel") == {"email": "me@example.com", "password": "general"}


def test_status_never_contains_the_password():
    config.ENV_FILE.write_text("WORKDAY_EMAIL=me@example.com\nWORKDAY_PASSWORD=hunter2\n"
                               "ACME_WORKDAY_EMAIL=other@example.com\n", encoding="utf-8")
    st = envfile.status()
    assert st["email"] == "me@example.com" and st["password_set"] is True
    assert st["company_overrides"] == ["ACME"]
    assert "hunter2" not in repr(st)


def test_fresh_home_gets_the_template():
    text = config.ENV_FILE.read_text(encoding="utf-8")
    assert "WORKDAY_EMAIL=" in text and "WORKDAY_PASSWORD=" in text
    assert envfile.account_for("acme") == {"email": "", "password": ""}


# ---------------------------------------------------------------- OS keychain
from tests.conftest import KEYCHAIN  # noqa: E402


def _env(text):
    config.ENV_FILE.write_text(text, encoding="utf-8")


def test_keychain_password_wins_over_env_and_company_over_general():
    _env("WORKDAY_EMAIL=me@example.com\nWORKDAY_PASSWORD=from-env\nNVIDIA_WORKDAY_PASSWORD=nv-env\n")
    assert envfile.account_for("acme")["password"] == "from-env"
    envfile.store_password("from-keychain")
    assert envfile.account_for("acme")["password"] == "from-keychain"
    assert envfile.account_for("nvidia")["password"] == "nv-env"      # a company's own .env line beats the general one
    envfile.store_password("nv-keychain", "NVIDIA")
    assert envfile.account_for("nvidia")["password"] == "nv-keychain"
    assert (envfile.SERVICE, "NVIDIA_WORKDAY_PASSWORD") in KEYCHAIN.store
    assert envfile.SERVICE == f"job-agent/{config.HOME_ID}"


def test_store_and_delete():
    envfile.store_password("pw")
    st = envfile.status()
    assert st["password_set"] and st["password_in_keychain"] and not st["password_in_env"] and st["keychain"]
    envfile.store_password("")
    assert not envfile.status()["password_set"]
    assert envfile.account_for("acme")["password"] == ""
    envfile.store_password("")  # deleting twice is fine


def test_company_names_are_checked():
    with pytest.raises(ValueError):
        envfile.store_password("pw", "bad name!")
    envfile.store_password("pw", "my-co")
    assert envfile.status()["company_overrides"] == ["MY_CO"]


def test_move_env_passwords_to_keychain_blanks_them_and_keeps_comments():
    # bytes, so the test controls the line endings exactly (one CRLF line in an LF file)
    config.ENV_FILE.write_bytes(b"# my notes\nWORKDAY_EMAIL=me@example.com\nexport WORKDAY_PASSWORD='p#ss word'\r\n"
                                b"ACME_WORKDAY_PASSWORD=acme-pw\nOTHER=1\n")
    moved = envfile.move_env_passwords_to_keychain()
    assert sorted(moved) == ["ACME_WORKDAY_PASSWORD", "WORKDAY_PASSWORD"]
    raw = config.ENV_FILE.read_bytes()
    assert b"p#ss word" not in raw and b"acme-pw" not in raw
    assert raw.startswith(b"# my notes\nWORKDAY_EMAIL=me@example.com\n# WORKDAY_PASSWORD is stored in the OS keychain")
    assert b"\nexport WORKDAY_PASSWORD=\r\n" in raw and raw.endswith(b"\nACME_WORKDAY_PASSWORD=\nOTHER=1\n")
    assert envfile.read() == {"WORKDAY_EMAIL": "me@example.com", "WORKDAY_PASSWORD": "", "ACME_WORKDAY_PASSWORD": "",
                              "OTHER": "1"}
    assert envfile.account_for("acme")["password"] == "acme-pw"
    assert envfile.account_for("globex")["password"] == "p#ss word"
    st = envfile.status()
    assert st["password_in_keychain"] and not st["password_in_env"] and st["env_passwords"] == []
    assert envfile.move_env_passwords_to_keychain() == []  # nothing left to move


def test_without_a_keychain_env_still_works(monkeypatch):
    monkeypatch.setattr(envfile, "_keyring", lambda: None)
    _env("WORKDAY_PASSWORD=from-env\n")
    assert envfile.account_for("acme")["password"] == "from-env"
    assert envfile.status()["keychain"] is False
    with pytest.raises(RuntimeError):
        envfile.store_password("pw")
    with pytest.raises(RuntimeError):
        envfile.move_env_passwords_to_keychain()


def test_status_never_contains_keychain_passwords():
    envfile.store_password("hunter2")
    envfile.store_password("hunter3", "ACME")
    assert "hunter" not in repr(envfile.status())
    assert config.KEYCHAIN_INDEX.read_text(encoding="utf-8").split() == ["ACME_WORKDAY_PASSWORD", "WORKDAY_PASSWORD"]
