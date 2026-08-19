# Tests / Проверки

Both checks talk to the server through the real MCP protocol — not through a
"similar" code path — and both need working credentials in the environment.

Обе проверки разговаривают с сервером по настоящему протоколу MCP, а не по
похожему пути, и обеим нужны рабочие переменные окружения с логином и паролем.

## probe.jsonl — handshake and reading

Raw protocol messages, one per line: `initialize`, `ping`, `tools/list`,
`list_calendars`, `list_events` for today. Read-only.

```
python src/server.py < tests/probe.jsonl
```

## write_cycle.py — the full write cycle

```
python tests/write_cycle.py "Calendar name"
```

Two rounds. First a one-off event tomorrow:

1. tries to delete it with a **wrong title** — this must be **refused**;
2. moves it and renames it;
3. checks that it appears in the day's listing;
4. deletes it with the correct title;
5. checks that it is gone.

Then a weekly series of three occurrences:

6. checks it expands into exactly three days;
7. edits the **middle occurrence** and checks that exactly one day changed;
8. deletes the **last occurrence** and checks that exactly one day vanished;
9. deletes the whole series and checks nothing is left.

Шаги 7 и 8 — главные: они доказывают, что правка и удаление одного дня серии
не задевают остальные дни.

The cycle cleans up after itself. If the final delete fails, the leftover
event's `uid` is printed — leaving rubbish in someone's real calendar without
saying so is not acceptable.

Круг самоубирающийся. Если удаление не сработает, программа напечатает метку
оставшегося события: молча оставлять мусор в чужом календаре нельзя.

## What success means for reading

Matching times **as the calendar owner sees them in the Yandex app**. Numbers
that merely look plausible prove nothing: a three-hour shift looks entirely
convincing. Compare against the app before trusting a reading path.

Совпадение времени с тем, что владелец календаря видит **глазами в приложении
Яндекса**. Правдоподобные числа не доказывают ничего: сдвиг на три часа
выглядит совершенно убедительно.
