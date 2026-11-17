import os
from pathlib import Path

import pytest

from giftgraph import cli
from giftgraph.store import Store


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith("GIFTGRAPH_") or key in {"API_ID", "API_HASH"}:
            monkeypatch.delenv(key)


@pytest.fixture
def captured(monkeypatch):
    calls = []

    async def execute(args):
        calls.append((args, {key: os.environ.get(key) for key in ("API_ID", "API_HASH")}))

    monkeypatch.setattr(cli, "execute", execute)
    return calls


def test_env_supplies_limits_and_paths(tmp_path, captured):
    (tmp_path / ".env").write_text(
        "GIFTGRAPH_SESSION='sessions/my account.session'\n"
        "GIFTGRAPH_DB=data/custom.db\n"
        "GIFTGRAPH_OUTPUT_DIR=custom-outputs\n"
        "GIFTGRAPH_DEPTH=4\n"
        "GIFTGRAPH_SEED_LIMIT=25\n"
        "GIFTGRAPH_MAX_NODES=350\n"
        "GIFTGRAPH_MAX_GIFTS=150\n"
        "GIFTGRAPH_MAX_PAGES=400\n"
        "GIFTGRAPH_PAGE_SIZE=50\n"
        "GIFTGRAPH_DELAY=1.5\n",
        encoding="utf-8",
    )
    assert cli.main(["crawl", "--user", "@example"]) == 0
    args, _ = captured[-1]
    assert args.session == Path("sessions/my account.session")
    assert args.db == Path("data/custom.db")
    assert args.output_dir == Path("custom-outputs")
    assert (args.depth, args.seed_limit, args.max_nodes) == (4, 25, 350)
    assert (args.max_gifts, args.max_pages, args.page_size, args.delay) == (150, 400, 50, 1.5)


def test_flag_then_process_environment_then_file_precedence(tmp_path, monkeypatch, captured):
    (tmp_path / ".env").write_text("GIFTGRAPH_DEPTH=4\nGIFTGRAPH_MAX_NODES=300\n")
    monkeypatch.setenv("GIFTGRAPH_DEPTH", "5")
    assert cli.main(["crawl", "--user", "@example"]) == 0
    assert captured[-1][0].depth == 5
    assert captured[-1][0].max_nodes == 300
    assert cli.main(["crawl", "--user", "@example", "--depth", "6"]) == 0
    assert captured[-1][0].depth == 6
    assert os.environ["GIFTGRAPH_DEPTH"] == "5"
    assert "GIFTGRAPH_MAX_NODES" not in os.environ


@pytest.mark.parametrize(
    "arguments",
    [
        ["--env-file", "configs/custom.env", "crawl", "--user", "@example"],
        ["crawl", "--user", "@example", "--env-file", "configs/custom.env"],
    ],
)
def test_explicit_file_replaces_default_and_keeps_paths_relative_to_cwd(
    tmp_path, captured, arguments
):
    (tmp_path / ".env").write_text("GIFTGRAPH_DEPTH=9\nGIFTGRAPH_MAX_NODES=7\n")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/custom.env").write_text("GIFTGRAPH_DEPTH=3\nGIFTGRAPH_DB=local/graph.db\n")
    assert cli.main(arguments) == 0
    args, _ = captured[-1]
    assert args.depth == 3
    assert args.max_nodes == 3000
    assert args.db == Path("local/graph.db")


def test_missing_default_file_uses_builtin_defaults(captured):
    assert cli.main(["crawl", "--user", "@example"]) == 0
    args, _ = captured[-1]
    assert (args.depth, args.max_nodes, args.max_gifts, args.max_pages) == (3, 3000, 1000, 2000)
    assert args.delay == 1


def test_missing_explicit_file_fails_without_executing(capsys, captured):
    assert cli.main(["--env-file", "missing.env", "crawl", "--user", "@example"]) == 1
    assert "missing.env" in capsys.readouterr().err
    assert captured == []


def test_credentials_are_scoped_and_shell_values_are_preserved(tmp_path, monkeypatch, captured):
    (tmp_path / ".env").write_text("API_ID=12345\nAPI_HASH=fake-file-hash\n")
    monkeypatch.setenv("API_HASH", "fake-shell-hash")
    assert cli.main(["crawl", "--user", "@example"]) == 0
    assert captured[-1][1] == {"API_ID": "12345", "API_HASH": "fake-shell-hash"}
    assert "API_ID" not in os.environ
    assert os.environ["API_HASH"] == "fake-shell-hash"
    (tmp_path / ".env").unlink()
    assert cli.main(["crawl", "--user", "@example"]) == 0
    assert captured[-1][1]["API_ID"] is None


