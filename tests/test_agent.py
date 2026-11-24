from __future__ import annotations

import json

import pytest

from app.agent import BookingAgent, SessionStore, clean_reply, to_feminine, trim_history
from app.llm.base import FunctionCall, LLMResult, LLMUnavailableError, Message
from app.llm.fake import ScriptedLLM
from tests.conftest import booking_args


def make_agent(business, llm, tools, storage, now_fn):
    agent = BookingAgent(business, llm, tools, storage, now_fn)
    sessions = SessionStore(ttl_seconds=3600, ip_bookings_limit=5)
    return agent, sessions.get_or_create("session-abc", "iphash")


async def test_function_call_round_trip(business, tools, storage, now_fn):
    llm = ScriptedLLM(
        [
            LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil", "date": "2026-11-24"}), state_id="st-1"),
            LLMResult("Во вторник свободно в 9:00, 13:00 и 17:00. Какое время удобно?"),
        ]
    )
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Хочу поменять масло во вторник")

    assert reply.text.startswith("Во вторник")
    assert reply.booking is None
    assert [m.role for m in session.history] == ["user", "assistant", "function", "assistant"]
    assert session.history[1].state_id == "st-1"

    second_call = llm.calls[1]
    assert second_call[0].role == "system"
    assert "2026-11-23" in second_call[0].content
    assert "Приветствие уже показано" in second_call[0].content
    function_msg = second_call[-1]
    assert function_msg.role == "function" and function_msg.name == "get_free_slots"
    assert json.loads(function_msg.content)["date"] == "2026-11-24"


async def test_booking_is_returned(business, tools, storage, now_fn):
    llm = ScriptedLLM(
        [
            LLMResult("", FunctionCall("create_booking", booking_args())),
            LLMResult("**Готово!** Ваша запись №1."),
        ]
    )
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Да, подтверждаю")
    assert reply.booking is not None and reply.booking.id == 1
    assert reply.text == "Готово! Ваша запись №1."


async def test_llm_error_rolls_back_turn(business, tools, storage, now_fn):
    def fail(*_):
        raise LLMUnavailableError("down")

    agent, session = make_agent(business, ScriptedLLM([fail]), tools, storage, now_fn)
    with pytest.raises(LLMUnavailableError):
        await agent.reply(session, "Привет")
    assert session.history == []


async def test_llm_error_after_booking_still_confirms(business, tools, storage, now_fn):
    def fail(*_):
        raise LLMUnavailableError("down")

    llm = ScriptedLLM([LLMResult("", FunctionCall("create_booking", booking_args())), fail])
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Подтверждаю")
    assert reply.booking is not None
    assert "Номер записи — 1" in reply.text


async def test_hallucinated_time_is_sent_back_for_rewrite(business, tools, storage, now_fn):
    llm = ScriptedLLM(
        [
            LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil", "date": "2026-11-24", "preferred_time": "14:00"})),
            LLMResult("Есть 14:07 и 18:00."),  # 14:07 нет среди свободных
            LLMResult("Во вторник свободно 18:00 и 18:30."),
        ]
    )
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Масло во вторник в 14:07")

    assert reply.text == "Во вторник свободно 18:00 и 18:30."
    correction = llm.calls[2]
    assert correction[-2].role == "assistant" and correction[-2].content == "Есть 14:07 и 18:00."
    assert correction[-1].role == "user" and "14:07" in correction[-1].content
    # черновик и замечание не попадают в историю диалога
    assert [m.role for m in session.history] == ["user", "assistant", "function", "assistant"]


async def test_promise_without_function_call_is_sent_back(business, tools, storage, now_fn):
    llm = ScriptedLLM(
        [
            LLMResult("Сейчас посмотрю свободное время."),
            LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil"})),
            LLMResult("Свободно сегодня."),
        ]
    )
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Когда можно на масло?")
    assert reply.text == "Свободно сегодня."
    assert "не вызвал функцию" in llm.calls[1][-1].content


async def test_unchecked_availability_claim_is_sent_back(business, tools, storage, now_fn):
    llm = ScriptedLLM(
        [
            LLMResult("Во вторник после обеда есть свободное время, во сколько удобно?"),
            LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil", "date": "2026-11-24"})),
            LLMResult("Во вторник свободно 13:00 и 15:00."),
        ]
    )
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Масло во вторник после обеда")
    assert reply.text == "Во вторник свободно 13:00 и 15:00."
    assert "расписание ещё не проверено" in llm.calls[1][-1].content


async def test_too_many_times_are_sent_back(business, tools, storage, now_fn):
    def dump_everything(messages, _):
        data = json.loads(messages[-1].content)
        times = [t for day in data["days"] for t in day["free_slots"]]
        return LLMResult("Свободно: " + ", ".join(times))

    llm = ScriptedLLM(
        [
            LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil"})),
            dump_everything,
            lambda messages, _: LLMResult("Ближайшее: " + json.loads(messages[-3].content)["days"][0]["free_slots"][0]),
        ]
    )
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Когда можно на масло?")
    assert "слишком много вариантов" in llm.calls[2][-1].content
    assert reply.text.startswith("Ближайшее: ")


async def test_after_diagnosis_model_must_answer_in_text(business, tools, storage, now_fn):
    llm = ScriptedLLM(
        [
            LLMResult("", FunctionCall("get_symptom_guide", {"system": "brakes"})),
            LLMResult("Ехать опасно. Есть ли следы жидкости у колёс?"),
        ]
    )
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Проваливается педаль тормоза")
    assert reply.text.startswith("Ехать опасно")
    # отнимать функции сразу после справочника нельзя: модель печатает обрывки разметки
    assert llm.allow_calls == [True, True]


