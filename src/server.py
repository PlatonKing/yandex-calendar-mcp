#!/usr/bin/env python3
"""MCP-сервер Яндекс Календаря: чтение и запись.

Как это работает. Ассистент запускает эту программу и разговаривает с ней
через стандартный ввод-вывод: одна строка — одно сообщение в формате JSON.
Это и есть протокол MCP поверх stdio. Отсюда два железных правила:

  * в стандартный вывод уходят ТОЛЬКО ответы протокола;
  * всё остальное — в стандартный поток ошибок, иначе связь рассыпется.

Удаление необратимо, поэтому защита встроена в само действие: сверка названия
события, копия в корзину до удаления и отдельный признак согласия для
повторяющихся серий. Подробности — в README.
"""

import json
import os
import sys
import traceback
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import calendar_client as cc  # noqa: E402

SERVER_NAME = "yandex-calendar"
SERVER_VERSION = "0.1.0"
FALLBACK_PROTOCOL = "2025-06-18"

WEEKDAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Описание инструментов
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_events",
        "description": (
            "События из Яндекс Календаря пользователя за период. "
            "Все даты и время — в поясе, настроенном для сервера (переменная "
            "YANDEX_TIMEZONE, по умолчанию Europe/Moscow). Пересчёт делает сам "
            "сервер, поэтому часы машины, где он работает, значения не имеют: "
            "точный пояс и смещение названы в первой строке ответа. "
            "Без указания дат берётся сегодняшний день в этом поясе. "
            "Повторяющиеся встречи разворачиваются в конкретные даты."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": "Начало периода, ГГГГ-ММ-ДД. По умолчанию — сегодня.",
                },
                "date_to": {
                    "type": "string",
                    "description": (
                        "Конец периода включительно, ГГГГ-ММ-ДД. "
                        "По умолчанию — тот же день, что и начало."
                    ),
                },
                "calendar": {
                    "type": "string",
                    "description": (
                        "Имя календаря. Без него просматриваются все календари, "
                        "и каждое событие подписывается своим."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_calendars",
        "description": (
            "Список календарей пользователя в Яндексе: их имена и адреса. "
            "Нужен, чтобы узнать точное имя календаря для list_events."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_event",
        "description": (
            "Создать событие в Яндекс Календаре: разовое, на весь день или "
            "повторяющееся. Время указывается в настроенном поясе, метка "
            "часового пояса проставляется сама. "
            "Календарь обязателен: их несколько и они под разные задачи — "
            "если пользователь не назвал календарь, спросите, а не выбирайте сами."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "calendar": {
                    "type": "string",
                    "description": "Точное имя календаря из list_calendars.",
                },
                "summary": {"type": "string", "description": "Название события."},
                "start": {
                    "type": "string",
                    "description": (
                        "Начало в настроенном поясе: ГГГГ-ММ-ДД ЧЧ:ММ. "
                        "Для события на весь день достаточно ГГГГ-ММ-ДД."
                    ),
                },
                "end": {
                    "type": "string",
                    "description": "Конец в настроенном поясе, ГГГГ-ММ-ДД ЧЧ:ММ. Необязательно.",
                },
                "duration_minutes": {
                    "type": "integer",
                    "description": (
                        "Длительность в минутах, если конец не указан. "
                        "По умолчанию 60."
                    ),
                },
                "all_day": {
                    "type": "boolean",
                    "description": "Событие на весь день, без времени.",
                },
                "days": {
                    "type": "integer",
                    "description": "Сколько дней занимает событие на весь день. По умолчанию 1.",
                },
                "location": {"type": "string", "description": "Место."},
                "description": {"type": "string", "description": "Описание, заметка."},
                "repeat": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly", "yearly"],
                    "description": (
                        "Сделать событие повторяющимся: каждый день, каждую "
                        "неделю, месяц или год. Без этого поля событие одиночное."
                    ),
                },
                "repeat_count": {
                    "type": "integer",
                    "description": "Сколько всего раз повторить. Нельзя вместе с repeat_until.",
                },
                "repeat_until": {
                    "type": "string",
                    "description": (
                        "До какой даты повторять, ГГГГ-ММ-ДД включительно. "
                        "Нельзя вместе с repeat_count."
                    ),
                },
            },
            "required": ["calendar", "summary", "start"],
        },
    },
    {
        "name": "update_event",
        "description": (
            "Изменить существующее событие: перенести время, переименовать, "
            "сменить место или описание. Событие ищется по метке id из list_events. "
            "Передавайте только то, что меняется; остальное останется как было. "
            "У повторяющегося события можно изменить либо один день — параметр "
            "occurrence, — либо всю серию целиком (apply_to_series). "
            "Копия события до правки сохраняется в корзине."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "Метка события — поле id из свежей выдачи list_events.",
                },
                "calendar": {
                    "type": "string",
                    "description": "Имя календаря, если известно: ускоряет поиск.",
                },
                "summary": {"type": "string", "description": "Новое название."},
                "start": {
                    "type": "string",
                    "description": "Новое начало в настроенном поясе, ГГГГ-ММ-ДД ЧЧ:ММ.",
                },
                "end": {
                    "type": "string",
                    "description": "Новый конец в настроенном поясе, ГГГГ-ММ-ДД ЧЧ:ММ.",
                },
                "duration_minutes": {
                    "type": "integer",
                    "description": (
                        "Новая длительность в минутах. Если перенесено только начало "
                        "и длительность не указана, она сохраняется прежней."
                    ),
                },
                "location": {"type": "string", "description": "Новое место."},
                "description": {"type": "string", "description": "Новое описание."},
                "occurrence": {
                    "type": "string",
                    "description": (
                        "Только для повторяющихся событий: день серии, который "
                        "меняется, ГГГГ-ММ-ДД. Если в этот день несколько "
                        "повторов, добавьте время: ГГГГ-ММ-ДД ЧЧ:ММ. Остальные "
                        "дни серии не затрагиваются."
                    ),
                },
                "apply_to_series": {
                    "type": "boolean",
                    "description": (
                        "Только для повторяющихся событий: изменить всю серию "
                        "целиком. Взаимоисключающе с occurrence. Для серии "
                        "нужно указать одно из двух, иначе инструмент откажет."
                    ),
                },
            },
            "required": ["uid"],
        },
    },
    {
        "name": "delete_event",
        "description": (
            "Удалить событие из Яндекс Календаря. Действие необратимо на стороне "
            "Яндекса, поэтому спрашивайте у пользователя явное согласие до вызова. "
            "Нужны и метка id, и название ровно как в выдаче list_events: названия "
            "сверяются, и при расхождении не удаляется ничего. У повторяющегося "
            "события можно удалить либо один день — параметр occurrence, — либо всю "
            "серию целиком (apply_to_series). Копия удалённого события сохраняется "
            "в корзине."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {
                    "type": "string",
                    "description": "Метка события — поле id из свежей выдачи list_events.",
                },
                "summary": {
                    "type": "string",
                    "description": "Название события для сверки, как в выдаче list_events.",
                },
                "calendar": {
                    "type": "string",
                    "description": "Имя календаря, если известно: ускоряет поиск.",
                },
                "occurrence": {
                    "type": "string",
                    "description": (
                        "Только для повторяющихся событий: день серии, который "
                        "удаляется, ГГГГ-ММ-ДД. Если в этот день несколько "
                        "повторов, добавьте время: ГГГГ-ММ-ДД ЧЧ:ММ. Сама серия "
                        "сохраняется, пропадает только этот день."
                    ),
                },
                "apply_to_series": {
                    "type": "boolean",
                    "description": (
                        "Только для повторяющихся событий: удалить всю серию "
                        "целиком. Взаимоисключающе с occurrence. Для серии нужно "
                        "указать одно из двух, иначе инструмент откажет."
                    ),
                },
            },
            "required": ["uid", "summary"],
        },
    },
]


