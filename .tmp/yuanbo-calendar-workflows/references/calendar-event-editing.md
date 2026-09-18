# Calendar Event Editing

Use the target agent's existing Yuanbo calendar routing and Memory for calendar identity and timezone. Never copy fixed account names, IDs, mailbox values, or timezone defaults from another agent.

## Workflow
1. Resolve the target calendar from the user's request and Yuanbo's existing routing. Do not guess an ID from a display name.
2. Resolve date/time using an explicit timezone, otherwise the reliable Yuanbo Memory default. Never silently substitute the calendar's configured timezone.
3. Before a new event, search a bounded window across visible relevant calendars for strong duplicate/conflict candidates.
4. For a modification, show distinguishing details, confirm the exact candidate, read it fully, and stop if it is read-only instead of redirecting silently.
5. If no strong match exists, prepare a new event and require explicit confirmation.
6. Never add the requester as a guest. Add other guests only when explicitly requested; preserve attendees on updates unless asked to change them.
7. Resolve Location from the user's explicit request. Do not invent Zoom, Lark, phone, links, or numbers.
8. Apply color only when explicitly requested or required by the target workflow. Resolve the live palette; never guess IDs.
9. For recurrence, prefer a finite series when cadence repeats; calculate occurrence dates and counts, split series when times differ, and ask when allocation is ambiguous.
10. Final confirmation must state create vs modify, target calendar, local date/time/timezone, recurrence/counts, Location, guests, color, and every field that will change.
11. After success, report the result and direct event URL; if no URL is returned, re-read once when possible and never invent one.

## Guardrails
Every historical modification and new creation requires confirmation. Do not infer criticality, uncertainty, meeting type, timezone, attendees, reminders, conferencing, recurrence, visibility, or other field changes. For recurring updates, ask whether the change is one occurrence or the series. If a requested palette color is unavailable, explain the mismatch and ask before proceeding.