def test_env_is_restored_after_execution_failure(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("API_HASH=fake-file-hash\nGIFTGRAPH_DEPTH=4\n")

    async def failing_execute(args):
        assert os.environ["API_HASH"] == "fake-file-hash"
        raise ValueError("Example execution error")

    monkeypatch.setattr(cli, "execute", failing_execute)
    assert cli.main(["crawl", "--user", "@example"]) == 1
    assert "API_HASH" not in os.environ
    assert "GIFTGRAPH_DEPTH" not in os.environ


@pytest.mark.parametrize("key,value", [("DEPTH", "wrong"), ("MAX_NODES", "invalid")])
def test_invalid_env_defaults_fail_before_execution(tmp_path, captured, key, value):
    (tmp_path / ".env").write_text(f"GIFTGRAPH_{key}={value}\nAPI_HASH=fake-hash\n")
    with pytest.raises(SystemExit) as error:
        cli.main(["crawl", "--user", "@example"])
    assert error.value.code == 2
    assert captured == []
    assert "API_HASH" not in os.environ


def test_explicit_flag_overrides_invalid_env_default(tmp_path, captured):
    (tmp_path / ".env").write_text("GIFTGRAPH_DEPTH=wrong\nGIFTGRAPH_MAX_NODES=wrong\n")
    assert cli.main(["crawl", "--user", "@example", "--depth", "3", "--max-nodes", "250"]) == 0
    assert captured[-1][0].depth == 3
    assert captured[-1][0].max_nodes == 250


def test_empty_values_use_defaults_and_do_not_load_blank_credentials(tmp_path, captured):
    (tmp_path / ".env").write_text("GIFTGRAPH_DEPTH=\nGIFTGRAPH_DB=\nAPI_ID=\nAPI_HASH=\n")
    assert cli.main(["crawl", "--user", "@example"]) == 0
    args, credentials = captured[-1]
    assert args.depth == 3
    assert args.db == Path("data/gifts.db")
    assert credentials == {"API_ID": None, "API_HASH": None}


def test_shell_expressions_and_variable_references_remain_literal(tmp_path, captured):
    (tmp_path / ".env").write_text(
        "export GIFTGRAPH_SESSION='${HOME}/token # literal.txt'\n"
        "GIFTGRAPH_DB='$(touch injected).db'\n"
        "UNRELATED_VARIABLE=ignored\n"
    )
    assert cli.main(["crawl", "--user", "@example"]) == 0
    args, _ = captured[-1]
    assert args.session == Path("${HOME}/token # literal.txt")
    assert args.db == Path("$(touch injected).db")
    assert not (tmp_path / "injected").exists()
    assert "UNRELATED_VARIABLE" not in os.environ


def test_malformed_env_errors_without_disclosing_values(tmp_path, capsys, captured):
    (tmp_path / ".env").write_text('API_HASH="fake-sensitive-value\n')
    assert cli.main(["crawl", "--user", "@example"]) == 1
    output = capsys.readouterr()
    assert "fake-sensitive-value" not in output.out + output.err
    assert captured == []


def test_help_restores_environment_without_execution(tmp_path, capsys, captured):
    (tmp_path / ".env").write_text("GIFTGRAPH_DEPTH=3\nAPI_HASH=fake-hash\n")
    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])
    assert error.value.code == 0
    assert "--env-file" in capsys.readouterr().out
    assert captured == []
    assert "API_HASH" not in os.environ


def test_demo_preserves_independent_defaults_but_accepts_flags(tmp_path, captured):
    (tmp_path / ".env").write_text(
        "GIFTGRAPH_DEPTH=invalid\nGIFTGRAPH_MAX_NODES=1\nGIFTGRAPH_DELAY=100\n"
        "GIFTGRAPH_DB=production.db\n"
    )
    assert cli.main(["demo"]) == 0
    args, _ = captured[-1]
    assert (args.depth, args.max_nodes, args.delay, args.db) == (2, 500, 0, Path("data/demo.db"))
    assert cli.main(["demo", "--depth", "1", "--db", "test.db"]) == 0
    assert captured[-1][0].depth == 1
    assert captured[-1][0].db == Path("test.db")


def test_resume_keeps_saved_limits_and_receives_fresh_page_budget(tmp_path):
    db = tmp_path / "saved.db"
    assert cli.main(["demo", "--db", str(db), "--max-pages", "1", "--page-size", "1"]) == 0
    with Store(db) as store:
        run_id = store.get_run()["id"]
        original_config = store.config(run_id)
        original_pages = store.stats(run_id).pages
    (tmp_path / ".env").write_text(
        f"GIFTGRAPH_DB={db}\nGIFTGRAPH_DEPTH=0\nGIFTGRAPH_MAX_PAGES=99\n"
    )
    assert cli.main(["resume"]) == 0
    with Store(db) as store:
        assert store.config(run_id) == original_config
        assert store.stats(run_id).pages == original_pages + 1


@pytest.mark.parametrize(
    "value,expected", [("true", True), ("1", True), ("false", False), ("0", False)]
)
def test_description_boolean_from_env(tmp_path, captured, value, expected):
    (tmp_path / ".env").write_text(f"GIFTGRAPH_GIFT_DESCRIPTION={value}\n")
    assert cli.main(["crawl", "--user", "@example"]) == 0
    assert captured[-1][0].gift_description is expected


def test_description_flag_overrides_false_env(tmp_path, captured):
    (tmp_path / ".env").write_text("GIFTGRAPH_GIFT_DESCRIPTION=false\n")
    assert cli.main(["crawl", "--user", "@example", "--gift_description"]) == 0
    assert captured[-1][0].gift_description is True
