# Calendar Event Editing

In Relay mode, use the current sender's verified persistent memory described below for updated personal rules and aliases. Use the target agent's configured calendar connection for account identity and timezone details not changed by those preferences. Never copy fixed account names, IDs, mailbox values, or timezone defaults from another agent.

## Persistent per-user memory in Relay mode

Follow the Agent Instructions' persistent-memory workflow. Before using this reference's static routing or color rules in Relay mode, apply the newest verified memory for the current sender; it must affect future searches, proposals, and writes. The current explicit request and shared safety/confirmation requirements take precedence. If memory is unavailable, pause a write whose target depends on saved preferences.

## Authoritative calendar routing

Use this table before every search, read, create, update, delete, cancel, recurrence, or invitation-response action. Never use the Google Calendar connector's default calendar.

- Explicit `Perfect710` only: `perfect710\@gmail.com`, unless the current sender has a newer verified memory explicitly revising this routing.
- 小奇, 小果, 果果, or 诺诺: `Family`, `family05788726987850932990\@group.calendar.google.com`.
- Work-related: ask the user to choose `Bo Bozway` (`bo\@bozway.com`) or `Bo YW` (`bo\@you.world`); do not choose silently unless the current sender has a newer verified memory explicitly revising this rule.
- Explicit `Bo Bozway`, `Bo YW`, or `Family`: honor that named calendar.
- No calendar name, no family/work cue, or unresolved nature: default to `Bo Bozway` (`bo\@bozway.com`) unless the current sender has a newer verified memory explicitly revising this default.
- Family cues outrank generic classification. Explicit calendar names outrank inferred nature, but `Perfect710` still requires the explicit name unless the current sender has a newer verified memory explicitly revising that routing.

## Authoritative event color routing

For every create and modify, compute one color before the proposal, show it in the text proposal, and use the same color in the write:

1. Uncertain, missing, approximate, unresolved, or date-only time: **pink**, regardless of modality or location.
2. Definite time plus explicit online/线上: **green**, even if a location is present.
3. If neither online/offline is specified and no actual location is supplied, use **green**. Blank, “not provided,” and “leave blank” are not actual locations.
4. Definite time plus explicit offline/线下 or an actual location: **purple**, unless explicit online/线上 was specified (which follows rule 2).
5. If the user explicitly specifies both online and offline and the intended mode is unclear, ask for clarification instead of guessing the color.

Resolve the live Google Calendar palette for the named color. Never use the connector default or silently substitute blue; if unavailable, report the mismatch and stop before writing.

## Workflow
1. Resolve the target calendar with the authoritative routing table above and use its exact ID. Do not derive an ID from a display name.
2. Resolve date/time using an explicit timezone, otherwise the reliable Yuanbo Memory default. Never silently substitute the calendar's configured timezone.
3. Before a new event, search a bounded window across visible relevant calendars for strong duplicate/conflict candidates.
4. For a modification, show distinguishing details, confirm the exact candidate, read it fully, and stop if it is read-only instead of redirecting silently.
5. If no strong match exists, prepare a new event and require explicit confirmation.
6. For a deletion or cancellation, identify the exact event and prepare a deletion proposal before calling the delete/cancel action. State the target calendar, event title/ID, local start/end time and timezone, and whether the whole event, one occurrence, or the entire recurring series will be removed. Require explicit confirmation for that exact proposal.
7. Never add the requester as a guest. Add other guests only when explicitly requested; preserve attendees on updates unless asked to change them.
8. Resolve Location from the user's explicit request. Do not invent Zoom, Lark, phone, links, or numbers.
9. Apply the authoritative color routing above. Resolve the live palette; never guess IDs.
10. For recurrence, prefer a finite series when cadence repeats; calculate occurrence dates and counts, split series when times differ, and ask when allocation is ambiguous.
11. Final confirmation must state create, modify, delete, or cancel, target calendar, local date/time/timezone, recurrence/counts and deletion scope where relevant, Location, guests, color, and every field that will change.
12. After a successful create or update, report the direct event URL; if the write returns no URL, re-read once when possible and never invent one. After a successful deletion, do not re-read the deleted event; follow the deletion verification rule below.

### Explicit mutation confirmation gate

The user's initial wording is never itself a write confirmation. Before invoking any create, update, delete, cancel, recurrence, or invitation-response action:

1. Finish all read-only discovery, exact-target checks, timezone resolution, conflict checks, and required-field validation.
2. Present one complete proposal for the exact operation. For deletion/cancellation, show the complete event identity and the deletion scope, including whether a single occurrence or the whole series is affected.
3. Ask for a confirmation that refers to that immediately preceding proposal. The operation-specific forms
   `确认创建`, `确认修改`, `确认删除`, or `确认取消` remain supported; the positive/negative response
   classification below also applies to create, update, delete, and cancel proposals.
