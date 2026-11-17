"""Restrict application MTProto calls to the reads needed by the gift crawler."""

import asyncio
import sqlite3
from collections import Counter
from pathlib import Path


class ReadOnlyViolation(RuntimeError):
    pass


def validate_existing_session(path: Path) -> None:
    """Reject incomplete sessions before Kurigram can attempt new authorization."""
    try:
        with sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True
        ) as connection:
            complete = connection.execute("""
                SELECT EXISTS(SELECT 1 FROM sessions WHERE user_id > 0 AND is_bot=0
                    AND test_mode IS NOT NULL AND LENGTH(auth_key)=256 AND api_id>0
                    AND dc_id>0 AND server_address IS NOT NULL AND port>0)
            """).fetchone()[0]
    except sqlite3.Error:
        raise RuntimeError(
            "Файл сессии повреждён или несовместим с Kurigram."
        ) from None
    if not complete:
        raise RuntimeError(
            "Нужна готовая авторизованная пользовательская сессия; новый вход отключён."
        )


def read_only_client_class(base_client, raw):
    allowed = frozenset(
        {
            raw.functions.contacts.ResolveUsername,
            raw.functions.users.GetUsers,
            raw.functions.channels.GetChannels,
            raw.functions.messages.GetChats,
            raw.functions.messages.GetFullChat,
            raw.functions.channels.GetParticipants,
            raw.functions.payments.GetSavedStarGifts,
            raw.functions.help.GetConfig,
        }
    )

    class ReadOnlyClient(base_client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.read_request_counts = Counter()
            self.read_request_delay = 0.0
            self._read_last_request = None
            self._read_request_lock = asyncio.Lock()

        async def invoke(self, query, *args, **kwargs):
            if type(query) not in allowed:
                raise ReadOnlyViolation(
                    f"Read-only режим: запрос {type(query).__name__} запрещён до отправки."
                )
            async with self._read_request_lock:
                loop = asyncio.get_running_loop()
                if self._read_last_request is not None:
                    remaining = self.read_request_delay - (loop.time() - self._read_last_request)
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                self._read_last_request = loop.time()
                name = type(query).QUALNAME
                self.read_request_counts[name] += 1
                return await super().invoke(query, *args, **kwargs)

    return ReadOnlyClient
