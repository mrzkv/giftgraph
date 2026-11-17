"""Read-only user-session crawler and offline graph exports."""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .config import ENV_OPTIONS, environment_file
from .crawler import crawl
from .demo import DEMO_SEEDS, DemoSource
from .export import export_gift_descriptions, export_graph
from .models import CrawlConfig, PeerUnavailable
from .store import Store


@contextmanager
def database_lock(path: Path):
    """Prevent two CLI writers from advancing the same crawl simultaneously."""
    lock_path = Path(str(path.resolve()) + ".lockfile")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"База уже используется другим сборщиком: {path}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class SingleTarget(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} можно передать только один раз")
        if not values.strip():
            parser.error(f"{option_string} не может быть пустым")
        setattr(namespace, self.dest, values)


def _boolean(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError("GIFTGRAPH_GIFT_DESCRIPTION: ожидается true/false или 1/0")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="giftgraph",
        description="Граф видимых подарков Telegram; только чтение готовой сессии",
        allow_abbrev=False,
    )
    result.add_argument("--verbose", action="store_true")
    result.add_argument(
        "--env-file", type=Path, help="Файл конфигурации (по умолчанию .env в текущей папке)"
    )
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("crawl", "Начать обход одного чата или пользователя"),
        ("resume", "Продолжить сохранённый обход с прежними лимитами"),
        ("status", "Показать состояние обхода"),
        ("export", "Экспортировать сохранённый граф"),
        ("demo", "Проверить сборщик на вымышленных данных без Telegram"),
    ):
        sub = commands.add_parser(
            name,
            help=help_text,
            allow_abbrev=False,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
        sub.add_argument(
            "--db", type=Path, default=Path("data/demo.db" if name == "demo" else "data/gifts.db")
        )
        if name in {"status", "resume", "export"}:
            sub.add_argument("--run-id", type=int, help="По умолчанию последний запуск")
        if name in {"crawl", "resume", "demo"}:
            sub.add_argument(
                "--export",
                type=Path,
                action="append",
                default=[],
                help="Файл .graphml/.json; можно несколько раз; crawl: outputs/<цель>.graphml",
            )
        if name in {"crawl", "resume"}:
            sub.add_argument(
                "--session",
                type=Path,
                help="Готовая .session; без флага: userbot.session или единственная .session в cwd",
            )
            sub.add_argument("--output-dir", type=Path, default=Path("outputs"))
        if name in {"crawl", "resume", "demo", "export"}:
            sub.add_argument(
                "--gift_description",
                "--gift-description",
                dest="gift_description",
                action="store_true",
                help="Отдельный JSON: ID события → подпись к подарку",
            )
        if name in {"crawl", "demo"}:
            sub.add_argument("--depth", type=int, default=2 if name == "demo" else 3)
            sub.add_argument("--max-nodes", type=int, default=500 if name == "demo" else 3000)
            sub.add_argument("--max-gifts", type=int, default=1000, help="Подарков на профиль")
            sub.add_argument(
                "--max-pages",
                type=int,
                default=2000,
                help="Страниц списков подарков на вызов команды; сообщения чата не читаются",
            )
            sub.add_argument(
                "--page-size", type=int, default=100, help="Подарков на страницу, 1–100"
            )
            sub.add_argument("--delay", type=float, default=0 if name == "demo" else 1.0)
        if name == "crawl":
            targets = sub.add_mutually_exclusive_group(required=True)
            targets.add_argument("--chat", action=SingleTarget, help="ID или username одного чата")
            targets.add_argument(
                "--user", action=SingleTarget, help="ID или username одного пользователя"
            )
            sub.add_argument(
                "--seed-limit", type=int, default=100, help="Стартовых участников чата"
            )
        if name == "export":
            sub.add_argument("--out", type=Path, required=True)
        if name != "demo":
            for action in sub._actions:
                # Target actions are nested in a mutually exclusive group, but
                # all configurable settings are regular subparser actions.
                variable = ENV_OPTIONS.get(action.dest)
                if variable is None:
                    continue
                value = os.environ.get(variable, "")
                if value.strip():
                    action.default = value.strip()
                action.help = f"{action.help or ''} [{variable}]".strip()
    return result


def _config(args: argparse.Namespace) -> CrawlConfig:
    return CrawlConfig(
        max_depth=args.depth,
        max_nodes=args.max_nodes,
        max_gifts_per_profile=args.max_gifts,
        max_pages=args.max_pages,
        page_size=args.page_size,
        request_delay=args.delay,
    )


def find_session(path: Path | None) -> Path:
    if path is not None:
        selected = path.expanduser()
        if selected.suffix != ".session":
            selected = Path(str(selected) + ".session")
        if not selected.is_file():
            raise ValueError(f"Файл сессии не найден: {selected}. Укажите готовый --session FILE.")
        return selected
    preferred = Path("userbot.session")
    if preferred.is_file():
        return preferred
    candidates = sorted(Path.cwd().glob("*.session"))
    candidates = [candidate for candidate in candidates if candidate.is_file()]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError("В текущей папке нет готовой .session. Укажите --session FILE.")
    raise ValueError(
        "В текущей папке несколько .session. Укажите нужный файл через --session FILE."
    )


def _source(session: Path | None, include_descriptions: bool, request_delay: float):
    from .telegram import TelegramSource

    api_id = os.environ.get("API_ID", "").strip()
    if api_id and (not api_id.isdecimal() or int(api_id) < 1):
        raise ValueError("API_ID должен быть положительным целым числом")
    return TelegramSource(
        find_session(session),
        int(api_id) if api_id else None,
        os.environ.get("API_HASH") or None,
        include_descriptions=include_descriptions,
        request_delay=request_delay,
    )


def _target_filename(reference: str) -> str:
    # Only a basename is derived from user input: no directory traversal.
    value = reference.strip().rstrip("/").rsplit("/", 1)[-1].removeprefix("@")
    value = re.sub(r"[^A-Za-z0-9_-]", "_", value).strip("_")
    return value or "graph"


def _export(store: Store, run_id: int, paths: list[Path], descriptions: bool = False) -> None:
    for path in paths:
        export_graph(store.graph_data(run_id), path)
        print(f"Экспорт: {path.resolve()}")
        if descriptions:
            sidecar = path.with_name(path.stem + ".gift_descriptions.json")
            export_gift_descriptions(store.gift_descriptions(run_id), sidecar)
            print(f"Подписи подарков: {sidecar.resolve()}")


def _context(store: Store, run_id: int) -> dict:
    row = store.db.execute("SELECT data FROM run_context WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def _validate_exports(paths: list[Path], descriptions: bool = False) -> None:
    if any(path.suffix.lower() not in {".json", ".graphml"} for path in paths):
        raise ValueError("--export/--out должен указывать файл .json или .graphml")
    if descriptions:
        graphs = {path.resolve() for path in paths}
        notes = {path.with_name(path.stem + ".gift_descriptions.json").resolve() for path in paths}
        if graphs & notes:
            raise ValueError("Путь экспорта графа совпадает с файлом подписей подарков")


async def execute(args: argparse.Namespace) -> None:
    paths = getattr(args, "export", [])
    _validate_exports(paths, getattr(args, "gift_description", False))
    if args.command == "export":
        _validate_exports([args.out], args.gift_description)
    if args.command in {"status", "export", "resume"} and not args.db.is_file():
        raise ValueError(f"База не найдена: {args.db}")
    config = _config(args) if args.command in {"crawl", "demo"} else None
    if args.command == "crawl" and args.seed_limit < 1:
        raise ValueError("--seed-limit должен быть положительным")
    # Check session existence before creating a database or contacting Telegram.
    session = find_session(args.session) if args.command == "crawl" else None
    with database_lock(args.db), Store(args.db) as store:
        if args.command == "status":
            run = store.get_run(args.run_id)
            data = asdict(store.stats(run["id"]))
            data.update(
                source=run["source"], error=run["error"], node_limit_hit=bool(run["node_limit_hit"])
            )
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        if args.command == "export":
            _export(store, store.get_run(args.run_id)["id"], [args.out], args.gift_description)
            return
        if args.command == "demo":
            run_id = store.create_run(DEMO_SEEDS, config, source="demo")
            stats = await crawl(store, DemoSource(), run_id)
        elif args.command == "resume":
            run = store.get_run(args.run_id)
            run_id = run["id"]
            context = _context(store, run_id)
            descriptions = bool(context.get("gift_description"))
            if args.gift_description and not descriptions:
                raise ValueError(
                    "Подписи не собирались в этом запуске. Начните crawl --gift_description."
                )
            args.gift_description = descriptions
            if not paths:
                paths = [Path(path) for path in context.get("export_paths", [])]
            _validate_exports(paths, descriptions)
            if run["source"] not in {"demo", "telegram"}:
                raise ValueError("Старый обход Bot API не поддерживается. Начните новый crawl.")
            if store.next_node(run_id) is None:
                stats = store.stats(run_id)
            elif run["source"] == "demo":
                stats = await crawl(store, DemoSource(), run_id)
            else:
                async with _source(
                    args.session, descriptions, store.config(run_id).request_delay
                ) as source:
                    stats = await crawl(store, source, run_id)
        else:
            reference = args.chat if args.chat is not None else args.user
            auto_export = not paths
            if auto_export:
                paths = [args.output_dir / (_target_filename(reference) + ".graphml")]
            context = {
                "target_kind": "chat" if args.chat is not None else "user",
                "target": reference,
                "gift_description": args.gift_description,
                "export_paths": [str(path.resolve()) for path in paths],
            }
            async with _source(session, args.gift_description, config.request_delay) as source:
                if auto_export and reference.lstrip("-").isdigit():
                    target = await source.resolve_peer(reference)
                    if target.username:
                        paths = [args.output_dir / (_target_filename(target.username) + ".graphml")]
                        context["export_paths"] = [str(path.resolve()) for path in paths]
                if args.chat is not None:
                    seeds = {}
                    async for peer in source.iter_members(args.chat, args.seed_limit):
                        if peer.kind == "user":
                            seeds[peer.key] = peer
                        if len(seeds) > config.max_nodes:
                            raise ValueError("Стартовых профилей больше --max-nodes")
                    seed_users = list(seeds.values())
                else:
                    peer = await source.resolve_peer(args.user)
                    if peer.kind != "user":
                        raise ValueError(
                            "Для --user нужен пользователь. Для группы используйте --chat."
                        )
                    seed_users = [peer]
                run_id = store.create_run(seed_users, config, source="telegram", context=context)
                print(f"Запуск {run_id}: {len(seed_users)} стартовых профилей", flush=True)
                stats = await crawl(store, source, run_id)
        print(json.dumps(asdict(stats), ensure_ascii=False, indent=2))
        _export(store, run_id, paths, args.gift_description)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    bootstrap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    bootstrap.add_argument("--env-file", type=Path)
    initial, remaining = bootstrap.parse_known_args(argv)
    try:
        with environment_file(initial.env_file):
            if not remaining:
                parser().print_help()
                return 0
            args = parser().parse_args(remaining)
            if hasattr(args, "gift_description"):
                args.gift_description = _boolean(args.gift_description)
            if os.name != "nt":
                os.umask(0o077)
            logging.basicConfig(
                level=logging.DEBUG if args.verbose else logging.INFO,
                format="%(levelname)s %(message)s",
            )
            logging.getLogger("pyrogram").setLevel(logging.WARNING)
            asyncio.run(execute(args))
    except KeyboardInterrupt:
        print("Остановлено. Прогресс сохранён; используйте giftgraph resume.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, PeerUnavailable) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logging.error("%s: %s", type(exc).__name__, exc)
        return 1
    return 0
