"""
`common.secrets` (systemd credentials, fail-closed in production) and `common.redact` (no secret in a log line).
"""

import logging

import pytest

from hpldn.backend.common import redact, secrets


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    """A $CREDENTIALS_DIRECTORY like systemd's /run/credentials/<unit>/."""
    directory = tmp_path / "credentials"
    directory.mkdir()
    monkeypatch.setenv(secrets.CREDENTIALS_DIRECTORY_ENV, str(directory))
    return directory


def test_a_credential_is_read_from_the_unit_directory(credentials, monkeypatch):
    monkeypatch.setenv(secrets.ENVIRONMENT_ENV, "production")
    monkeypatch.delenv("HPLDN_TEST_SECRET", raising=False)
    (credentials / "api_key").write_text("from-the-credential-0123\n")
    assert secrets.read_secret("api_key", "HPLDN_TEST_SECRET", required=True) == "from-the-credential-0123"


def test_production_never_reads_the_environment(credentials, monkeypatch):
    monkeypatch.setenv(secrets.ENVIRONMENT_ENV, "production")
    monkeypatch.setenv("HPLDN_TEST_SECRET", "in-the-environment-0123")  # gitleaks:allow (public fingerprint or test fixture)
    with pytest.raises(secrets.SecretError, match="set in the environment"):
        secrets.read_secret("api_key", "HPLDN_TEST_SECRET")


def test_production_fails_closed_without_the_credential(credentials, monkeypatch):
    monkeypatch.setenv(secrets.ENVIRONMENT_ENV, "production")
    monkeypatch.delenv("HPLDN_TEST_SECRET", raising=False)
    with pytest.raises(secrets.SecretError, match="LoadCredentialEncrypted"):
        secrets.read_secret("pass_secret", "HPLDN_TEST_SECRET", required=True)
    assert secrets.read_secret("pass_secret", "HPLDN_TEST_SECRET") == ""


def test_off_the_host_the_environment_is_the_fallback(monkeypatch):
    monkeypatch.delenv(secrets.ENVIRONMENT_ENV, raising=False)
    monkeypatch.delenv(secrets.CREDENTIALS_DIRECTORY_ENV, raising=False)
    monkeypatch.setenv("HPLDN_TEST_SECRET", "develop-env-value-0123")
    assert secrets.read_secret("api_key", "HPLDN_TEST_SECRET") == "develop-env-value-0123"


def test_a_credential_name_is_checked():
    with pytest.raises(secrets.SecretError, match="not a credential name"):
        secrets.read_secret("../etc/shadow", "X")


def test_every_secret_read_is_masked_in_logs(monkeypatch):
    monkeypatch.delenv(secrets.ENVIRONMENT_ENV, raising=False)
    monkeypatch.delenv(secrets.CREDENTIALS_DIRECTORY_ENV, raising=False)
    monkeypatch.setenv("HPLDN_TEST_SECRET", "hunter2-but-longer")
    value = secrets.read_secret("whatever", "HPLDN_TEST_SECRET")
    assert redact.redact(f"connecting with {value} now") == "connecting with *** now"


@pytest.mark.parametrize(
    ("line", "leaked"),
    [
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        ('headers={"X-API-Key": "k3y-value"}', "k3y-value"),
        ("x-hpldn-pass=payload.signature", "payload.signature"),
        ("token 0123456789abcdef0123456789abcdef01 seen", "0123456789abcdef0123456789abcdef01"),
        ("blob dGhpcyBpcyBhIGxvbmcgYmFzZTY0IHN0cmluZyB0aGF0IGxlYWtz== end", "dGhpcyBpcyBhIGxvbmcgYmFzZTY0IHN0cmluZyB0aGF0IGxlYWtz"),
    ],
)
def test_token_shapes_are_masked_whatever_their_value(line, leaked):
    masked = redact.redact(line)
    assert leaked not in masked
    assert redact.MASK in masked


def test_ordinary_log_lines_are_left_alone():
    line = '203.0.113.9 "GET /addresses/search?q[7]&limit[1]" 200 3.1ms rid=5f0c2a9e41d84b7c'
    assert redact.redact(line) == line


def test_the_filter_redacts_message_args_and_traceback(caplog, monkeypatch):
    monkeypatch.delenv(secrets.ENVIRONMENT_ENV, raising=False)
    secrets.register_secret("s3cr3t-in-an-exception")
    logger = logging.getLogger("hpldn.test.redact")
    logger.addFilter(redact.RedactFilter())
    caplog.set_level(logging.INFO, logger="hpldn.test.redact")
    try:
        raise RuntimeError("boom with s3cr3t-in-an-exception")
    except RuntimeError:
        logger.exception("failed for %s", "s3cr3t-in-an-exception")
    record = caplog.records[-1]
    assert "s3cr3t" not in record.getMessage()
    assert "s3cr3t" not in (record.exc_text or "")
    assert "boom with ***" in record.exc_text


def test_install_covers_handlers_added_later(capsys, monkeypatch):
    secrets.register_secret("late-handler-secret-value")
    redact.install()
    logger = logging.getLogger("hpldn.test.redact.late")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("value=%s", "late-handler-secret-value")
    finally:
        logger.removeHandler(handler)
    assert "late-handler-secret-value" not in capsys.readouterr().err
