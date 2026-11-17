import json
import os
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from giftgraph import cli
from giftgraph.models import Gift, GiftEvent, GiftPage, Peer
from giftgraph.store import Store


@pytest.fixture(autouse=True)
def isolated_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith("GIFTGRAPH_") or key in {"API_ID", "API_HASH"}:
            monkeypatch.delenv(key)


@pytest.fixture
def fake_source(tmp_path, monkeypatch):
    """A provider with no transport; any real Telegram access is a test failure."""
    (tmp_path / "userbot.session").touch()
    instances = []

    class Source:
        def __init__(
            self,
            session_path,
            api_id=None,
            api_hash=None,
            *,
            include_descriptions=False,
            request_delay=1.0,
        ):
            self.session = Path(session_path)
            self.include_descriptions = include_descriptions
            self.request_delay = request_delay
            self.member_calls = []
            self.gift_calls = []
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def resolve_peer(self, reference):
            if isinstance(reference, Peer):
                return reference
            if str(reference).startswith("-") or "chat" in str(reference).lower():
                return Peer(42, "channel", "Test chat", "test_chat")
            if str(reference).isdigit():
                return Peer(int(reference), username=None)
            return Peer(1, username="test_user")

        async def iter_members(self, chat, limit):
            self.member_calls.append((chat, limit))
            for user in (Peer(1, username="test_user"), Peer(2, username="sender"))[:limit]:
                yield user

        async def fetch_gifts(self, peer, offset, limit):
            self.gift_calls.append((peer, offset, limit))
            if peer.id != 1:
                return GiftPage()
            gift = Gift("regular:101", "regular", "101", title="Heart", stars=25)
            note = "Спасибо за помощь!" if self.include_descriptions else None
            event = GiftEvent(
                "event:1", gift.key, "user:2", "user:1", 1794787200, "user:1", description=note
            )
            return GiftPage(
                gifts=(gift,), events=(event,), peers=(Peer(2, username="sender"),), item_count=1
            )

    monkeypatch.setattr("giftgraph.telegram.TelegramSource", Source)
    return instances


def test_demo_export_status_resume_work_without_telegram_credentials(tmp_path, capsys):
    db = str(tmp_path / "demo.db")
    output = str(tmp_path / "demo.json")
    assert cli.main(["demo", "--db", db, "--max-pages", "1", "--page-size", "1"]) == 0
    assert cli.main(["status", "--db", db]) == 0
    capsys.readouterr()
    for _ in range(5):
        assert cli.main(["resume", "--db", db]) == 0
    assert cli.main(["export", "--db", db, "--out", output]) == 0
    graph = json.loads(Path(output).read_text())
    assert graph["metadata"]["source"] == "demo"
    assert graph["metadata"]["status"] == "complete"
    assert len(graph["nodes"]) == 3
    assert len(graph["edges"]) == 4


@pytest.mark.parametrize(
    "arguments",
    [
        ["crawl"],
        ["crawl", "--chat", "@test_chat", "--user", "@test_user"],
        ["crawl", "--user", "1", "--user", "2"],
        ["crawl", "--chat", "@one", "--chat", "@two"],
    ],
)
def test_crawl_requires_one_target_exactly_once(arguments):
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["bot-info"],
        ["interactive"],
        ["crawl", "--user", "1", "--backend", "bot"],
        ["crawl", "--user", "1", "--token-file", "token.txt"],
    ],
)
def test_bot_workflows_are_removed(arguments):
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2


def test_crawl_defaults_are_saved_and_username_names_the_export(fake_source):
    assert cli.main(["crawl", "--user", "@test_user", "--delay", "0"]) == 0
    output = Path("outputs/test_user.graphml")
    assert output.is_file()
    assert ET.parse(output).getroot() is not None
    with Store("data/gifts.db") as store:
        config = store.config(store.get_run()["id"])
        assert (config.max_depth, config.max_nodes, config.max_gifts_per_profile) == (3, 3000, 1000)
        assert (config.max_pages, config.page_size) == (2000, 100)
    assert len(fake_source) == 1
    assert fake_source[0].session.name == "userbot.session"
    assert not fake_source[0].include_descriptions


def test_user_without_username_uses_numeric_id_for_default_export(fake_source):
    assert cli.main(["crawl", "--user", "12345", "--depth", "0"]) == 0
    assert Path("outputs/12345.graphml").is_file()


