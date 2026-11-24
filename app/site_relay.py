"""Связь с сайтом через PHP-посредника на хостинге сайта.

Виджет на сайте отправляет сообщение в https://<сайт>/bot-api/ — PHP кладёт его в
очередь. Бот сам забирает очередь (long polling, как у Telegram) и отправляет ответы
обратно. Поэтому на сервере бота не нужны ни открытые порты, ни HTTPS-сертификат.

Посредник лежит в проекте сайта: public/bot-api/relay.php.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any

import httpx

from app.chat_service import ChatOutcome, ChatRejected, ChatService

log = logging.getLogger(__name__)

MIN_EMPTY_POLL_SECONDS = 1.0
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
JOB_RE = re.compile(r"^[a-f0-9]{24}$")


class SiteRelay:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        chat: ChatService,
        client_key: Callable[[str], str],
        config_payload: Callable[[], dict[str, Any]],
        recent_payload: Callable[[], dict[str, Any]],
        chat_response: Callable[[ChatOutcome], dict[str, Any]],
        wait_seconds: int = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.chat = chat
        self.client_key = client_key
        self.config_payload = config_payload
        self.recent_payload = recent_payload
        self.chat_response = chat_response
        self.wait_seconds = wait_seconds
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"X-Relay-Token": token},
            timeout=httpx.Timeout(wait_seconds + 20, connect=10),
            transport=transport,
        )
        self._state_changed = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    def state_changed(self) -> None:
        """Появилась запись — обновить таблицу «последние записи» на сайте."""
        self._state_changed.set()

    async def _post(self, path: str, payload: dict[str, Any], **params: Any) -> dict[str, Any]:
        response = await self._http.post(path, json=payload, params=params or None)
        response.raise_for_status()
        return response.json()

    async def push_state(self) -> None:
        await self._post("api/worker/state", {"config": self.config_payload(), "recent": self.recent_payload()})

    async def _state_loop(self) -> None:
        while True:
            try:
                await self.push_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Сайт: не удалось обновить настройки и записи (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self._state_changed.wait(), timeout=60)
            except TimeoutError:
                pass
            self._state_changed.clear()

    async def run(self) -> None:
        state_task = asyncio.create_task(self._state_loop())
        backoff = 1.0
        loop = asyncio.get_running_loop()
        try:
            while True:
                started = loop.time()
                try:
                    data = await self._post("api/worker/pull", {}, wait=self.wait_seconds)
                    backoff = 1.0
                    if not data.get("jobs") and loop.time() - started < MIN_EMPTY_POLL_SECONDS:
                        # Посредник ответил «пусто» сразу, не подождав: не долбим хостинг запросами.
                        await asyncio.sleep(MIN_EMPTY_POLL_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — сеть, хостинг, неверный ключ
                    log.warning("Сайт недоступен (%s: %s), повтор через %.0f с", type(exc).__name__, exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                for job in data.get("jobs") or []:
                    task = asyncio.create_task(self.handle(job))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        finally:
            state_task.cancel()

    async def handle(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("id", ""))
        if not JOB_RE.match(job_id):
            log.warning("Сайт: задание без корректного id пропущено")
            return
        session_id = str(job.get("session_id", ""))
        status, body = 200, {}
        try:
            if not SESSION_RE.match(session_id):
                raise ChatRejected(422, "Неверный идентификатор диалога.")
            outcome = await self.chat.handle(
                session_id=f"site-{session_id}",
                client_key=self.client_key(str(job.get("ip", ""))),
                text=str(job.get("message", "")),
                channel="web",
            )
            body = self.chat_response(outcome)
            if outcome.booking is not None:
                self.state_changed()
        except ChatRejected as exc:
            status, body = exc.status_code, {"detail": exc.message}
        except Exception:
            log.exception("Сайт: ошибка при обработке сообщения")
            status, body = 500, {"detail": "Что-то пошло не так. Попробуйте отправить сообщение ещё раз."}
        try:
            await self._post("api/worker/reply", {"id": job_id, "status": status, "body": body})
        except Exception:
            log.exception("Сайт: не удалось отправить ответ")

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self._http.aclose()