# --------------------------------------------------------------------------
# Оформление ответа для ассистента
# --------------------------------------------------------------------------


def _human_day(value: date) -> str:
    return f"{value.isoformat()} ({WEEKDAYS[value.weekday()]})"


def _human_range(ev: dict) -> str:
    if ev["all_day"]:
        return "весь день"
    start = ev["start"]
    end = ev["end"]
    if isinstance(end, datetime) and end != start:
        return f"{start:%H:%M}–{end:%H:%M}"
    return f"{start:%H:%M}"


def _event_day(ev: dict) -> date:
    value = ev["start"]
    return value.date() if isinstance(value, datetime) else value


def format_events(data: dict) -> str:
    now = data["now"]
    lines = [
        f"Сейчас: {now:%Y-%m-%d %H:%M} ({WEEKDAYS[now.weekday()]}), "
        f"пояс {data['timezone']}, смещение {now:%z}.",
        f"Период: {_human_day(data['date_from'])} — {_human_day(data['date_to'])}, "
        "время в настроенном поясе.",
        "",
    ]

    events = data["events"]
    if not events:
        lines.append("Событий нет.")
    else:
        current_day = None
        for ev in events:
            day = _event_day(ev)
            if day != current_day:
                current_day = day
                lines.append(_human_day(day) + ":")
            parts = [f"  {_human_range(ev)} — {ev['summary']}"]
            if ev["location"]:
                parts.append(f"место: {ev['location']}")
            parts.append(f"календарь: {ev['calendar']}")
            if ev["repeating"]:
                parts.append("повторяющееся")
            if not ev["expanded"]:
                parts.append(
                    "ВНИМАНИЕ: правило повтора не развёрнуто, дата может быть неточной"
                )
            if ev["uid"]:
                parts.append(f"id: {ev['uid']}")
            lines.append(" | ".join(parts))

    for warning in data["warnings"]:
        lines.append(f"Предупреждение: {warning}")

    return "\n".join(lines)


def format_one(ev: dict) -> str:
    day = _event_day(ev)
    parts = [f"{_human_day(day)}, {_human_range(ev)} — {ev['summary']}"]
    if ev["location"]:
        parts.append(f"место: {ev['location']}")
    if ev["repeating"]:
        parts.append("повторяющееся")
    if ev["uid"]:
        parts.append(f"id: {ev['uid']}")
    return " | ".join(parts)


