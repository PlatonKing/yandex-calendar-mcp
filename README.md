# yandex-calendar-mcp

An MCP server that gives an AI assistant access to **Yandex Calendar** over
CalDAV. Read the schedule, create, move and delete events.

[Русская версия](README.ru.md)

> **Note on language:** tool descriptions and responses are in **Russian**,
> because the server was written for a Russian-speaking assistant. The code,
> configuration and this document are in English. Everything else works
> regardless of language.

## What it does

**Reading**

- Events for any period: today by default, or an explicit range up to 92 days.
- All calendars at once, with every event labelled by the calendar it came
  from — or a single calendar selected by name.
- Recurring events expanded into concrete dates. If expansion fails, the event
  is returned with a warning rather than presented as accurate.
- Attendees listed with how each of them replied — accepted, declined,
  tentative or no answer yet.
- All-day events recognised as such, not shown as midnight appointments.
- Every answer states the time zone and UTC offset it used.

**Writing**

- Create an event: title, start, end or duration, location, description;
  timed or all-day spanning several days.
- Create a recurring event — daily, weekly, monthly or yearly — bounded by a
  number of occurrences or by an end date.
- Update an event: move it, rename it, change location or description. Only
  the fields you pass are touched; a moved event keeps its original duration
  unless you say otherwise.
- Edit or delete **a single occurrence** of a recurring series, or the whole
  series. A changed day becomes a `RECURRENCE-ID` override, a removed day an
  `EXDATE` — the standard mechanisms, so the result looks right in the Yandex
  app and in any other client.
- Invite people: attendees can be added when creating an event, and added or
  removed later. Yandex mails the invitations and cancellations itself.
- Delete an event, with the safeguards described below.

**Time zones, handled deliberately**

- Every timestamp is converted to the configured zone with `zoneinfo`. Time
  strings are never sliced by character position.
- Written events carry an explicit `DTSTART;TZID=…` plus a full `VTIMEZONE`
  block, so any calendar client reads them the same way.
- The machine running the server is usually on UTC; that never leaks into the
  answers, and the zone actually used is named in every response, so a mistake
  is visible instead of silent.

**Built for an assistant to drive**

- Responses are plain readable text with the event `uid` included, so an edit
  or delete can follow a listing directly.
- `create_event` requires an explicit calendar: people keep several calendars
  for different purposes, and an event silently filed into the wrong one is an
  event that is lost. The assistant is expected to ask rather than guess.
- Errors come back as sentences explaining what to do next, not stack traces.

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
| `YANDEX_ORGANIZER` | no | value of `YANDEX_USERNAME` | address that appears as the meeting organizer in invitations |

The server speaks MCP over stdio. Example client configuration is in
[`examples/`](examples/).

## Tools

| Tool | What it does |
|---|---|
| `list_events` | events for a period; defaults to today; expands recurrences |
| `list_calendars` | calendar names and URLs |
| `create_event` | create an event, one-off or recurring, with attendees; `calendar` is required |
| `update_event` | move, rename, change location or description, add or remove attendees; one day of a series or the whole series |
| `delete_event` | delete an event; one day of a series or the whole series |

## Deleting is irreversible — what protects you

1. **Title check.** `delete_event` takes both the `uid` and the event title.
   They are compared; on mismatch nothing is deleted and the response names
   both titles and the calendar. A stale or confused `uid` is the realistic
   way to destroy the wrong event, and this is what catches it.
2. **Trash.** Before every delete *and* every update, the full original event
   is written to `YANDEX_TRASH_DIR` as a timestamped `.ics` file. If the copy
   cannot be written, the operation does not run at all. Recovery is importing
   that file back.
3. **Recurring series.** Any edit or delete on a series has to say what it
   applies to: `occurrence` for one day, `apply_to_series` for the whole
   series. With neither, the tool refuses and explains the choice. This is
   what keeps "cancel Tuesday" from erasing a year of meetings.

## Inviting people sends real email

Yandex advertises `calendar-auto-schedule`, so adding an attendee makes the
server send an invitation, and removing one sends a cancellation. Neither can
be recalled. This was verified end to end, not merely from the server's
advertised capabilities: an invitation added through `create_event` arrived in
the recipient's mailbox.

What follows from that:

- Addresses are taken literally and validated. The server never derives an
  address from a name — an assistant that half-remembers a contact would
  otherwise email a stranger.
- `update_event` **adds and removes** attendees instead of replacing the list,
  so answers already given by the others are preserved.
- Every response lists exactly who is on the event and how they replied, so a
  wrong address is visible immediately.

An assistant driving this server should confirm the address list with its user
before calling. Sending an invitation is not an undoable action.

## Testing

```
python tests/write_cycle.py "Calendar name"
```

Self-cleaning rounds against a real calendar through the actual protocol. A
one-off event: create → attempt to delete with a wrong title (must be refused)
→ move and rename → confirm it appears in the listing → delete → confirm it is
gone. A three-occurrence series: edit one day, delete one day, delete the whole
series, checking each time that exactly one day changed. If a delete fails, the
leftover event's `uid` is printed instead of being silently left behind.

The attendee round only runs when an address is passed explicitly, because it
sends real email:

```
python tests/write_cycle.py "Calendar name" someone@example.com
```

`tests/probe.jsonl` is a set of raw protocol messages for checking the
handshake and read path:

```
python src/server.py < tests/probe.jsonl
```

## Known limitations

- All-day events can be created, but their time cannot be moved.
- Recurrence rules are created in their common forms (every day / week /
  month / year, with a count or an end date). More elaborate rules — "every
  second Tuesday", weekday sets — are read and expanded correctly but cannot
  be created through the tool.
- Reminders and alarms are not handled.
- Attendees are always invited as required participants; optional attendees
  and per-attendee roles are not exposed.
- Verified against Yandex Calendar only. Other CalDAV servers are likely to
  work but are untested.

## License

MIT — see [LICENSE](LICENSE).
