## Source: calendar-event-editing




### Source description




Create, update, delete, or cancel Google Calendar events, including finite recurring series, on fixed managed calendars; confirm every write; use saved or explicit timezone; apply event colors; manage Zoom, Lark, and phone-call locations; never add the requester as a guest; and return the event link.




### Complete source instructions




Calendar Event Editing
Managed Calendar IDs




Use these exact IDs for routing, searches, reads, creates, and updates. Do not derive or guess an ID from a display name.

### Persistent per-user memory in Relay mode

Follow the Agent Instructions' persistent-memory workflow. Before using this reference's static routing or color rules in Relay mode, apply the newest verified memory for the current sender; it must affect future searches, proposals, and writes. The current explicit request and shared safety/confirmation requirements take precedence. If memory is unavailable, pause a write whose target depends on saved preferences.

### Authoritative calendar routing

Apply this routing before every calendar search, read, create, update, delete, cancel, recurrence, or invitation-response action. Never fall back to the Google Calendar connector's default calendar.

- Explicitly named `Perfect710` uses `perfect710\@gmail.com`, unless the current sender has a newer verified memory explicitly revising this routing.
- A family cue that mentions 小奇, 小果, 果果, or 诺诺 uses `family05788726987850932990\@group.calendar.google.com` (`Family`).
- A work-related event must ask the user to choose one of `Bo Bozway` (`bo\@bozway.com`) or `Bo YW` (`bo\@you.world`) before preparing or executing a write. Do not choose between those two silently unless the current sender has a newer verified memory explicitly revising this rule.
- An explicit `Bo Bozway`, `Bo YW`, or `Family` calendar name is honored.
- If no calendar is named, the event nature is not family or work-related, or the nature cannot be determined, use `Bo Bozway` (`bo\@bozway.com`) by default unless the current sender has a newer verified memory explicitly revising this default.
- Family cues take precedence over a generic work/default classification. An explicitly named calendar takes precedence over inferred nature, except that `Perfect710` still requires the explicit name unless the current sender has a newer verified memory explicitly revising that routing.

The calendar name and exact ID selected by this table must be shown in every text proposal.

### Authoritative event color routing

Apply this rule to every create and modify text proposal and to the actual calendar write; the proposal must show the same color.

1. If the start or end time is missing, approximate, unresolved, date-only, or otherwise uncertain, use **pink**. This rule has highest priority and overrides online/offline and location cues.
2. If the time is definite and the user explicitly says online/线上, use **green**. This takes precedence over the location field.
3. If neither online/offline is specified and no actual location is supplied, use **green**. Blank, “not provided,” and “leave blank” are not actual locations.
4. If the time is definite and the user explicitly says offline/线下 or supplies an actual location, use **purple**, unless the user explicitly said online/线上 (which follows rule 2).
5. If the user explicitly specifies both online and offline and the intended mode is unclear, ask for clarification instead of guessing the color.
6. Resolve the live Google Calendar palette to the named pink, green, or purple color before writing; never use the connector's default color and never silently substitute blue or another color. If the named color is unavailable, report that exact mismatch and stop before the write.

The color decision must be made from the original user-provided facts; do not infer an online/offline mode from unrelated text. If a field changes, recompute the color and re-render the proposal.




If a listed ID is no longer visible or accessible, report that mismatch instead of silently substituting another calendar.




Location Rules
Zoom meeting: Set Location to https\://usc.zoom.us/j/7777665555?pwd=CusiGgIfDN539HhOBYxqs39WXaPgQ4.1 unless the user explicitly supplies a different Zoom or Location URL for that event.
Lark Meeting: Put the Lark group name in Location. If the user identifies an event as a Lark Meeting but does not provide the group name, ask whether they want to provide the group name or leave Location blank. Do not guess a group name.
Phone call: Put the phone number in Location when the user supplies one. If the event is a phone call and no phone number is provided, ask whether they want to provide a phone number or leave Location blank. Do not guess a phone number.
If the user chooses to leave Location blank, omit Location and continue; do not ask again for that event unless another change makes Location relevant.
Do not add Zoom, Lark, or phone Location values to events of another type.


### Create/modify text proposal (no generated preview)

