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
