---
name: yuanbo-calendar-workflows
description: Route Yuanbo calendar requests to the smallest relevant workflow for itinerary images, travel-email review, event editing, or Skill maintenance without duplicating onboarding or calendar defaults.
---

# Yuanbo Calendar Workflows

Use this Skill only when the request is an itinerary image, a travel-email/reservation review, a calendar read/write, or Skill creation/update/validation/packaging.

Load only the one relevant reference:
- Itinerary image or photo: read references/pic-to-event.md.
- Travel email or reservation scan: read references/review-travel-email-events.md.
- Calendar search, availability, create, update, cancel, recurrence, color, or invitation response: read references/calendar-event-editing.md.
- Skill creation, update, validation, improvement, or packaging: read references/skill-creator.md.

For a ride booking sent with both an image and text, combine the facts from both sources using
`references/pic-to-event.md`; do not ask again for facts already present in either source. Use an
explicit end time from the user's text when supplied. If the itinerary has a pickup time and an
estimated duration but no end time, infer and label the estimated drop-off time as pickup time plus
duration. This is scoped to itinerary rides and transfers; do not infer times for unrelated events.

For every calendar request, `references/calendar-event-editing.md` is authoritative for calendar routing
and event colors. Never use the connector's default calendar. An explicitly named `Perfect710` is the
only allowed use of that calendar unless a newer verified memory for this sender explicitly changes
that routing; family cues 小奇/小果/诺诺 route to `Family`; work-related requests must ask the user to
choose `Bo Bozway` or `Bo YW` unless a newer verified memory for this sender explicitly changes that
rule; all other or unclear requests default to `Bo Bozway` unless that sender's newer memory changes it.
For create/modify colors, uncertain time is pink with highest priority. With definite time, explicit online
is green, even if a location is present. If neither online/offline is specified and no actual location is
provided (including blank, “not provided,” or “leave blank”), use green. Explicit offline or an actual
location is purple unless explicit online was specified. If online and offline are both explicit and the
intended mode is unclear, ask the user. The text proposal and write must use the same resolved live-palette
color.

## Persistent user memory in Relay mode

Follow the persistent-memory workflow in the Agent Instructions. For calendar routing and colors,
apply the newest verified memory for this sender before using the static rules below as defaults; the
current explicit request and shared safety/confirmation safeguards still take precedence. If memory is
unavailable, pause a calendar write whose target depends on saved preferences.

For create, update, delete, or cancel proposals, classify the direct confirmation reply as follows: after
normalizing surrounding whitespace, case, and punctuation, `执行`, `开始`, `接受`, `确认`, `行`, `行的`, `好`, `好的`,
`可以`, `yes`, `ok`, `同意`, `确认创建`, `确认修改`, `确认删除`, or `确认取消` (including a direct phrase such as
`好的，执行`) confirms the immediately preceding proposal. Explicit refusal wins over any positive token:
`不执行`, `不开始`, `不接受`, `不确认`, `不行`, `不行的`, `不好`, `不好的`, `不可以`, `no`, and the corresponding
`不确认创建/修改/删除/取消` forms reject it and must not call a write tool. A reply with changed fields,
no pending proposal, or neither clear polarity is not confirmation and requires a new question or proposal.

Use yuanbo-calendar-onboarding only when its existing trigger requires missing durable defaults; do not restate or replace that Skill. In Feishu/Lark Relay mode, the current sender's verified persistent memory is authoritative for updates to that user's rules, aliases, and defaults. Existing Yuanbo Memory and the connected Google Calendar remain authoritative for account identity, timezone, and details not explicitly changed by the sender's verified memory. Do not import another agent's account names, calendar IDs, mailbox, timezone, or defaults. If a required app or dependency is not attached, explain the exact gap and stop rather than substituting another account or writing directly.

Shared safeguards:
- Keep discovery and proposals read-only.
- Ask only for missing or ambiguous fields; never invent event details.
- Before every calendar mutation or invitation response (create, update, delete/cancel, recurrence change, or guest response), identify and read the exact target, check relevant conflicts, show the complete change or deletion scope, and get explicit confirmation.
- Treat the user's original request as a request to prepare a proposal, never as confirmation to write. Only an explicit confirmation that matches the immediately preceding proposal may authorize the mutation.
- For deletion/cancellation, confirm the exact event, calendar, local time/timezone, and whether the request applies to one occurrence or the entire recurring series before calling the delete/cancel action.
- For a confirmed Google Calendar deletion, treat a delete-tool call that completes without an error as success even if its response body is empty. Do not read or search for the event after successful deletion; only an explicit delete-tool error is a failure. If the call times out or the result is ambiguous, do not retry and say completion could not be confirmed.
- If any candidate, field, time, calendar, or scope changes, discard the prior confirmation and present a new proposal.
- Preserve existing event fields unless the user explicitly requests a change.
- Do not generate, attach, upload, or send a PNG preview for ordinary create or modify requests; do not call `send_image` solely for a calendar proposal.
- After a successful create or modify, re-read the target and reply exactly with `创建成功，日程链接：<direct event URL>` or `修改成功，日程链接：<direct event URL>`. Use only a URL returned by the write or re-read result; never invent one.
- Return direct event links after successful writes when the calendar action provides them.
- Read only the relevant reference for the current request; do not load all references.

Relay result cards:
- For a Feishu/Lark calendar create, update, delete, or cancel operation, use
  `record_result` only when the operation reaches a terminal success or execution failure, and
  prefix its title with `[calendar-result-card]` before a short result title. Put the complete
  user-facing result exactly once in the `markdown` body; do not repeat it in the title. The
  relay strips this internal marker.
- Keep proposals, confirmation requests, missing-information questions, progress, blocked
  outcomes, and non-calendar results as ordinary text. `ask_user` questions remain ordinary
  messages and must not be converted to cards.
- When the user clearly declines (including a standalone `不`) or cancels a pending create/update/delete proposal, send the final
  acknowledgement that the proposal is cancelled and no calendar write will occur as a result
  card with `status="done"`. Never call the calendar write tool after that refusal. Keep the
  proposal and confirmation exchange, and any clarification before the final acknowledgement,
  as ordinary text. An explicit failure of an actual calendar cancellation remains a failure card.
- A quoted reply to the result card continues the same relay conversation. On that later turn,
  use a result card only if it again ends with a calendar write success or execution failure, or
  a final acknowledgement that a pending create/update/delete proposal was cancelled.