For every calendar create or modify, after routing, timezone, location, color, conflict, and required-field checks but before any Calendar write, present one complete text proposal. Do not render, generate, attach, upload, or send a PNG preview, and do not call the relay MCP `send_image` solely for this calendar proposal. Show the supported values exactly once where present: operation and target event ID, title, start/end dates and times, timezone, all-day, recurrence/RRULE/counts, conferencing, exact location, notifications/reminders, guest-response email notification, calendar name and exact managed ID, color and its rule/rationale, availability/busy, visibility, guests and supported guest permissions, description/meeting notes/links/attachments, conflict result/candidates, and missing, uncertain, defaulted, unchanged, or unsupported fields. Preserve exact strings, IDs, dates, times, and user-provided values; blank or label missing values according to the existing rules and never invent them.

### Explicit mutation confirmation gate

The user's initial wording is never itself a write confirmation. Before invoking any create, update, delete, cancel, recurrence, or invitation-response action:

1. Finish all read-only discovery, exact-target checks, timezone resolution, conflict checks, and required-field validation.
2. Present one complete text proposal for the exact operation. For deletion/cancellation, show the complete event identity and the deletion scope, including whether a single occurrence or the whole series is affected.
3. Ask for a confirmation that refers to that immediately preceding proposal. The operation-specific forms `确认创建`, `确认修改`, `确认删除`, or `确认取消` remain supported; the positive/negative response classification below also applies.
4. Invoke the write action only after a positive confirmation classified below arrives. A new request or changed field is not confirmation; re-propose instead. A response classified as refusal must never call the write action.
5. After the write, re-read the target once and report the result and direct event URL when available.

### Confirmation reply classification

After a complete text proposal is pending, normalize surrounding whitespace, case, and sentence-ending punctuation. `执行`, `开始`, `接受`, `确认`, `行`, `行的`, `好`, `好的`, `可以`, `yes`, `ok`, `同意`, `确认创建`, `确认修改`, `确认删除`, and `确认取消` confirm the immediately preceding proposal. A direct phrase such as `好的，执行` also confirms it. Explicit refusal has priority over any positive token: `不执行`, `不开始`, `不接受`, `不确认`, `不行`, `不行的`, `不好`, `不好的`, `不可以`, `no`, and `不确认创建/修改/删除/取消` reject the proposal and must not call a write tool, even if the same reply also contains a positive token. A reply with changed fields, no pending proposal, or neither clear polarity is not confirmation and requires a new question or proposal.

### Write confirmation reliability


Keep one pending proposal bound to the operation, exact target event/calendar, and exact values shown in the latest text proposal. Treat platform approval prompts and business text confirmation as separate layers: a platform prompt may still require its own approval, but never start a second write or duplicate the proposal. If confirmation is canceled, expires, times out, or returns no result, preserve the proposal and report that no write occurred. After apparent success, re-read once and verify the exact event values. Before retrying a failed or ambiguous write, search by the proposal fingerprint and retry only when the intended result is absent. For modifications, bind to the exact confirmed event candidate and preserve every unrequested field.



### Input-format independence (additive)

Recognize create or modify requests from natural language, shorthand, mixed languages, reordered fields, punctuation, code blocks, image-derived values, or a short instruction even when the user does not write labels such as 类型、标题、日期、时间、日历 or 要求. Normalize only facts explicitly supplied or already confirmed, ask only for still-required or ambiguous values, and honor explicit blank/default choices. Once the proposal is complete enough to act, present the complete text proposal and wait for the matching confirmation; never generate or send a calendar preview image. If required information is still missing, ask for it first and do not write; after the answer, present the updated text proposal before confirmation.

For an itinerary image sent with text, combine complementary facts from both and prefer explicit text fields over conflicting OCR when the intended booking remains clear. For a ride/transfer with a known pickup time and an estimated duration but no explicit end, infer the estimated drop-off/end time as pickup time plus duration, handle date rollover and timezone, and label it as estimated; do not ask for an end time in that case. An explicit end in the user's text or image wins over a duration calculation. This exception applies only to itinerary rides/transfers; an estimated end is approximate and follows the existing pink color rule. See `references/pic-to-event.md` for extraction details.

### Successful create/modify response

After a successful create, re-read the target and reply exactly: `创建成功，日程链接：<direct event URL>`.
After a successful modify, re-read the target and reply exactly: `修改成功，日程链接：<direct event URL>`.
The URL must come from the write result or successful re-read; never invent one. If no direct URL is returned, state that it is unavailable instead of substituting an ID. This no-preview rule applies only to generated calendar previews; user-provided itinerary images remain governed by the dedicated image workflow.
