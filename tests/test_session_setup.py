"""Exercise the setup helper with local fixtures; never authorize in Telegram."""

import builtins
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "generate_session.py"
FAKE_HASH = "1234567890abcdef1234567890abcdef"


@pytest.fixture
def setup_script(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("giftgraph_session_setup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PROJECT_DIR", tmp_path)
    monkeypatch.delenv("API_ID", raising=False)
    monkeypatch.delenv("API_HASH", raising=False)
    return module


@pytest.fixture
def fake_client(monkeypatch):
    clients = []

    class Client:
        def __init__(self, name, **kwargs):
            self.name = name
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            self.disconnected = False
            self.is_connected = False
            clients.append(self)

        async def start(self, *, use_qr):
            assert use_qr is True
            self.started = True
            self.is_connected = True

        async def stop(self):
            self.stopped = True
            self.is_connected = False

        async def disconnect(self):
            self.disconnected = True
            self.is_connected = False

    monkeypatch.setitem(sys.modules, "pyrogram", SimpleNamespace(Client=Client))
    monkeypatch.setitem(sys.modules, "qrcode", SimpleNamespace())
    return clients


def credentials(path):
    path.write_text(f"API_ID=12345\nAPI_HASH={FAKE_HASH}\n", encoding="utf-8")


def ready_session(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions(user_id,is_bot,test_mode,auth_key,api_id,dc_id,"
            "server_address,port)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
            (123, 0, 0, bytes(256), 12345, 2, "149.154.167.50", 443),
        )


def test_help_does_not_import_client_or_read_credentials(setup_script, monkeypatch, capsys):
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        assert not name.startswith(("pyrogram", "qrcode", "giftgraph"))
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(SystemExit) as error:
        setup_script.main(["--help"])
    assert error.value.code == 0
    assert "--env-file" in capsys.readouterr().out


def test_project_env_is_used_from_another_working_directory(
    setup_script, tmp_path, monkeypatch, fake_client, capsys
):
    credentials(tmp_path / ".env")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("API_ID=wrong\nAPI_HASH=wrong\n")
    monkeypatch.chdir(elsewhere)
    assert setup_script.main([]) == 0
    client = fake_client[0]
    assert client.name == "userbot"
    assert client.kwargs == {
        "api_id": 12345,
        "api_hash": FAKE_HASH,
        "workdir": str(tmp_path),
        "no_updates": True,
    }
    assert client.started and client.stopped
    output = capsys.readouterr()
    assert FAKE_HASH not in output.out + output.err
    assert "API_ID" not in os.environ and "API_HASH" not in os.environ


def test_explicit_env_file_and_shell_environment_precedence(
    setup_script, tmp_path, monkeypatch, fake_client
):
    credentials(tmp_path / "custom.env")
    monkeypatch.setenv("API_ID", "54321")
    assert setup_script.main(["--env-file", str(tmp_path / "custom.env")]) == 0
    assert fake_client[0].kwargs["api_id"] == 54321
    assert os.environ["API_ID"] == "54321"
    assert "API_HASH" not in os.environ


def test_missing_default_env_can_use_environment(setup_script, monkeypatch, fake_client):
    monkeypatch.setenv("API_ID", "12345")
    monkeypatch.setenv("API_HASH", FAKE_HASH)
    assert setup_script.main([]) == 0
    assert fake_client[0].started


def test_missing_default_env_does_not_load_another_cwd_env(
    setup_script, tmp_path, monkeypatch, fake_client, capsys
):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    credentials(elsewhere / ".env")
    monkeypatch.chdir(elsewhere)
    assert setup_script.main([]) == 1
    assert fake_client == []
    assert "API_ID" in capsys.readouterr().err


def test_missing_explicit_env_errors_before_authentication(setup_script, fake_client, capsys):
    assert setup_script.main(["--env-file", "missing.env"]) == 1
    assert fake_client == []
    assert "missing.env" in capsys.readouterr().err


@pytest.mark.parametrize(
    "api_id,api_hash,expected",
    [
        ("", "", "API_ID"),
        ("wrong", FAKE_HASH, "API_ID"),
        ("0", FAKE_HASH, "API_ID"),
        ("-12", FAKE_HASH, "API_ID"),
        ("123", "fake-sensitive-not-a-hash", "API_HASH"),
    ],
)
def test_invalid_credentials_fail_before_client_creation(
    setup_script, monkeypatch, fake_client, capsys, api_id, api_hash, expected
):
    monkeypatch.setenv("API_ID", api_id)
    monkeypatch.setenv("API_HASH", api_hash)
    assert setup_script.main([]) == 1
    assert fake_client == []
    output = capsys.readouterr()
    assert expected in output.err
    assert "fake-sensitive-not-a-hash" not in output.out + output.err


def test_existing_ready_session_needs_no_credentials_or_client(setup_script, tmp_path, fake_client):
    ready_session(tmp_path / "userbot.session")
    assert setup_script.main([]) == 0
    assert fake_client == []


def test_existing_invalid_session_is_not_replaced_or_authorized(
    setup_script, tmp_path, fake_client, capsys
):
    path = tmp_path / "userbot.session"
    path.write_bytes(b"not an sqlite database")
    assert setup_script.main([]) == 1
    assert path.read_bytes() == b"not an sqlite database"
    assert fake_client == []
    assert "повреждён" in capsys.readouterr().err


def test_missing_qr_extra_has_install_message_and_never_starts_client(
    setup_script, tmp_path, monkeypatch, fake_client, capsys
):
    credentials(tmp_path / ".env")
    monkeypatch.setitem(sys.modules, "qrcode", None)
    assert setup_script.main([]) == 1
    assert fake_client == []
    assert 'python -m pip install ".[qr]"' in capsys.readouterr().err


@pytest.mark.parametrize("failure", [ConnectionError, RuntimeError, ValueError])
def test_authorization_failure_is_redacted_and_connection_is_closed(
    setup_script, tmp_path, monkeypatch, fake_client, capsys, failure
):
    credentials(tmp_path / ".env")
    client_class = sys.modules["pyrogram"].Client

    async def failing_start(self, *, use_qr):
        self.is_connected = True
        raise failure("fake-sensitive-phone-and-hash")

    monkeypatch.setattr(client_class, "start", failing_start)
    assert setup_script.main([]) == 1
    assert fake_client[0].disconnected
    output = capsys.readouterr()
    assert failure.__name__ in output.err
    assert "fake-sensitive-phone-and-hash" not in output.out + output.err
    assert "API_HASH" not in os.environ
