"""Чтение и изменение Яндекс Календаря по протоколу CalDAV.

Главная забота модуля — часовые пояса. Часы машины, где крутится сервер,
обычно идут по UTC, а пользователь живёт в своём поясе. Поэтому любое время,
которое уходит наружу, приводится к нужному поясу явно, а не «как получится».

Пояс задаётся переменной окружения YANDEX_TIMEZONE, по умолчанию Europe/Moscow.
"""

import copy
import os
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import caldav
from icalendar import Calendar, Event, Timezone, vCalAddress, vText

try:  # разворачивание повторяющихся событий; без него работаем честно, но хуже
    import recurring_ical_events
except ImportError:  # pragma: no cover
    recurring_ical_events = None


DEFAULT_URL = "https://caldav.yandex.ru"
DEFAULT_TZ = "Europe/Moscow"
# Корзина по умолчанию — папка trash рядом с проектом. Переопределяется
# переменной YANDEX_TRASH_DIR; на сервере её стоит задать явно.
DEFAULT_TRASH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trash"
)
MAX_RANGE_DAYS = 92
DEFAULT_DURATION_MINUTES = 60

EMAIL_SHAPE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")

# что можно ответить на чужое приглашение
RESPONSES = {
    "accept": "ACCEPTED",
    "tentative": "TENTATIVE",
    "decline": "DECLINED",
}

# как называются ответы участников на приглашение
PARTSTAT_LABELS = {
    "NEEDS-ACTION": "не ответил",
    "ACCEPTED": "принял",
    "DECLINED": "отказался",
    "TENTATIVE": "под вопросом",
    "DELEGATED": "передал другому",
}


class CalendarError(Exception):
    """Ошибка, текст которой можно показывать пользователю как есть."""


def local_tz() -> ZoneInfo:
    name = os.environ.get("YANDEX_TIMEZONE") or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise CalendarError(
            f"Не найден часовой пояс {name!r}. "
            "Либо имя неверное, либо в системе нет справочника поясов — "
            "поставьте пакет tzdata."
        ) from exc


def _credentials() -> tuple[str, str, str]:
    url = os.environ.get("YANDEX_CALDAV_URL") or DEFAULT_URL
    user = os.environ.get("YANDEX_USERNAME") or ""
    password = os.environ.get("YANDEX_PASSWORD") or ""
    missing = [
        name
        for name, value in (("YANDEX_USERNAME", user), ("YANDEX_PASSWORD", password))
        if not value
    ]
    if missing:
        raise CalendarError(
            "Не заданы переменные окружения: "
            + ", ".join(missing)
            + ". Нужны логин вида имя@yandex.ru и пароль приложения, созданный "
            "на https://id.yandex.ru/security/app-passwords для типа «Календарь "
            "(CalDAV)». Обычный пароль аккаунта для CalDAV не подходит."
        )
    return url, user, password


def _connect() -> "caldav.Principal":
    url, user, password = _credentials()
    try:
        client = caldav.DAVClient(url=url, username=user, password=password)
        return client.principal()
    except Exception as exc:
        raise CalendarError(
            f"Не удалось подключиться к {url}: {exc}. "
            "Частая причина — обычный пароль аккаунта вместо пароля приложения."
        ) from exc


def list_calendars() -> list[dict]:
    calendars = []
    for cal in _connect().calendars():
        try:
            name = cal.name or ""
        except Exception:
            name = ""
        calendars.append({"name": name, "url": str(cal.url)})
    return calendars


def _pick(calendars: list, wanted: str | None) -> list:
    """Выбор календарей по имени. Без имени — все, чтобы ничего не потерять.

    Чужой проект брал calendars[0] вслепую; здесь либо явное совпадение,
    либо честный перебор всех.
    """
    if not wanted:
        return calendars
    needle = wanted.strip().casefold()
    exact = [c for c in calendars if (c.name or "").casefold() == needle]
    if exact:
        return exact
    partial = [c for c in calendars if needle in (c.name or "").casefold()]
    if partial:
        return partial
    known = ", ".join(repr(c.name) for c in calendars) or "ни одного"
    raise CalendarError(f"Календарь {wanted!r} не найден. Доступны: {known}.")


def parse_day(value: str | None, fallback: date) -> date:
    if value is None or value == "":
        return fallback
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise CalendarError(
            f"Дату {value!r} не разобрать. Нужен вид ГГГГ-ММ-ДД, например 2026-08-19."
        ) from exc