def format_created(data: dict) -> str:
    return "\n".join(
        [
            f"Событие создано в календаре «{data['calendar']}», время в настроенном поясе:",
            "  " + format_one(data["event"]),
        ]
    )


def format_updated(data: dict) -> str:
    return "\n".join(
        [
            f"Изменено в календаре «{data['calendar']}» ({data['scope']}), "
            "время в настроенном поясе.",
            "  было: " + format_one(data["before"]),
            "  стало: " + format_one(data["after"]),
            f"  копия до правки: {data['backup']}",
        ]
    )


def format_deleted(data: dict) -> str:
    return "\n".join(
        [
            f"Удалено из календаря «{data['calendar']}» ({data['scope']}):",
            "  " + format_one(data["event"]),
            f"  копия сохранена: {data['backup']}",
        ]
    )


def format_calendars(calendars: list[dict]) -> str:
    if not calendars:
        return "Календарей не найдено."
    lines = ["Календари пользователя:"]
    for cal in calendars:
        lines.append(f"  {cal['name'] or '(без имени)'} — {cal['url']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Вызов инструментов
# --------------------------------------------------------------------------


def call_tool(name: str, arguments: dict) -> dict:
    try:
        if name == "list_events":
            data = cc.list_events(
                date_from=arguments.get("date_from"),
                date_to=arguments.get("date_to"),
                calendar=arguments.get("calendar"),
            )
            text = format_events(data)
        elif name == "list_calendars":
            text = format_calendars(cc.list_calendars())
        elif name == "create_event":
            text = format_created(
                cc.create_event(
                    calendar=arguments.get("calendar") or "",
                    summary=arguments.get("summary") or "",
                    start=arguments.get("start") or "",
                    end=arguments.get("end"),
                    duration_minutes=arguments.get("duration_minutes"),
                    all_day=bool(arguments.get("all_day")),
                    days=int(arguments.get("days") or 1),
                    location=arguments.get("location"),
                    description=arguments.get("description"),
                    repeat=arguments.get("repeat"),
                    repeat_count=arguments.get("repeat_count"),
                    repeat_until=arguments.get("repeat_until"),
                )
            )
        elif name == "update_event":
            text = format_updated(
                cc.update_event(
                    uid=arguments.get("uid") or "",
                    calendar=arguments.get("calendar"),
                    summary=arguments.get("summary"),
                    start=arguments.get("start"),
                    end=arguments.get("end"),
                    duration_minutes=arguments.get("duration_minutes"),
                    location=arguments.get("location"),
                    description=arguments.get("description"),
                    occurrence=arguments.get("occurrence"),
                    apply_to_series=bool(arguments.get("apply_to_series")),
                )
            )
        elif name == "delete_event":
            text = format_deleted(
                cc.delete_event(
                    uid=arguments.get("uid") or "",
                    summary=arguments.get("summary") or "",
                    calendar=arguments.get("calendar"),
                    occurrence=arguments.get("occurrence"),
                    apply_to_series=bool(arguments.get("apply_to_series")),
                )
            )
        else:
            return _tool_error(f"Инструмент {name!r} не существует.")
    except cc.CalendarError as exc:
        return _tool_error(str(exc))
    except Exception as exc:
        log("сбой инструмента:\n" + traceback.format_exc())
        return _tool_error(f"Сбой при обращении к календарю: {exc}")

    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# --------------------------------------------------------------------------
# Протокол MCP поверх stdio
# --------------------------------------------------------------------------


def handle(message: dict):
    """Возвращает ответ или None, если ответ не нужен (уведомление)."""
    method = message.get("method")
    msg_id = message.get("id")
    is_request = msg_id is not None

    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        version = requested if isinstance(requested, str) else FALLBACK_PROTOCOL
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        return _result(msg_id, call_tool(name, arguments))

    if method == "ping":
        return _result(msg_id, {})

    if not is_request:  # на уведомления не отвечают никогда
        return None

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Метод {method!r} не поддерживается"},
    }


def _result(msg_id, result):
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def send(payload) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    log(f"запуск, версия {SERVER_VERSION}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"строка не разобрана как JSON: {exc}")
            continue

        # пачка сообщений приходит массивом — старые версии протокола так умеют
        batch = message if isinstance(message, list) else [message]
        answers = []
        for item in batch:
            if not isinstance(item, dict):
                continue
            try:
                answer = handle(item)
            except Exception as exc:
                log("сбой обработки сообщения:\n" + traceback.format_exc())
                item_id = item.get("id")
                answer = (
                    {
                        "jsonrpc": "2.0",
                        "id": item_id,
                        "error": {"code": -32603, "message": str(exc)},
                    }
                    if item_id is not None
                    else None
                )
            if answer is not None:
                answers.append(answer)

        if not answers:
            continue
        if isinstance(message, list):
            send(answers)
        else:
            send(answers[0])

    log("стандартный ввод закрыт, завершаюсь")


if __name__ == "__main__":
    main()
