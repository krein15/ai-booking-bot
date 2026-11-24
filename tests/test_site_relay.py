from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.agent import BookingAgent, SessionStore
from app.chat_service import ChatService
from app.llm.base import FunctionCall, LLMResult
from app.llm.fake import ScriptedLLM
from app.ratelimit import SlidingWindowLimiter
from app.site_relay import SiteRelay
from tests.conftest import booking_args

JOB_ID = "0123456789abcdef01234567"


class FakeRelay:
    """Подменяет relay.php: отдаёт задания и запоминает ответы."""

    def __init__(self, jobs=None):
        self.jobs = list(jobs or [])
        self.replies: list[dict] = []
        self.states: list[dict] = []
        self.tokens: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.tokens.add(request.headers.get("X-Relay-Token", ""))
        body = json.loads(request.content or b"{}")
        path = request.url.path
        if path.endswith("/api/worker/pull"):
            jobs, self.jobs = self.jobs, []
            return httpx.Response(200, json={"jobs": jobs})
        if path.endswith("/api/worker/reply"):
            self.replies.append(body)
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/api/worker/state"):
            self.states.append(body)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={})


@pytest.fixture
def make_relay(business, tools, storage, now_fn, make_settings):
    def _make(steps, jobs=None):
        agent = BookingAgent(business, ScriptedLLM(steps), tools, storage, now_fn)
        chat = ChatService(make_settings(), agent, SessionStore(3600, 5), SlidingWindowLimiter())
        fake = FakeRelay(jobs)
        relay = SiteRelay(
            base_url="https://site.example/bot-api",
            token="secret-token",
            chat=chat,
            client_key=lambda ip: f"ip:{ip}",
            config_payload=lambda: {"greeting": "Здравствуйте!"},
            recent_payload=lambda: {"items": []},
            chat_response=lambda outcome: {"reply": outcome.reply, "booking": outcome.booking is not None},
            transport=httpx.MockTransport(fake.handler),
        )
        return relay, fake

    return _make


def job(message: str, session: str = "site-session-1", job_id: str = JOB_ID) -> dict:
    return {"id": job_id, "session_id": session, "message": message, "ip": "203.0.113.5", "created": 0}


async def test_job_is_answered(make_relay):
    relay, fake = make_relay([LLMResult("Какая у вас машина?")])
    await relay.handle(job("Хочу поменять масло"))
    assert fake.replies == [{"id": JOB_ID, "status": 200, "body": {"reply": "Какая у вас машина?", "booking": False}}]
    assert fake.tokens == {"secret-token"}
    await relay.aclose()


async def test_booking_refreshes_site_state(make_relay, storage):
    relay, fake = make_relay(
        [LLMResult("", FunctionCall("create_booking", booking_args())), LLMResult("Записала, номер 1.")]
    )
    await relay.handle(job("Подтверждаю"))
    assert fake.replies[0]["body"]["booking"] is True
    assert relay._state_changed.is_set()
    assert storage.recent_bookings()[0].channel == "web"
    await relay.aclose()


async def test_rejections_and_bad_jobs(make_relay):
    relay, fake = make_relay([])
    await relay.handle(job("привет", session="bad id!"))
    assert fake.replies[0]["status"] == 422

    await relay.handle({"id": "../../etc", "session_id": "site-session-1", "message": "x"})
    assert len(fake.replies) == 1  # задание с подозрительным id просто пропускаем
    await relay.aclose()


async def test_run_pulls_jobs_and_pushes_state(make_relay, monkeypatch):
    monkeypatch.setattr("app.site_relay.MIN_EMPTY_POLL_SECONDS", 0.05)
    relay, fake = make_relay([LLMResult("Ответ")], jobs=[job("Привет")])
    pulls = 0
    original = fake.handler

    def counting(request):
        nonlocal pulls
        pulls += request.url.path.endswith("/pull")
        return original(request)

    relay._http._transport = httpx.MockTransport(counting)
    task = asyncio.create_task(relay.run())
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert fake.replies[0]["body"]["reply"] == "Ответ"
    assert fake.states[0] == {"config": {"greeting": "Здравствуйте!"}, "recent": {"items": []}}
    # мгновенные пустые ответы не превращаются в шквал запросов
    assert pulls <= 15
    await relay.aclose()
