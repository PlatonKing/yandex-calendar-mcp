#!/usr/bin/env python3
"""Полный круг записи: одиночное событие и повторяющаяся серия.

Проверка идёт через настоящий протокол: запускается server.py как отдельная
программа, и с ней ведётся тот же обмен строками JSON, что ведёт ассистент.

Оба круга самоубирающиеся — созданное удаляется в конце. Если удаление не
сработает, тестовое событие останется в календаре, и об этом будет сказано
прямо: молча оставлять мусор в чужом календаре нельзя.

Запуск:
    python3 tests/write_cycle.py "Личное"
"""

import json
import os
import subprocess
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "src", "server.py")

TITLE = "ТЕСТ yandex-calendar-mcp — можно удалять"
TITLE_NEW = "ТЕСТ yandex-calendar-mcp — переименовано"
SERIES = "ТЕСТ yandex-calendar-mcp — серия"
SERIES_DAY = "ТЕСТ yandex-calendar-mcp — правленый день серии"
GUESTS = "ТЕСТ yandex-calendar-mcp — с участником"


class Server:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.counter = 0

    def ask(self, method, params=None):
        self.counter += 1
        message = {"jsonrpc": "2.0", "id": self.counter, "method": method}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def call(self, name, arguments):
        answer = self.ask("tools/call", {"name": name, "arguments": arguments})
        result = answer.get("result", {})
        text = "".join(part.get("text", "") for part in result.get("content", []))
        return result.get("isError", False), text

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=15)


def uid_from(text):
    for chunk in text.split("|"):
        if chunk.strip().startswith("id:"):
            return chunk.split("id:", 1)[1].strip()
    return ""


def single_cycle(server, calendar, steps):
    """Разовое событие: создать, отбить чужое название, изменить, удалить."""
    day = (date.today() + timedelta(days=1)).isoformat()

    failed, text = server.call(
        "create_event",
        {
            "calendar": calendar,
            "summary": TITLE,
            "start": f"{day} 14:00",
            "duration_minutes": 30,
            "location": "проверка записи",
        },
    )
    steps.append(("разовое: создание", failed, text))
    if failed:
        return None

    uid = uid_from(text)
    if not uid:
        steps.append(("разовое: разбор метки", True, "в ответе нет id"))
        return None

    # предохранитель: при неверном названии удалять нельзя. Ожидается отказ,
    # то есть здесь «провал» — это как раз успешное удаление.
    failed, text = server.call(
        "delete_event",
        {"uid": uid, "summary": "заведомо чужое название", "calendar": calendar},
    )
    steps.append(("разовое: отказ удалять при несовпадении названия", not failed, text))

    failed, text = server.call(
        "update_event",
        {"uid": uid, "calendar": calendar, "summary": TITLE_NEW, "start": f"{day} 16:30"},
    )
    steps.append(("разовое: изменение", failed, text))

    failed, text = server.call(
        "list_events", {"date_from": day, "date_to": day, "calendar": calendar}
    )
    steps.append(("разовое: видно в выдаче", failed or TITLE_NEW not in text, text))

    failed, text = server.call(
        "delete_event", {"uid": uid, "summary": TITLE_NEW, "calendar": calendar}
    )
    steps.append(("разовое: удаление", failed, text))
    if failed:
        return uid

    failed, text = server.call(
        "list_events", {"date_from": day, "date_to": day, "calendar": calendar}
    )
    steps.append(("разовое: после удаления не видно", failed or TITLE_NEW in text, text))
    return None


