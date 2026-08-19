# yandex-calendar-mcp

An MCP server that gives an AI assistant access to **Yandex Calendar** over
CalDAV. Read the schedule, create, move and delete events.

[Русская версия](README.ru.md)

> **Note on language:** tool descriptions and responses are in **Russian**,
> because the server was written for a Russian-speaking assistant. The code,
> configuration and this document are in English. Everything else works
> regardless of language.

## Why another one

Existing implementations get time zones wrong in a way that is quiet and
therefore dangerous — the numbers look plausible and are three hours off. This
server treats time zones as the primary concern:

- Every timestamp is converted to the configured zone with `zoneinfo`. Time
  strings are never sliced by character position.
- Written events carry an explicit `DTSTART;TZID=…` plus a full `VTIMEZONE`
  block, so any calendar client reads them the same way.
- The answer always states the zone and the UTC offset it used, so a mistake
  is visible instead of silent.

Beyond that:

- **Recurring events are expanded** into concrete dates. If expansion fails,
  the event is returned with a warning rather than presented as accurate.
- **Calendars are selected by name.** With no name given, reading spans all
  calendars and every event is labelled with the one it came from — no
  "first calendar in the list" guesswork.
- **Editing exists**, which matters more day to day than creating.
- **Deleting is guarded** — see below.

## Requirements

- Python 3.10 or newer (3.13 tested)
- A Yandex **app password**, not the account password:
  https://id.yandex.ru/security/app-passwords → application type
  "Календарь (CalDAV)"

```
pip install -r requirements.txt
```

`tzdata` is in the requirements on purpose: slim Linux images often ship
without a time zone database, and `ZoneInfo("Europe/Moscow")` then fails.

## Configuration

All settings come from environment variables.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `YANDEX_USERNAME` | yes | — | `name@yandex.ru` |
| `YANDEX_PASSWORD` | yes | — | app password |
| `YANDEX_CALDAV_URL` | no | `https://caldav.yandex.ru` | CalDAV endpoint |
| `YANDEX_TIMEZONE` | no | `Europe/Moscow` | IANA zone for all input and output |
| `YANDEX_TRASH_DIR` | no | `<project>/trash` | where copies of changed and deleted events are kept |

The server speaks MCP over stdio. Example client configuration is in
[`examples/`](examples/).

## Tools

| Tool | What it does |
|---|---|
| `list_events` | events for a period; defaults to today; expands recurrences |
| `list_calendars` | calendar names and URLs |
| `create_event` | create an event; `calendar` is required |
| `update_event` | move, rename, change location or description |
| `delete_event` | delete an event |

`create_event` deliberately requires an explicit calendar. People keep several
calendars for different purposes, and an event silently filed into the wrong
one is an event that is lost.

## Deleting is irreversible — what protects you

1. **Title check.** `delete_event` takes both the `uid` and the event title.
   They are compared; on mismatch nothing is deleted and the response names
   both titles and the calendar. A stale or confused `uid` is the realistic
   way to destroy the wrong event, and this is what catches it.
2. **Trash.** Before every delete *and* every update, the full original event
   is written to `YANDEX_TRASH_DIR` as a timestamped `.ics` file. If the copy
   cannot be written, the operation does not run at all. Recovery is importing
   that file back.
3. **Recurring series.** Editing or deleting a recurring event affects the
   **entire series**. Without an explicit `apply_to_series` flag the tool
   refuses and explains why. Changing a single occurrence is not supported.

## Testing

```
python tests/write_cycle.py "Calendar name"
```

Runs a full self-cleaning round against a real calendar through the actual
protocol: create → attempt to delete with a wrong title (must be refused) →
move and rename → confirm it appears in the listing → delete → confirm it is
gone. If the final delete fails, the leftover event's `uid` is printed instead
of being silently left behind.

`tests/probe.jsonl` is a set of raw protocol messages for checking the
handshake and read path:

```
python src/server.py < tests/probe.jsonl
```

## Known limitations

- A single occurrence of a recurring series cannot be edited or deleted —
  only the whole series.
- All-day events can be created, but their time cannot be moved.
- Attendees, reminders and invitations are not handled.
- Verified against Yandex Calendar only. Other CalDAV servers are likely to
  work but are untested.

## License

MIT — see [LICENSE](LICENSE).