def test_chat_limits_seeds_and_uses_resolved_chat_name(fake_source):
    assert cli.main(["crawl", "--chat", "-1000000000042", "--seed-limit", "1", "--depth", "0"]) == 0
    assert fake_source[0].member_calls[0][1] == 1
    assert Path("outputs/test_chat.graphml").is_file()
    with Store("data/gifts.db") as store:
        assert store.stats(store.get_run()["id"]).nodes == 1


def test_explicit_export_and_description_sidecar(fake_source):
    assert (
        cli.main(
            [
                "crawl",
                "--user",
                "@test_user",
                "--depth",
                "1",
                "--delay",
                "0",
                "--export",
                "custom/graph.json",
                "--gift_description",
            ]
        )
        == 0
    )
    assert fake_source[0].include_descriptions
    graph = json.loads(Path("custom/graph.json").read_text())
    assert graph["nodes"][0]["user_id"] == "1"
    assert graph["edges"][0]["gift_id"] == "101"
    sidecar = json.loads(Path("custom/graph.gift_descriptions.json").read_text())
    assert sidecar == {"event:1": "Спасибо за помощь!"}
    assert "Спасибо" not in Path("custom/graph.json").read_text()


def test_default_collection_creates_no_description_file(fake_source):
    assert cli.main(["crawl", "--user", "@test_user", "--delay", "0"]) == 0
    assert list(Path("outputs").glob("*.gift_descriptions.json")) == []


def test_single_session_in_working_directory_is_discovered(fake_source):
    Path("userbot.session").rename("another.session")
    assert cli.main(["crawl", "--user", "@test_user", "--depth", "0"]) == 0
    assert fake_source[0].session.name == "another.session"


def test_ambiguous_session_files_require_explicit_choice(fake_source, capsys):
    Path("userbot.session").unlink()
    Path("one.session").touch()
    Path("two.session").touch()
    assert cli.main(["crawl", "--user", "@test_user", "--depth", "0"]) == 1
    assert "--session" in capsys.readouterr().err
    assert fake_source == []


def test_explicit_session_has_priority_over_discovery(fake_source):
    Path("selected.session").touch()
    assert (
        cli.main(["crawl", "--user", "@test_user", "--session", "selected.session", "--depth", "0"])
        == 0
    )
    assert fake_source[0].session.name == "selected.session"


def test_missing_session_is_actionable_and_does_not_authorize(capsys):
    assert cli.main(["crawl", "--user", "@test_user", "--session", "missing.session"]) == 1
    assert "session" in capsys.readouterr().err


def test_resume_reuses_saved_export_path_and_description_preference(fake_source):
    assert (
        cli.main(
            [
                "crawl",
                "--user",
                "@test_user",
                "--max-pages",
                "1",
                "--delay",
                "0",
                "--export",
                "custom/checkpoint.json",
                "--gift-description",
            ]
        )
        == 0
    )
    assert len(fake_source) == 1
    assert cli.main(["resume"]) == 0
    assert len(fake_source) == 2
    assert fake_source[-1].include_descriptions
    graph = json.loads(Path("custom/checkpoint.json").read_text())
    assert graph["metadata"]["status"] == "complete"
    assert Path("custom/checkpoint.gift_descriptions.json").is_file()


def test_invalid_limit_fails_before_creating_source(fake_source):
    assert cli.main(["crawl", "--user", "@test_user", "--max-gifts", "0"]) == 1
    assert fake_source == []


def test_invalid_export_suffix_fails_before_source(fake_source):
    assert cli.main(["crawl", "--user", "@test_user", "--export", "graph.txt"]) == 1
    assert fake_source == []


def test_description_sidecar_collision_fails_before_source(fake_source, capsys):
    assert (
        cli.main(
            [
                "crawl",
                "--user",
                "@test_user",
                "--gift_description",
                "--export",
                "outputs/x.graphml",
                "--export",
                "outputs/x.gift_descriptions.json",
            ]
        )
        == 1
    )
    assert "совпадает" in capsys.readouterr().err
    assert fake_source == []


def test_status_missing_database_does_not_create_one(tmp_path):
    db = tmp_path / "missing.db"
    assert cli.main(["status", "--db", str(db)]) == 1
    assert not db.exists()