def series_cycle(server, calendar, steps):
    """Серия: правка одного дня, удаление одного дня, удаление всей серии."""
    first = date.today() + timedelta(days=1)
    second = first + timedelta(days=7)
    third = first + timedelta(days=14)
    span = {
        "date_from": first.isoformat(),
        "date_to": third.isoformat(),
        "calendar": calendar,
    }

    failed, text = server.call(
        "create_event",
        {
            "calendar": calendar,
            "summary": SERIES,
            "start": f"{first.isoformat()} 12:00",
            "duration_minutes": 30,
            "repeat": "weekly",
            "repeat_count": 3,
        },
    )
    steps.append(("серия: создание трёх повторов", failed, text))
    if failed:
        return None
    uid = uid_from(text)

    failed, text = server.call("list_events", span)
    steps.append(
        ("серия: развернулась в три дня", failed or text.count(SERIES) != 3, text)
    )

    # правка одного дня: остальные дни серии обязаны остаться прежними
    failed, text = server.call(
        "update_event",
        {
            "uid": uid,
            "calendar": calendar,
            "occurrence": second.isoformat(),
            "summary": SERIES_DAY,
            "start": f"{second.isoformat()} 18:00",
        },
    )
    steps.append(("серия: правка одного дня", failed, text))

    failed, text = server.call("list_events", span)
    ok = (not failed) and text.count(SERIES_DAY) == 1 and text.count(SERIES) == 2
    steps.append(("серия: изменился ровно один день", not ok, text))

    # удаление одного дня: сама серия обязана остаться
    failed, text = server.call(
        "delete_event",
        {
            "uid": uid,
            "summary": SERIES,
            "calendar": calendar,
            "occurrence": third.isoformat(),
        },
    )
    steps.append(("серия: удаление одного дня", failed, text))

    failed, text = server.call("list_events", span)
    ok = (not failed) and text.count(SERIES) == 1 and text.count(SERIES_DAY) == 1
    steps.append(("серия: пропал ровно один день", not ok, text))

    failed, text = server.call(
        "delete_event",
        {"uid": uid, "summary": SERIES, "calendar": calendar, "apply_to_series": True},
    )
    steps.append(("серия: удаление всей серии", failed, text))
    if failed:
        return uid

    failed, text = server.call("list_events", span)
    steps.append(
        ("серия: после удаления ничего не осталось", failed or SERIES in text, text)
    )
    return None


def attendee_cycle(server, calendar, address, steps):
    """Участники. Запускается только с явно переданным адресом.

    На этот адрес уйдут настоящие письма: приглашение при добавлении и
    отмена при удалении. Поэтому по умолчанию круг не запускается.
    """
    day = (date.today() + timedelta(days=2)).isoformat()

    failed, text = server.call(
        "create_event",
        {
            "calendar": calendar,
            "summary": GUESTS,
            "start": f"{day} 10:00",
            "duration_minutes": 30,
            "attendees": [f"Проверка связи <{address}>"],
        },
    )
    steps.append(("участники: создание со встречей", failed, text))
    if failed:
        return None
    uid = uid_from(text)

    failed, text = server.call(
        "list_events", {"date_from": day, "date_to": day, "calendar": calendar}
    )
    steps.append(("участники: видны в выдаче", failed or address not in text, text))

    failed, text = server.call(
        "update_event",
        {"uid": uid, "calendar": calendar, "attendees_remove": [address]},
    )
    steps.append(("участники: удаление участника", failed, text))

    failed, text = server.call(
        "list_events", {"date_from": day, "date_to": day, "calendar": calendar}
    )
    steps.append(("участники: после удаления не видны", failed or address in text, text))

    failed, text = server.call(
        "delete_event", {"uid": uid, "summary": GUESTS, "calendar": calendar}
    )
    steps.append(("участники: уборка события", failed, text))
    return None if not failed else uid


def main():
    calendar = sys.argv[1] if len(sys.argv) > 1 else "Личное"
    address = sys.argv[2] if len(sys.argv) > 2 else None
    server = Server()
    server.ask(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "cycle", "version": "1"},
        },
    )

    steps = []
    leftovers = [
        single_cycle(server, calendar, steps),
        series_cycle(server, calendar, steps),
    ]
    if address:
        leftovers.append(attendee_cycle(server, calendar, address, steps))
    else:
        print("Круг с участниками пропущен: адрес не передан вторым доводом.")
        print("Он рассылает настоящие письма, поэтому сам по себе не запускается.")
    server.close()

    report(steps, [uid for uid in leftovers if uid])
    sys.exit(0 if all(not bad for _, bad, _ in steps) else 1)


def report(steps, leftovers):
    for name, bad, text in steps:
        print(f"--- {name}: {'ПРОВАЛ' if bad else 'ok'}")
        print(text)
    if leftovers:
        print()
        print("ВНИМАНИЕ: тестовые события остались в календаре, метки:")
        for uid in leftovers:
            print(" ", uid)
        print("Удалите их вручную.")


if __name__ == "__main__":
    main()