4. Invoke the write action only after a positive confirmation classified below arrives. A new request or a changed field
   is not confirmation; re-propose instead. A response classified as refusal must never call the write action.
5. After a successful create or update, re-read the target once when needed for its direct event URL. For a successful deletion, do not re-read the deleted event; follow the deletion verification rule below.

For create, update, delete, or cancel proposals, a direct reply of `执行`, `开始`, `接受`, `确认`, `行`, `行的`, `好`, `好的`,
`可以`, `yes`, `ok`, `同意`, `确认创建`, `确认修改`, `确认删除`, or `确认取消` confirms the immediately preceding
proposal after surrounding whitespace, case, and punctuation are normalized. A direct phrase such as `好的，执行` also
confirms it. Explicit refusal has priority over any positive token: `不执行`, `不开始`, `不接受`, `不确认`, `不行`,
`不行的`, `不好`, `不好的`, `不可以`, `no`, or `不确认创建/修改/删除/取消` rejects the proposal and must not call a
write tool, even if the same reply also contains `确认` or `可以`. A reply that changes fields, arrives without a pending
proposal, or has neither clear polarity is not confirmation and requires a new question or proposal. This confirmation
classification does not change discovery, conflict, or write safeguards.

For ordinary create or modify proposals, do not render, generate, attach, upload, or send a PNG preview. Do not call the relay MCP `send_image` solely for a calendar proposal. Keep the complete operation details in the text proposal and ask for the matching confirmation. User-provided itinerary images and image-to-event extraction remain governed by `references/pic-to-event.md`; this no-preview rule concerns only generated calendar previews.

After a successful create or modify, re-read the target once and reply exactly with `创建成功，日程链接：<direct event URL>` or `修改成功，日程链接：<direct event URL>`. The URL must come from the write result or the successful re-read; never invent one. If the connected calendar tool returns no direct URL, state that the URL is unavailable instead of substituting an ID or Markdown link.

For a confirmed Google Calendar deletion, a delete-tool call that completes without an error is success even if the response body is empty; the Google Calendar delete API returns an empty body on success. Do not read or search for the event after a successful deletion, and do not let a stale post-delete read override the delete response. Report failure only for an explicit delete-tool error. If the call times out or its outcome is otherwise ambiguous, do not retry; say completion could not be confirmed.

## Feishu/Lark final result cards

In Relay mode, after a calendar create, modify, delete, or cancel operation has finished, send its
terminal success or execution-failure result through `record_result` and prefix its title with
`[calendar-result-card]` before a short result title. Put the complete user-facing result exactly
once in the `markdown` body; do not repeat it in the title. The relay strips this internal marker
and sends a message card containing the result. For successful create/modify, the exact
success line above belongs in the card body. Use `status="done"` for success and
`status="failed"` for an execution failure. Do not use cards for proposals, confirmations,
questions, progress, or blocked outcomes; those remain ordinary text or `ask_user` messages.
Ambiguous replies and other non-terminal interactions also remain ordinary text. Do not use the
title marker for non-calendar results. A reply quoting the
result card continues the same conversation; on continuation, use a card again only if that turn
itself ends with a calendar write success or execution failure, or a final acknowledgement that
the user clearly declined (including a standalone `不`) or cancelled a pending create, update, or delete proposal. For that acknowledgement,
use `status="done"` and clearly state that the proposal is cancelled and no calendar write will
occur; never call a write tool after the refusal. Keep the proposal, confirmation exchange, and
clarification before that final acknowledgement as ordinary text. An explicit failure of an actual
calendar cancellation remains a failure card. In ordinary ChatGPT conversations, keep the existing
text response behavior.

## Guardrails
Every historical modification, deletion/cancellation, new creation, recurrence mutation, and invitation response requires confirmation. Do not infer criticality, uncertainty, meeting type, timezone, attendees, reminders, conferencing, recurrence, visibility, deletion scope, or other field changes. For recurring updates or deletions, ask whether the change is one occurrence or the series. If a requested palette color is unavailable, explain the mismatch and ask before proceeding.

## Image-derived time exception

For an itinerary image sent with text, combine complementary facts from both and prefer explicit text fields over conflicting OCR when the intended booking remains clear. For a ride/transfer with a known pickup time and an estimated duration but no explicit end, infer the estimated drop-off/end time as pickup time plus duration, handle date rollover and timezone, and label it as estimated; do not ask for an end time in that case. An explicit end in the user's text or image wins over a duration calculation. This exception applies only to itinerary rides/transfers; an estimated end is approximate and follows the existing pink color rule. See `references/pic-to-event.md` for extraction details.