def _as_local(value, tz: ZoneInfo):
    """Приводит время события к местному поясу.

    Возвращает (значение, признак «событие на весь день»).
    Дата без времени остаётся датой — это событие на весь день.
    Время без пояса по стандарту iCalendar считается местным.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz), False
        return value.astimezone(tz), False
    if isinstance(value, date):
        return value, True
    return None, False


def _event_bounds(component, tz: ZoneInfo):
    raw_start = component.get("DTSTART")
    if raw_start is None:
        return None, None, False
    start, all_day = _as_local(raw_start.dt, tz)

    raw_end = component.get("DTEND")
    if raw_end is not None:
        end, _ = _as_local(raw_end.dt, tz)
    else:
        duration = component.get("DURATION")
        if duration is not None and start is not None:
            end = start + duration.dt
        elif all_day:
            end = start + timedelta(days=1)
        else:
            end = start
    return start, end, all_day


def _expand(ical, start: datetime, end: datetime):
    """Разворачивает повторяющиеся события в конкретные даты.

    Второе значение — удалось ли развернуть. Если нет, событие всё равно
    вернётся, но будет помечено: лучше честная пометка, чем тихая потеря.
    """
    if recurring_ical_events is not None:
        try:
            return list(recurring_ical_events.of(ical).between(start, end)), True
        except Exception:
            pass
    return list(ical.walk("VEVENT")), False


def list_events(
    date_from: str | None = None,
    date_to: str | None = None,
    calendar: str | None = None,
) -> dict:
    tz = local_tz()
    today = datetime.now(tz).date()
    day_from = parse_day(date_from, today)
    day_to = parse_day(date_to, day_from)

    if day_to < day_from:
        raise CalendarError(f"Конец периода ({day_to}) раньше начала ({day_from}).")
    if (day_to - day_from).days + 1 > MAX_RANGE_DAYS:
        raise CalendarError(
            f"Период больше {MAX_RANGE_DAYS} дней. Запросите отрезок покороче."
        )

    start = datetime.combine(day_from, time.min, tzinfo=tz)
    end = datetime.combine(day_to + timedelta(days=1), time.min, tzinfo=tz)

    principal = _connect()
    try:
        available = principal.calendars()
    except Exception as exc:
        raise CalendarError(f"Не удалось получить список календарей: {exc}") from exc

    events: list[dict] = []
    warnings: list[str] = []

    for cal in _pick(available, calendar):
        cal_name = cal.name or "без имени"
        try:
            found = cal.search(start=start, end=end, event=True, expand=False)
        except Exception as exc:
            warnings.append(f"Календарь «{cal_name}» не ответил: {exc}")
            continue

        for item in found:
            try:
                ical = item.icalendar_instance
            except Exception as exc:
                warnings.append(f"Событие в «{cal_name}» не разобрано: {exc}")
                continue

            components, expanded = _expand(ical, start, end)
            for component in components:
                if component.name != "VEVENT":
                    continue
                if str(component.get("STATUS", "")).upper() == "CANCELLED":
                    continue

                ev_start, ev_end, all_day = _event_bounds(component, tz)
                if ev_start is None:
                    continue

                repeating = component.get("RRULE") is not None
                events.append(
                    {
                        "calendar": cal_name,
                        "summary": str(component.get("SUMMARY", "") or "(без названия)"),
                        "location": str(component.get("LOCATION", "") or ""),
                        "start": ev_start,
                        "end": ev_end,
                        "all_day": all_day,
                        "uid": str(component.get("UID", "") or ""),
                        "repeating": repeating,
                        "expanded": expanded or not repeating,
                        "attendees": _people(component),
                        "organizer": _organizer_of(component),
                        "my_status": _my_status(component),
                    }
                )

    def sort_key(ev):
        value = ev["start"]
        if isinstance(value, datetime):
            return (value.date(), 1, value.time())
        return (value, 0, time.min)

    events.sort(key=sort_key)

    return {
        "timezone": str(tz),
        "now": datetime.now(tz),
        "date_from": day_from,
        "date_to": day_to,
        "events": events,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Запись: создание, изменение, удаление
# ---------------------------------------------------------------------------


def parse_moment(value: str, tz: ZoneInfo) -> datetime:
    """Разбирает «2026-09-01 15:00» или «2026-09-01T15:00» как московское время."""
    if not value or not value.strip():
        raise CalendarError("Не указано время начала.")
    text = value.strip().replace(" ", "T")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CalendarError(
            f"Время {value!r} не разобрать. Нужен вид ГГГГ-ММ-ДД ЧЧ:ММ, "
            "например 2026-09-01 15:00."
        ) from exc
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def _trash_dir() -> str:
    return os.environ.get("YANDEX_TRASH_DIR") or DEFAULT_TRASH


def _snapshot(uid: str, action: str, raw: str) -> str:
    """Сохраняет копию события до изменения или удаления.

    Без успешной копии дальше не идём: удаление в чужом сервисе необратимо,
    и единственное, что делает промах исправимым, — это сохранённый оригинал.
    """
    folder = _trash_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", uid)[:80] or "без-метки"
    path = os.path.join(folder, f"{stamp}-{action}-{safe}.ics")
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(raw)
    except OSError as exc:
        raise CalendarError(
            f"Не удалось сохранить копию события в {folder}: {exc}. "
            "Без копии изменение и удаление не выполняются."
        ) from exc
    return path


def _vevent(ical):
    """Главная запись серии — та, у которой нет метки RECURRENCE-ID.

    Записи с такой меткой — заместители отдельных дней; принимать их за
    серию нельзя, иначе правка уйдёт не туда.
    """
    events = list(ical.walk("VEVENT"))
    for component in events:
        if component.get("RECURRENCE-ID") is None:
            return component
    if events:
        return events[0]
    raise CalendarError("В объекте календаря нет описания встречи.")


def _same_value(one, other) -> bool:
    """Сравнивает значения времени, не падая на смеси даты и даты со временем."""
    if isinstance(one, datetime) != isinstance(other, datetime):
        return False
    try:
        return one == other
    except TypeError:  # одно со сведениями о поясе, другое без
        return False


def parse_occurrence(value: str, tz: ZoneInfo):
    """Разбирает указание на один день серии: дату или дату со временем."""
    text = (value or "").strip().replace(" ", "T")
    if not text:
        raise CalendarError("Не указан день серии.")
    if "T" in text:
        return parse_moment(text, tz)
    return parse_day(text, date.today())


def _instance_start(ical, wanted, tz: ZoneInfo):
    """Находит конкретный повтор серии и его точку начала.

    Возвращает (значение DTSTART этого повтора, сам повтор). Вид значения —
    дата для событий на весь день, время с поясом для остальных — обязан
    совпадать с видом у серии: иначе календарь не поймёт, какой день исключён.
    """
    if recurring_ical_events is None:
        raise CalendarError(
            "Не установлена библиотека recurring_ical_events — без неё нельзя "
            "определить, какой именно повтор серии имеется в виду."
        )

    day = wanted.date() if isinstance(wanted, datetime) else wanted
    window_start = datetime.combine(day, time.min, tzinfo=tz)
    window_end = window_start + timedelta(days=1)
    try:
        found = recurring_ical_events.of(ical).between(window_start, window_end)
    except Exception as exc:
        raise CalendarError(f"Не удалось развернуть серию: {exc}") from exc

    instances = [c for c in found if c.name == "VEVENT" and c.get("DTSTART") is not None]
    if not instances:
        raise CalendarError(
            f"В этой серии нет события {day.isoformat()}. "
            "Проверьте дату по свежей выдаче list_events."
        )

    if isinstance(wanted, datetime):
        exact = [
            c
            for c in instances
            if isinstance(c.get("DTSTART").dt, datetime)
            and c.get("DTSTART").dt.astimezone(tz) == wanted
        ]
        if not exact:
            times = ", ".join(_instance_label(c, tz) for c in instances)
            raise CalendarError(
                f"В день {day.isoformat()} нет повтора на {wanted:%H:%M}. "
                f"Есть: {times}."
            )
        instances = exact

    if len(instances) > 1:
        times = ", ".join(_instance_label(c, tz) for c in instances)
        raise CalendarError(
            f"В день {day.isoformat()} у серии несколько повторов ({times}). "
            "Укажите occurrence вместе со временем: ГГГГ-ММ-ДД ЧЧ:ММ."
        )

    return instances[0].get("DTSTART").dt, instances[0]


def _instance_label(component, tz: ZoneInfo) -> str:
    value = component.get("DTSTART").dt
    if isinstance(value, datetime):
        return f"{value.astimezone(tz):%H:%M}"
    return "весь день"


def _resolve_scope(component, occurrence, apply_to_series: bool, tz: ZoneInfo):
    """Что именно затрагивается: одиночное событие, один день серии или серия.

    Возвращает None для обычного события, строку "series" для всей серии
    и разобранное указание на день — для одного повтора.
    """
    if component.get("RRULE") is None:
        return None
    if occurrence and apply_to_series:
        raise CalendarError(
            "Указано и occurrence, и apply_to_series. Выберите что-то одно: "
            "либо один день серии, либо вся серия целиком."
        )
    if occurrence:
        return parse_occurrence(occurrence, tz)
    if apply_to_series:
        return "series"
    raise CalendarError(
        "Это повторяющееся событие, и непонятно, что имеется в виду. "
        "Укажите occurrence — день серии (ГГГГ-ММ-ДД, а если в этот день "
        "несколько повторов, то с временем), либо apply_to_series=true, чтобы "
        "затронуть всю серию целиком. Ничего не сделано."
    )


def _find_override(ical, start_value):
    """Ищет запись-заместитель для конкретного дня серии."""
    for component in ical.walk("VEVENT"):
        raw = component.get("RECURRENCE-ID")
        if raw is not None and _same_value(raw.dt, start_value):
            return component
    return None


def _override_for(ical, master, instance, original_start):
    """Запись-заместитель для одного дня серии; создаётся, если её ещё нет.

    Заместитель — отдельная запись с меткой RECURRENCE-ID: она говорит
    «вместо повтора такого-то числа читай это». Остальные дни серии живут
    по общему правилу и не затрагиваются.
    """
    existing = _find_override(ical, original_start)
    if existing is not None:
        return existing

    fresh = copy.deepcopy(master)
    for prop in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
        fresh.pop(prop, None)
    fresh.add("recurrence-id", original_start)
    fresh.pop("DTSTART", None)
    fresh.add("dtstart", instance.get("DTSTART").dt)
    fresh.pop("DTEND", None)
    fresh.pop("DURATION", None)
    raw_end = instance.get("DTEND")
    if raw_end is not None:
        fresh.add("dtend", raw_end.dt)
    ical.add_component(fresh)
    return fresh


def _exdates(component) -> list:
    raw = component.get("EXDATE")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    values = []
    for item in items:
        for entry in getattr(item, "dts", []):
            values.append(entry.dt)
    return values


def _add_exdate(component, start_value) -> None:
    """Помечает день серии как исключённый — так удаляется один повтор."""
    existing = _exdates(component)
    if any(_same_value(value, start_value) for value in existing):
        raise CalendarError("Этот день уже исключён из серии, удалять нечего.")
    component.pop("EXDATE", None)
    component.add("exdate", existing + [start_value])


def _find_event(uid: str, calendar: str | None = None):
    if not uid or not uid.strip():
        raise CalendarError("Не указана метка события (id).")
    uid = uid.strip()
    principal = _connect()
    try:
        available = principal.calendars()
    except Exception as exc:
        raise CalendarError(f"Не удалось получить список календарей: {exc}") from exc

    for cal in _pick(available, calendar):
        try:
            found = cal.event_by_uid(uid)
        except Exception:
            continue
        if found is not None:
            return found, (cal.name or "без имени")

    where = f"в календаре {calendar!r}" if calendar else "ни в одном календаре"
    raise CalendarError(
        f"Событие с меткой {uid!r} не найдено {where}. "
        "Возьмите метку из свежей выдачи list_events: она могла устареть."
    )


def _touch(component, bump_sequence: bool = True) -> None:
    """Отмечает правку: часы изменения и, если нужно, номер версии.

    Номер версии принадлежит организатору встречи. Когда мы всего лишь
    отвечаем на чужое приглашение, поднимать его нельзя: для организатора
    это выглядело бы так, будто встречу переписали.
    """
    now = datetime.now(timezone.utc)
    component.pop("DTSTAMP", None)
    component.add("dtstamp", now)
    component.pop("LAST-MODIFIED", None)
    component.add("last-modified", now)
    if not bump_sequence:
        return
    try:
        sequence = int(component.get("SEQUENCE", 0))
    except (TypeError, ValueError):
        sequence = 0
    component.pop("SEQUENCE", None)
    component.add("sequence", sequence + 1)


def _describe(component, tz: ZoneInfo) -> dict:
    start, end, all_day = _event_bounds(component, tz)
    return {
        "summary": str(component.get("SUMMARY", "") or "(без названия)"),
        "location": str(component.get("LOCATION", "") or ""),
        "start": start,
        "end": end,
        "all_day": all_day,
        "uid": str(component.get("UID", "") or ""),
        "repeating": component.get("RRULE") is not None,
        "expanded": True,
        "attendees": _people(component),
        "organizer": _organizer_of(component),
        "my_status": _my_status(component),
    }


def _parse_person(spec: str) -> tuple[str, str]:
    """Разбирает «Имя Фамилия <адрес@почта>» или просто «адрес@почта»."""
    text = (spec or "").strip()
    if not text:
        raise CalendarError("Пустая запись участника.")
    name = ""
    email = text
    if "<" in text and text.endswith(">"):
        name, email = text.split("<", 1)
        name = name.strip().strip('"')
        email = email[:-1].strip()
    if email.lower().startswith("mailto:"):
        email = email[7:]
    email = email.strip()
    if not EMAIL_SHAPE.match(email):
        raise CalendarError(
            f"«{spec}» не похоже на адрес почты. Участник указывается как "
            "адрес@почта или «Имя <адрес@почта>». Адрес нужен точный: "
            "приглашение уходит письмом живому человеку, и отозвать его нельзя."
        )
    return name, email


def _email_of(item) -> str:
    value = str(item).strip()
    if value.lower().startswith("mailto:"):
        value = value[7:]
    return value


def _attendee(name: str, email: str) -> vCalAddress:
    person = vCalAddress(f"mailto:{email}")
    if name:
        person.params["CN"] = vText(name)
    person.params["ROLE"] = vText("REQ-PARTICIPANT")
    person.params["PARTSTAT"] = vText("NEEDS-ACTION")
    person.params["RSVP"] = vText("TRUE")
    return person


def _ensure_organizer(component) -> None:
    """Организатор обязателен: без него сервер не рассылает приглашения."""
    if component.get("ORGANIZER") is not None:
        return
    address = (
        os.environ.get("YANDEX_ORGANIZER") or os.environ.get("YANDEX_USERNAME") or ""
    ).strip()
    if not address:
        raise CalendarError(
            "Неизвестен адрес организатора встречи: не задан YANDEX_USERNAME."
        )
    component.add("organizer", vCalAddress(f"mailto:{address}"), encode=0)


def _people(component) -> list[dict]:
    raw = component.get("ATTENDEE")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    people = []
    for item in items:
        params = getattr(item, "params", {})
        status = str(params.get("PARTSTAT", "NEEDS-ACTION") or "NEEDS-ACTION").upper()
        people.append(
            {
                "email": _email_of(item),
                "name": str(params.get("CN", "") or ""),
                "status": PARTSTAT_LABELS.get(status, status.lower()),
            }
        )
    return people


def _my_addresses() -> set[str]:
    """Адреса, под которыми владелец календаря значится участником."""
    found = set()
    for name in ("YANDEX_SELF", "YANDEX_USERNAME", "YANDEX_ORGANIZER"):
        value = (os.environ.get(name) or "").strip().casefold()
        if value:
            found.add(value)
    return found


def _my_attendee(component):
    """Находит строку участника, которая относится к самому владельцу."""
    mine = _my_addresses()
    raw = component.get("ATTENDEE")
    if raw is None:
        return None
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if _email_of(item).casefold() in mine:
            return item
    return None


def _reread(uid: str, calendar: str | None):
    """Перечитывает событие с сервера. None — если его там больше нет."""
    try:
        obj, _ = _find_event(uid, calendar)
        return obj.icalendar_instance
    except Exception:
        return None


def _ensure_mine(component, action: str) -> None:
    """Запрещает править правило чужой серии.

    Проверено на живых данных: Яндекс принимает такой запрос без ошибки
    и молча его отбрасывает — правило встречи принадлежит организатору.
    Раньше это выглядело как успешное удаление, которого не было.
    """
    organizer = _organizer_of(component)
    if not organizer or organizer.casefold() in _my_addresses():
        return
    raise CalendarError(
        f"Эту встречу создал другой человек ({organizer}), и {action} Яндекс "
        "не сохранит: правило серии принадлежит организатору. Чтобы убрать "
        "встречу из своего расписания, ответьте отказом — respond_event "
        "с response=decline. Организатор получит уведомление, а встреча уйдёт "
        "из вашего календаря. Ничего не сделано."
    )


def _my_status(component) -> str:
    """Ответ самого владельца на это приглашение, если он там участник."""
    item = _my_attendee(component)
    if item is None:
        return ""
    params = getattr(item, "params", {})
    status = str(params.get("PARTSTAT", "NEEDS-ACTION") or "NEEDS-ACTION").upper()
    return PARTSTAT_LABELS.get(status, status.lower())


def _organizer_of(component) -> str:
    raw = component.get("ORGANIZER")
    if raw is None:
        return ""
    return _email_of(raw)


def _set_partstat(component, status: str) -> bool:
    """Ставит ответ владельца в этой записи. Возвращает, нашлась ли строка."""
    raw = component.get("ATTENDEE")
    if raw is None:
        return False
    items = raw if isinstance(raw, list) else [raw]
    mine = _my_addresses()
    touched = False
    rebuilt = []
    for item in items:
        if _email_of(item).casefold() in mine:
            fresh = vCalAddress(str(item))
            for key, value in getattr(item, "params", {}).items():
                fresh.params[key] = value
            fresh.params["PARTSTAT"] = vText(status)
            fresh.params.pop("RSVP", None)
            rebuilt.append(fresh)
            touched = True
        else:
            rebuilt.append(item)
    if not touched:
        return False
    component.pop("ATTENDEE", None)
    for item in rebuilt:
        component.add("attendee", item, encode=0)
    return True


def _apply_attendees(component, add, remove) -> None:
    """Добавляет и убирает участников, сохраняя ответы остальных.

    Список не перезаписывается целиком намеренно: перезапись стёрла бы уже
    полученные ответы «принял» и «отказался» у тех, кого не трогали.
    """
    raw = component.get("ATTENDEE")
    if raw is None:
        items = []
    else:
        items = list(raw) if isinstance(raw, list) else [raw]

    if remove:
        drop = {_parse_person(spec)[1].casefold() for spec in remove}
        kept = [item for item in items if _email_of(item).casefold() not in drop]
        if len(kept) == len(items):
            raise CalendarError(
                "Ни одного из названных участников в событии нет — ничего не убрано. "
                "Сверьте адреса с выдачей list_events."
            )
        items = kept

    if add:
        present = {_email_of(item).casefold() for item in items}
        for spec in add:
            name, email = _parse_person(spec)
            if email.casefold() in present:
                continue
            items.append(_attendee(name, email))
            present.add(email.casefold())

    component.pop("ATTENDEE", None)
    for item in items:
        component.add("attendee", item, encode=0)
    if items:
        _ensure_organizer(component)


REPEAT_RULES = {
    "daily": "DAILY",
    "weekly": "WEEKLY",
    "monthly": "MONTHLY",
    "yearly": "YEARLY",
}


def _repeat_rule(repeat, repeat_count, repeat_until, all_day: bool, tz: ZoneInfo):
    """Собирает правило повтора. Без repeat событие остаётся одиночным."""
    if not repeat or not str(repeat).strip():
        if repeat_count or repeat_until:
            raise CalendarError(
                "Указан предел повторов, но не указан сам повтор: "
                "нужен repeat — daily, weekly, monthly или yearly."
            )
        return None

    freq = REPEAT_RULES.get(str(repeat).strip().lower())
    if freq is None:
        raise CalendarError(
            f"Повтор {repeat!r} не понят. Бывает daily, weekly, monthly, yearly."
        )
    if repeat_count and repeat_until:
        raise CalendarError(
            "Указаны и repeat_count, и repeat_until. Выберите что-то одно: "
            "либо число повторов, либо дату окончания."
        )

    rule = {"freq": freq}
    if repeat_count:
        if int(repeat_count) < 2:
            raise CalendarError("Число повторов должно быть не меньше двух.")
        rule["count"] = int(repeat_count)
    elif repeat_until:
        last_day = parse_day(repeat_until, date.today())
        if all_day:
            rule["until"] = last_day
        else:
            # по стандарту предел повторов у события со временем задаётся в UTC
            rule["until"] = datetime.combine(
                last_day, time(23, 59, 59), tzinfo=tz
            ).astimezone(timezone.utc)
    return rule


def create_event(
    calendar: str,
    summary: str,
    start: str,
    end: str | None = None,
    duration_minutes: int | None = None,
    all_day: bool = False,
    days: int = 1,
    location: str | None = None,
    description: str | None = None,
    repeat: str | None = None,
    repeat_count: int | None = None,
    repeat_until: str | None = None,
    attendees: list | None = None,
) -> dict:
    tz = local_tz()
    if not calendar or not calendar.strip():
        raise CalendarError(
            "Не указан календарь. Календарей несколько и они под разные задачи — "
            "спросите, в какой класть. Список даёт list_calendars."
        )
    if not summary or not summary.strip():
        raise CalendarError("Не указано название события.")

    principal = _connect()
    targets = _pick(principal.calendars(), calendar)
    if len(targets) > 1:
        names = ", ".join(repr(c.name) for c in targets)
        raise CalendarError(
            f"Под описание {calendar!r} подходит несколько календарей: {names}. "
            "Назовите один точно."
        )
    target = targets[0]

    ical = Calendar()
    ical.add("prodid", "-//yandex-calendar-mcp//RU")
    ical.add("version", "2.0")

    event = Event()
    uid = f"{uuid.uuid4()}@yandex-calendar-mcp"
    event.add("uid", uid)
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("summary", summary.strip())

    if all_day:
        day_start = parse_day(start.split("T")[0].split(" ")[0], date.today())
        if days < 1:
            raise CalendarError("Число дней должно быть не меньше одного.")
        event.add("dtstart", day_start)
        event.add("dtend", day_start + timedelta(days=days))
    else:
        moment = parse_moment(start, tz)
        if end:
            finish = parse_moment(end, tz)
        else:
            minutes = duration_minutes or DEFAULT_DURATION_MINUTES
            if minutes < 1:
                raise CalendarError("Длительность должна быть больше нуля.")
            finish = moment + timedelta(minutes=minutes)
        if finish <= moment:
            raise CalendarError(
                f"Конец события ({finish:%Y-%m-%d %H:%M}) не позже начала "
                f"({moment:%Y-%m-%d %H:%M})."
            )
        event.add("dtstart", moment)
        event.add("dtend", finish)
        try:  # описание пояса внутри события — чтобы его правильно понял любой календарь
            ical.add_component(Timezone.from_tzid(str(tz)))
        except Exception:
            pass

    if location and location.strip():
        event.add("location", location.strip())
    if description and description.strip():
        event.add("description", description.strip())

    rule = _repeat_rule(repeat, repeat_count, repeat_until, all_day, tz)
    if rule is not None:
        event.add("rrule", rule)

    if attendees:
        _apply_attendees(event, attendees, None)

    ical.add_component(event)

    try:
        target.save_event(ical.to_ical().decode("utf-8"))
    except Exception as exc:
        raise CalendarError(f"Яндекс не принял событие: {exc}") from exc

    return {
        "timezone": str(tz),
        "calendar": target.name or "без имени",
        "event": _describe(event, tz),
        "uid": uid,
    }


def update_event(
    uid: str,
    calendar: str | None = None,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    duration_minutes: int | None = None,
    location: str | None = None,
    description: str | None = None,
    attendees_add: list | None = None,
    attendees_remove: list | None = None,
    occurrence: str | None = None,
    apply_to_series: bool = False,
) -> dict:
    tz = local_tz()
    if all(
        not value if isinstance(value, list) else value is None
        for value in (
            summary,
            start,
            end,
            duration_minutes,
            location,
            description,
            attendees_add,
            attendees_remove,
        )
    ):
        raise CalendarError("Не указано ни одного изменения.")

    obj, cal_name = _find_event(uid, calendar)
    ical = obj.icalendar_instance
    master = _vevent(ical)
    scope = _resolve_scope(master, occurrence, apply_to_series, tz)

    if scope is None or scope == "series":
        component = master
        before = _describe(master, tz)
        kind = "одиночное событие" if scope is None else "вся серия"
    else:
        # правка одного дня серии: заводится запись-заместитель с меткой
        # RECURRENCE-ID — она говорит «вместо повтора такого-то числа читай это»
        original_start, instance = _instance_start(ical, scope, tz)
        before = _describe(instance, tz)
        kind = "один день серии"
        _ensure_mine(master, "правку одного дня серии")
        component = _override_for(ical, master, instance, original_start)

    backup = _snapshot(str(master.get("UID", "") or uid), "изменение", obj.data)

    if summary is not None:
        if not summary.strip():
            raise CalendarError("Название не может быть пустым.")
        component.pop("SUMMARY", None)
        component.add("summary", summary.strip())

    if location is not None:
        component.pop("LOCATION", None)
        if location.strip():
            component.add("location", location.strip())

    if description is not None:
        component.pop("DESCRIPTION", None)
        if description.strip():
            component.add("description", description.strip())

    if attendees_add or attendees_remove:
        _apply_attendees(component, attendees_add, attendees_remove)

    if start is not None or end is not None or duration_minutes is not None:
        if before["all_day"]:
            raise CalendarError(
                "Это событие на весь день; перенос времени для таких пока не сделан."
            )
        moment = parse_moment(start, tz) if start is not None else before["start"]
        if end is not None:
            finish = parse_moment(end, tz)
        elif duration_minutes is not None:
            if duration_minutes < 1:
                raise CalendarError("Длительность должна быть больше нуля.")
            finish = moment + timedelta(minutes=duration_minutes)
        elif isinstance(before["end"], datetime) and isinstance(before["start"], datetime):
            finish = moment + (before["end"] - before["start"])
        else:
            finish = moment + timedelta(minutes=DEFAULT_DURATION_MINUTES)

        if finish <= moment:
            raise CalendarError(
                f"Конец события ({finish:%Y-%m-%d %H:%M}) не позже начала "
                f"({moment:%Y-%m-%d %H:%M})."
            )

        component.pop("DTSTART", None)
        component.add("dtstart", moment)
        component.pop("DTEND", None)
        component.pop("DURATION", None)
        component.add("dtend", finish)

    _touch(component)

    try:
        obj.data = ical.to_ical().decode("utf-8")
        obj.save()
    except Exception as exc:
        raise CalendarError(
            f"Яндекс не принял изменение: {exc}. Копия события до правки: {backup}"
        ) from exc

    after = _describe(component, tz)
    _confirm_saved(uid, calendar, original_start, after, tz, backup)

    return {
        "timezone": str(tz),
        "calendar": cal_name,
        "scope": kind,
        "before": before,
        "after": after,
        "backup": backup,
    }


def _confirm_saved(uid, calendar, original_start, expected: dict, tz, backup: str) -> None:
    """Перечитывает событие и убеждается, что правка действительно легла.

    Сервер может принять запрос без ошибки и не применить его — так уже было
    с чужими встречами. Рапортовать успех, не проверив, нельзя.
    """
    fresh = _reread(uid, calendar)
    if fresh is None:
        raise CalendarError(
            "После правки событие не читается с сервера — что с ним, неизвестно. "
            f"Копия до правки: {backup}"
        )
    if original_start is None:
        checked = _vevent(fresh)
    else:
        checked = _find_override(fresh, original_start) or _vevent(fresh)
    saved = _describe(checked, tz)

    people_expected = sorted(p["email"].casefold() for p in expected["attendees"])
    people_saved = sorted(p["email"].casefold() for p in saved["attendees"])
    mismatch = [
        name
        for name, one, two in (
            ("название", expected["summary"], saved["summary"]),
            ("время", str(expected["start"]), str(saved["start"])),
            ("место", expected["location"], saved["location"]),
            ("участники", people_expected, people_saved),
        )
        if one != two
    ]
    if not mismatch:
        return
    raise CalendarError(
        "Яндекс принял правку без ошибки, но на сервере она не сохранилась: "
        + ", ".join(mismatch)
        + ". Обычная причина — событие чужое: менять его может только "
        "организатор, а участник может лишь ответить через respond_event. "
        f"Копия до правки: {backup}"
    )


def delete_event(
    uid: str,
    summary: str,
    calendar: str | None = None,
    occurrence: str | None = None,
    apply_to_series: bool = False,
) -> dict:
    """Удаление с предохранителями: сверка названия, копия, отчёт о сделанном.

    Название запрашивается не для красоты: метка события может устареть или
    быть перепутана, и тогда сверка — единственное, что отличает нужное
    событие от чужого.

    Один день серии удаляется не стиранием события, а пометкой «исключённая
    дата» (EXDATE) в самой серии: серия остаётся целой, пропадает только
    названный день.
    """
    tz = local_tz()
    if not summary or not summary.strip():
        raise CalendarError(
            "Не указано название удаляемого события. Оно нужно для сверки: "
            "возьмите его из выдачи list_events ровно как там написано."
        )

    obj, cal_name = _find_event(uid, calendar)
    ical = obj.icalendar_instance
    master = _vevent(ical)
    scope = _resolve_scope(master, occurrence, apply_to_series, tz)

    original_start = None
    if scope is None or scope == "series":
        found = _describe(master, tz)
        kind = "одиночное событие" if scope is None else "вся серия"
    else:
        original_start, instance = _instance_start(ical, scope, tz)
        found = _describe(instance, tz)
        kind = "один день серии"

    if found["summary"].strip().casefold() != summary.strip().casefold():
        raise CalendarError(
            f"Название не совпало. Просили удалить «{summary.strip()}», "
            f"а по этой метке лежит «{found['summary']}» в календаре «{cal_name}». "
            "Ничего не удалено."
        )

    if original_start is not None:
        _ensure_mine(master, "удаление одного дня серии")

    backup = _snapshot(str(master.get("UID", "") or uid), "удаление", obj.data)

    try:
        if original_start is None:
            obj.delete()
        else:
            override = _find_override(ical, original_start)
            if override is not None:
                ical.subcomponents.remove(override)
            _add_exdate(master, original_start)
            _touch(master)
            obj.data = ical.to_ical().decode("utf-8")
            obj.save()
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError(
            f"Яндекс не дал удалить событие: {exc}. Копия события: {backup}"
        ) from exc

    # проверка после записи: сервер мог принять запрос и не применить его
    fresh = _reread(uid, calendar)
    if original_start is None:
        if fresh is not None:
            raise CalendarError(
                "Яндекс принял запрос без ошибки, но событие осталось в календаре. "
                "Скорее всего, оно чужое: удалять чужие события участнику нельзя, "
                "нужен отказ через respond_event с response=decline. "
                f"Ничего не удалено. Копия события: {backup}"
            )
    elif fresh is not None:
        if not any(_same_value(v, original_start) for v in _exdates(_vevent(fresh))):
            raise CalendarError(
                "Яндекс принял запрос без ошибки, но день серии остался на месте — "
                "изменение не сохранилось. Ничего не удалено. "
                f"Копия события: {backup}"
            )

    return {
        "timezone": str(tz),
        "calendar": cal_name,
        "scope": kind,
        "event": found,
        "backup": backup,
    }


def respond_event(
    uid: str,
    summary: str,
    response: str,
    calendar: str | None = None,
    occurrence: str | None = None,
    apply_to_series: bool = False,
) -> dict:
    """Ответ на чужое приглашение: пойду, возможно пойду, не пойду.

    Меняется только собственная строка участника — сама встреча остаётся
    чужой, и трогать её нельзя. Организатору уходит уведомление об ответе.

    Для повторяющейся встречи ответ можно дать на один день или на всю серию,
    ровно как в приложении Яндекса.
    """
    tz = local_tz()
    status = RESPONSES.get((response or "").strip().lower())
    if status is None:
        raise CalendarError(
            f"Ответ {response!r} не понят. Бывает accept — пойду, "
            "tentative — возможно пойду, decline — не пойду."
        )
    if not summary or not summary.strip():
        raise CalendarError(
            "Не указано название встречи. Оно нужно для сверки: возьмите его "
            "из выдачи list_events ровно как там написано."
        )

    obj, cal_name = _find_event(uid, calendar)
    ical = obj.icalendar_instance
    master = _vevent(ical)
    scope = _resolve_scope(master, occurrence, apply_to_series, tz)

    original_start = None
    if scope is None or scope == "series":
        found = _describe(master, tz)
        kind = "встреча" if scope is None else "вся серия"
    else:
        original_start, instance = _instance_start(ical, scope, tz)
        found = _describe(instance, tz)
        kind = "один день серии"

    if found["summary"].strip().casefold() != summary.strip().casefold():
        raise CalendarError(
            f"Название не совпало. Отвечали на «{summary.strip()}», "
            f"а по этой метке лежит «{found['summary']}» в календаре «{cal_name}». "
            "Ответ не отправлен."
        )

    if _my_attendee(master) is None:
        people = ", ".join(person["email"] for person in _people(master)) or "никого"
        raise CalendarError(
            "Вас нет среди участников этой встречи, отвечать не на что. "
            f"В участниках: {people}. Своё собственное событие удаляется "
            "через delete_event, а не ответом на приглашение."
        )

    backup = _snapshot(str(master.get("UID", "") or uid), "ответ", obj.data)

    if original_start is None:
        # ответ на всю серию: у отдельных дней мог стоять свой ответ,
        # его тоже надо привести к общему, иначе останутся расхождения
        targets = list(ical.walk("VEVENT"))
    else:
        targets = [_override_for(ical, master, instance, original_start)]

    changed = False
    for component in targets:
        if _set_partstat(component, status):
            _touch(component, bump_sequence=False)
            changed = True

    if not changed:
        raise CalendarError(
            "Не нашлась ваша строка участника — ничего не изменено. "
            f"Копия события: {backup}"
        )

    try:
        obj.data = ical.to_ical().decode("utf-8")
        obj.save()
    except Exception as exc:
        raise CalendarError(
            f"Яндекс не принял ответ: {exc}. Копия события: {backup}"
        ) from exc

    wanted = PARTSTAT_LABELS.get(status, status.lower())
    try:
        outcome = _confirm_response(uid, calendar, original_start, wanted, status, tz, backup)
    except CalendarError as direct_failure:
        # правку своей копии Яндекс отбрасывает, когда организатор вне Яндекса.
        # Тогда остаётся штатный путь протокола: положить готовый ответ
        # в служебный ящик исходящих, а рассылку сервер берёт на себя.
        try:
            sent = _reply_via_outbox(_connect(), ical, master, status, original_start)
        except CalendarError as outbox_failure:
            raise CalendarError(
                f"{direct_failure} Служебный ящик тоже не помог: {outbox_failure}"
            ) from outbox_failure
        try:
            outcome = _confirm_response(
                uid, calendar, original_start, wanted, status, tz, backup
            )
            outcome += f"; ответ ушёл через служебный ящик ({sent})"
        except CalendarError:
            outcome = (
                f"ответ отправлен организатору через служебный ящик ({sent}), "
                "но в вашем календаре встреча осталась — уберите её вручную "
                "в приложении"
            )

    # показываем состояние с сервера, а не свою черновую копию: иначе
    # в одном ответе рядом окажутся «встреча осталась» и «ваш ответ: отказался»
    after = master if original_start is None else targets[0]
    shown = _describe(after, tz)
    settled = _reread(uid, calendar)
    if settled is not None:
        on_server = (
            _vevent(settled)
            if original_start is None
            else (_find_override(settled, original_start) or _vevent(settled))
        )
        shown["my_status"] = _my_status(on_server)
        shown["attendees"] = _people(on_server)

    return {
        "timezone": str(tz),
        "calendar": cal_name,
        "scope": kind,
        "response": PARTSTAT_LABELS.get(status, status.lower()),
        "outcome": outcome,
        "organizer": _organizer_of(master),
        "event": shown,
        "backup": backup,
    }


def _outbox_url(principal) -> str:
    """Адрес служебного ящика исходящих ответов на приглашения.

    Через него ответ уходит организатору напрямую, минуя правку своей копии
    события. Для встреч с внешним организатором это единственный путь: свою
    копию Яндекс в таком случае менять не даёт.
    """
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><c:schedule-outbox-URL/></d:prop></d:propfind>"
    )
    try:
        response = principal.client.request(
            str(principal.url), "PROPFIND", body, {"Depth": "0"}
        )
    except Exception as exc:
        raise CalendarError(f"Не удалось спросить адрес служебного ящика: {exc}") from exc

    raw = getattr(response, "raw", "") or ""
    text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    block = re.search(
        r"schedule-outbox-url[^>]*>(.*?)</[^>]*schedule-outbox-url>", text, re.I | re.S
    )
    href = re.search(r"<[^>]*href>([^<]+)<", block.group(1), re.I) if block else None
    if href is None:
        raise CalendarError(
            "Сервер не сообщил адрес служебного ящика исходящих ответов — "
            "отправить ответ этим путём нельзя."
        )
    return urljoin(str(principal.client.url), href.group(1).strip())


def _reply_via_outbox(principal, ical, master, status: str, recurrence_id) -> str:
    """Отправляет организатору готовый ответ через служебный ящик.

    Возвращает то, что сервер сказал о доставке.
    """
    organizer = _organizer_of(master)
    if not organizer:
        raise CalendarError("У встречи не указан организатор — ответить некому.")

    mine = _my_attendee(master)
    if mine is None:
        raise CalendarError("Вас нет среди участников встречи.")
    my_address = _email_of(mine)

    reply = Calendar()
    reply.add("prodid", "-//yandex-calendar-mcp//RU")
    reply.add("version", "2.0")
    reply.add("method", "REPLY")

    # описание пояса берём из самого события: время в ответе обязано
    # читаться так же, как в оригинале
    for component in ical.walk("VTIMEZONE"):
        reply.add_component(copy.deepcopy(component))

    event = Event()
    event.add("uid", str(master.get("UID", "")))
    event.add("dtstamp", datetime.now(timezone.utc))
    raw_start = master.get("DTSTART")
    if raw_start is not None:
        event.add("dtstart", raw_start.dt)
    if recurrence_id is not None:
        event.add("recurrence-id", recurrence_id)
    summary = master.get("SUMMARY")
    if summary is not None:
        event.add("summary", str(summary))
    try:
        event.add("sequence", int(master.get("SEQUENCE", 0) or 0))
    except (TypeError, ValueError):
        event.add("sequence", 0)

    event.add("organizer", vCalAddress(f"mailto:{organizer}"), encode=0)
    answer = vCalAddress(f"mailto:{my_address}")
    answer.params["PARTSTAT"] = vText(status)
    event.add("attendee", answer, encode=0)
    reply.add_component(event)

    url = _outbox_url(principal)
    try:
        response = principal.client.request(
            url,
            "POST",
            reply.to_ical(),
            {
                "Content-Type": "text/calendar; charset=utf-8",
                "Originator": f"mailto:{my_address}",
                "Recipient": f"mailto:{organizer}",
            },
        )
    except Exception as exc:
        raise CalendarError(f"Служебный ящик не принял ответ: {exc}") from exc

    code = getattr(response, "status", 0)
    raw = getattr(response, "raw", "") or ""
    text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    if code not in (200, 201, 202, 204):
        raise CalendarError(
            f"Служебный ящик ответил кодом {code}. Ответ не отправлен. "
            f"{text[:300]}"
        )
    reported = re.search(r"request-status[^>]*>([^<]+)<", text, re.I)
    return reported.group(1).strip() if reported else f"код {code}"


def _occurrence_present(ical, start_value, tz) -> bool:
    """Остался ли этот день серии в календаре после ответа."""
    try:
        found, _ = _instance_start(ical, start_value, tz)
    except CalendarError:
        return False
    return _same_value(found, start_value)


def _confirm_response(uid, calendar, original_start, wanted, status, tz, backup) -> str:
    """Убеждается, что ответ действительно принят, и говорит, чем это вышло.

    У Яндекса отказ от одного дня чужой серии выглядит не как сохранённая
    отметка «отказался», а как исчезновение этого дня из календаря. Это тоже
    успех, и проверка обязана его признавать — иначе честный инструмент
    сообщит о неудаче там, где всё получилось.
    """
    fresh = _reread(uid, calendar)
    if fresh is None:
        if status == "DECLINED":
            return "встреча убрана из календаря целиком"
        raise CalendarError(
            "После ответа событие не читается с сервера — что с ним, неизвестно. "
            f"Копия до ответа: {backup}"
        )

    if original_start is None:
        saved = _my_status(_vevent(fresh))
        if saved == wanted:
            return f"отметка участия: {wanted}"
        raise CalendarError(
            f"Яндекс принял ответ без ошибки, но на сервере по-прежнему стоит "
            f"«{saved or 'ничего'}», а не «{wanted}». Ответ не сохранён. "
            f"Копия до ответа: {backup}"
        )

    override = _find_override(fresh, original_start)
    if override is not None and _my_status(override) == wanted:
        return f"отметка участия на этот день: {wanted}"

    if not _occurrence_present(fresh, original_start, tz):
        if status == "DECLINED":
            return "этот день убран из календаря, остальные дни серии на месте"
        raise CalendarError(
            "День пропал из календаря, хотя ответ был не отказом. "
            f"Проверьте расписание вручную. Копия до ответа: {backup}"
        )

    saved = _my_status(override) if override is not None else _my_status(_vevent(fresh))
    raise CalendarError(
        f"Яндекс принял ответ без ошибки, но на сервере по-прежнему стоит "
        f"«{saved or 'ничего'}», а не «{wanted}», и день остался в календаре. "
        f"Ответ не сохранён. Копия до ответа: {backup}"
    )
