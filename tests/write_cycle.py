#!/usr/bin/env python3
"""Полный круг записи: создать событие, изменить, найти в выдаче, удалить.

Проверка идёт через настоящий протокол: запускается server.py как отдельная
программа, и с ней ведётся тот же обмен строками JSON, что ведёт ассистент.

Круг самоубирающийся — созданное событие удаляется в конце. Если удаление
не сработает, тестовое событие останется в календаре, и об этом будет сказано
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


def main():
    calendar = sys.argv[1] if len(sys.argv) > 1 else "Личное"
    day = (date.today() + timedelta(days=1)).isoformat()

    server = Server()
    server.ask(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "cycle", "version": "1"}},
    )

    steps = []

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
    steps.append(("создание", failed, text))
    if failed:
        report(steps, None)
        server.close()
        sys.exit(1)

    uid = ""
    for chunk in text.split("|"):
        if chunk.strip().startswith("id:"):
            uid = chunk.split("id:", 1)[1].strip()
    if not uid:
        steps.append(("разбор метки", True, "в ответе нет id созданного события"))
        report(steps, None)
        server.close()
        sys.exit(1)

    # предохранитель: при неверном названии удалять нельзя. Ожидается отказ,
    # то есть здесь «провал» — это как раз успешное удаление.
    failed, text = server.call(
        "delete_event",
        {"uid": uid, "summary": "заведомо чужое название", "calendar": calendar},
    )
    steps.append(("отказ удалять при несовпадении названия", not failed, text))

    failed, text = server.call(
        "update_event",
        {"uid": uid, "calendar": calendar, "summary": TITLE_NEW, "start": f"{day} 16:30"},
    )
    steps.append(("изменение", failed, text))

    failed, text = server.call("list_events", {"date_from": day, "date_to": day, "calendar": calendar})
    steps.append(("видно в выдаче", failed or TITLE_NEW not in text, text))

    failed, text = server.call("delete_event", {"uid": uid, "summary": TITLE_NEW, "calendar": calendar})
    steps.append(("удаление", failed, text))
    deleted = not failed

    failed, text = server.call("list_events", {"date_from": day, "date_to": day, "calendar": calendar})
    steps.append(("после удаления не видно", failed or TITLE_NEW in text, text))

    server.close()
    report(steps, uid if not deleted else None)
    sys.exit(0 if all(not bad for _, bad, _ in steps) else 1)


def report(steps, leftover_uid):
    for name, bad, text in steps:
        mark = "ПРОВАЛ" if bad else "ok"
        print(f"--- {name}: {mark}")
        print(text)
    if leftover_uid:
        print()
        print("ВНИМАНИЕ: тестовое событие осталось в календаре, метка:", leftover_uid)
        print("Удалите его вручную.")


if __name__ == "__main__":
    main()
