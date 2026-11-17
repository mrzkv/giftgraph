"""Create userbot.session once, separately from the read-only gift crawler.

Install this project with QR support: python -m pip install ".[qr]"
Put API_ID/API_HASH in the .env next to this script, then run:
    python generate_session.py

Scan the terminal QR code in Telegram: Settings -> Devices -> Link Desktop Device.
If requested, enter your two-step verification password in the terminal.
The resulting session is private account access data; do not share or commit it.
"""

import argparse
import asyncio
import os
import re
import sys
from contextlib import nullcontext
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SESSION_NAME = "userbot"


def load_api_credentials() -> tuple[int, str]:
    api_id_value = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()
    if not api_id_value or not api_hash:
        raise ValueError(
            "Укажите API_ID и API_HASH в .env или окружении. "
            "Получить их можно на https://my.telegram.org/apps."
        )
    if not re.fullmatch(r"[0-9]+", api_id_value) or int(api_id_value) < 1:
        raise ValueError("API_ID должен быть положительным целым числом.")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("API_HASH должен содержать 32 шестнадцатеричных символа.")
    return int(api_id_value), api_hash


def _configuration(env_file: Path | None):
    selected = env_file.expanduser() if env_file is not None else PROJECT_DIR / ".env"
    if env_file is None and not selected.exists():
        # An unrelated .env in the invoking directory must not be loaded.
        return nullcontext()
    from giftgraph.config import environment_file

    return environment_file(selected)


async def create_session(api_id: int, api_hash: str) -> None:
    """Run QR authorization only when the user explicitly invokes this helper."""
    # Check the optional dependency before opening a new session or connecting.
    import qrcode  # noqa: F401
    from pyrogram import Client

    session = PROJECT_DIR / (SESSION_NAME + ".session")
    if session.exists():
        raise RuntimeError("Файл userbot.session уже появился. Повторите запуск для его проверки.")
    previous_umask = os.umask(0o077) if os.name != "nt" else None
    client = None
    started = False
    try:
        client = Client(
            SESSION_NAME,
            api_id=api_id,
            api_hash=api_hash,
            workdir=str(PROJECT_DIR),
            no_updates=True,
        )
        await client.start(use_qr=True)
        started = True
        print(f"Сессия сохранена: {session}")
    finally:
        try:
            if client is not None:
                if started:
                    await client.stop()
                elif getattr(client, "is_connected", False):
                    await client.disconnect()
        finally:
            if previous_umask is not None:
                os.umask(previous_umask)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Однократно создать пользовательскую сессию Telegram через QR-код."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Файл API_ID/API_HASH; по умолчанию .env рядом с generate_session.py",
    )
    args = parser.parse_args(argv)
    try:
        with _configuration(args.env_file):
            session = PROJECT_DIR / (SESSION_NAME + ".session")
            if session.exists():
                from giftgraph.readonly import validate_existing_session

                validate_existing_session(session)
                print(f"Готовая сессия уже существует: {session}. Новый вход не требуется.")
                return 0
            api_id, api_hash = load_api_credentials()
            try:
                asyncio.run(create_session(api_id, api_hash))
            except ImportError:
                raise
            except Exception as exc:
                # Library errors can carry account details; do not echo them.
                print(
                    f"Не удалось создать сессию ({type(exc).__name__}). "
                    "Проверьте соединение, API-ключи и подтверждение входа в Telegram.",
                    file=sys.stderr,
                )
                return 1
    except KeyboardInterrupt:
        print("Создание сессии остановлено пользователем.", file=sys.stderr)
        return 130
    except ImportError:
        print('Установите проект с QR-поддержкой: python -m pip install ".[qr]"', file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Authorization exceptions can carry account details. Only the class is
        # reported here; tokens, credentials and session content are never logged.
        print(
            f"Не удалось создать сессию ({type(exc).__name__}). "
            "Проверьте соединение, API-ключи и подтверждение входа в Telegram.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
