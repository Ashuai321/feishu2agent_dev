# Calendar Event Editing

Use the target agent's existing Yuanbo calendar routing and Memory for calendar identity and timezone. Never copy fixed account names, IDs, mailbox values, or timezone defaults from another agent.

## Workflow
1. Resolve the target calendar from the user's request and Yuanbo's existing routing. Do not guess an ID from a display name.
2. Resolve date/time using an explicit timezone, otherwise the reliable Yuanbo Memory default. Never silently substitute the calendar's configured timezone.
3. Before a new event, search a bounded window across visible relevant calendars for strong duplicate/conflict candidates.
4. For a modification, show distinguishing details, confirm the exact candidate, read it fully, and stop if it is read-only instead of redirecting silently.
5. If no strong match exists, prepare a new event and require explicit confirmation.
6. For a deletion or cancellation, identify the exact event and prepare a deletion proposal before calling the delete/cancel action. State the target calendar, event title/ID, local start/end time and timezone, and whether the whole event, one occurrence, or the entire recurring series will be removed. Require explicit confirmation for that exact proposal.
7. Never add the requester as a guest. Add other guests only when explicitly requested; preserve attendees on updates unless asked to change them.
8. Resolve Location from the user's explicit request. Do not invent Zoom, Lark, phone, links, or numbers.
9. Apply color only when explicitly requested or required by the target workflow. Resolve the live palette; never guess IDs.
10. For recurrence, prefer a finite series when cadence repeats; calculate occurrence dates and counts, split series when times differ, and ask when allocation is ambiguous.
11. Final confirmation must state create, modify, delete, or cancel, target calendar, local date/time/timezone, recurrence/counts and deletion scope where relevant, Location, guests, color, and every field that will change.
12. After success, report the result and direct event URL; if no URL is returned, re-read once when possible and never invent one.

### Explicit mutation confirmation gate

The user's initial wording is never itself a write confirmation. Before invoking any create, update, delete, cancel, recurrence, or invitation-response action:

1. Finish all read-only discovery, exact-target checks, timezone resolution, conflict checks, and required-field validation.
2. Present one complete proposal for the exact operation. For deletion/cancellation, show the complete event identity and the deletion scope, including whether a single occurrence or the whole series is affected.
3. Ask for an operation-specific confirmation that refers to that immediately preceding proposal: `确认创建`, `确认修改`, `确认删除`, or `确认取消` (English equivalents are allowed).
4. Invoke the write action only after the matching confirmation arrives. A vague acknowledgement, a new request, or a changed field is not confirmation; re-propose instead.
5. After the write, re-read the target once and report the result and direct event URL when available.

For create or modify proposals that require a static PNG preview, the image shown in ChatGPT is not
automatically delivered to Feishu/Lark. Call the relay MCP `send_image` with an actual HTTPS URL,
data URL, or base64 payload and wait for success before calling `record_result`; a Markdown image
link or ChatGPT-side attachment alone is not delivery. Preserve the template's visual structure, but
do not reject or withhold a successfully rendered image solely because its pixel dimensions or aspect
ratio differ from the reference. If image rendering or sending fails, return the exact failure and do
not claim that the preview reached Feishu/Lark.

## Guardrails
Every historical modification, deletion/cancellation, new creation, recurrence mutation, and invitation response requires confirmation. Do not infer criticality, uncertainty, meeting type, timezone, attendees, reminders, conferencing, recurrence, visibility, deletion scope, or other field changes. For recurring updates or deletions, ask whether the change is one occurrence or the series. If a requested palette color is unavailable, explain the mismatch and ask before proceeding.