async def test_text_only_step_sends_no_functions(business, tools, storage, now_fn):
    same = LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil"}))
    llm = ScriptedLLM([same, same, LLMResult("Есть время.")])
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    await agent.reply(session, "Когда на масло?")
    assert llm.function_counts[0] > 0 and llm.function_counts[2] == 0


async def test_tool_markup_never_reaches_client(business, tools, storage, now_fn):
    markup = '< выгодныеtool_calls>< выгодныеinvoke name="get_free_slots"></ выгодныеinvoke></ выгодныеtool_calls>'
    llm = ScriptedLLM(
        [
            LLMResult("", FunctionCall("get_symptom_guide", {"system": "brakes"})),
            LLMResult(markup),
            LLMResult("Ехать опасно, нужен эвакуатор."),
        ]
    )
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Проваливается педаль")
    assert reply.text == "Ехать опасно, нужен эвакуатор."

    stubborn = ScriptedLLM([LLMResult(markup), LLMResult(markup)])
    agent, session = make_agent(business, stubborn, tools, storage, now_fn)
    assert "invoke" not in (await agent.reply(session, "?")).text


async def test_no_empty_promises_when_schedule_is_unavailable(business, tools, storage, now_fn):
    ask = lambda d: LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil", "date": d}))  # noqa: E731
    llm = ScriptedLLM(
        [
            ask("2026-11-24"),
            ask("2026-11-25"),  # второй запрос расписания — дальше только текст
            LLMResult("Минутку, посмотрю ещё."),
            LLMResult("На какой день подобрать запись?"),
        ]
    )
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Масло на этой неделе")
    assert reply.text == "На какой день подобрать запись?"
    assert "Не обещай" in llm.calls[3][-1].content


@pytest.mark.parametrize("garbage", ["<", "< выгодныеtool_calls>", "  </invoke>  ", "ok"])
def test_markup_detection(garbage):
    from app.agent import looks_like_markup

    assert looks_like_markup(garbage)
    assert not looks_like_markup("Свистит приводной ремень, меняем вместе с роликом.")


async def test_model_ignoring_text_only_does_not_loop(business, tools, storage, now_fn):
    same = LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil"}))
    llm = ScriptedLLM([same, same, LLMResult("Есть время завтра.", FunctionCall("get_free_slots", {"service_id": "oil"}))])
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "Когда на масло?")
    assert reply.text == "Есть время завтра."
    assert llm.allow_calls == [True, True, False]


async def test_second_schedule_request_ends_the_search(business, tools, storage, now_fn):
    ask = lambda t: LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil", "date": "2026-11-24", "preferred_time": t}))  # noqa: E731
    llm = ScriptedLLM([ask("08:00"), ask("13:00"), LLMResult("Есть 13:00.")])
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    await agent.reply(session, "Масло во вторник")
    assert llm.allow_calls == [True, True, False]


async def test_review_happens_only_once_per_turn(business, tools, storage, now_fn):
    llm = ScriptedLLM([LLMResult("Приезжайте в 14:07"), LLMResult("Приезжайте в 14:07")])
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    reply = await agent.reply(session, "?")
    assert reply.text == "Приезжайте в 14:07"
    assert len(llm.calls) == 2


async def test_working_hours_and_slots_pass_review(business, tools, storage, now_fn):
    llm = ScriptedLLM(
        [
            LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil", "date": "2026-11-24"})),
            lambda messages, _: LLMResult(
                f"Работаем с 9:00 до 20:00, свободно {json.loads(messages[-1].content)['free_slots'][0]}."
            ),
        ]
    )
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    await agent.reply(session, "Когда во вторник?")
    assert len(llm.calls) == 2


async def test_endless_function_calls_are_cut(business, tools, storage, now_fn):
    loop = [LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil"})) for _ in range(10)]
    agent, session = make_agent(business, ScriptedLLM(loop), tools, storage, now_fn)
    reply = await agent.reply(session, "?")
    assert "уточним" in reply.text


def test_trim_history_starts_with_user():
    history = [
        Message("user", "1"),
        Message("assistant", "", function_call=FunctionCall("get_free_slots", {})),
        Message("function", "{}", name="get_free_slots"),
        Message("assistant", "ok"),
        Message("user", "2"),
        Message("assistant", "ok2"),
    ]
    trimmed = trim_history(history, limit=4)
    assert trimmed[0].content == "2"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Готово, записал вас на 15:00.", "Готово, записала вас на 15:00."),
        ("Записал вас на диагностику.", "Записала вас на диагностику."),
        ("Я понял, проверил расписание.", "Я поняла, проверила расписание."),
        ("Я нашёл окно, рад помочь!", "Я нашла окно, рада помочь!"),
        ("Мастер проверил подвеску, вы записались.", "Мастер проверил подвеску, вы записались."),
        ("Записал(а) вас на 19:00, рад(-а) помочь.", "Записала вас на 19:00, рада помочь."),
    ],
)
def test_to_feminine(source, expected):
    assert to_feminine(source) == expected


async def test_repeated_function_call_gets_hint(business, tools, storage, now_fn):
    call = LLMResult("", FunctionCall("get_free_slots", {"service_id": "oil", "date": "2026-11-24"}))
    llm = ScriptedLLM([call, call, LLMResult("Свободно 13:00.")])
    agent, session = make_agent(business, llm, tools, storage, now_fn)
    await agent.reply(session, "Когда во вторник?")
    first, second = (json.loads(m.content) for m in session.history if m.role == "function")
    assert "note" not in first
    assert "повторный запрос" in second["note"]


def test_clean_reply_strips_markdown():
    assert clean_reply("## Заголовок\n**жирный** и `код`\n\n\n\nконец") == "Заголовок\nжирный и код\n\nконец"
