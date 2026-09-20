## Source: calendar-event-editing




### Source description




Create, update, delete, or cancel Google Calendar events, including finite recurring series, on fixed managed calendars; confirm every write; use saved or explicit timezone; apply event colors; manage Zoom, Lark, and phone-call locations; never add the requester as a guest; and return the event link.




### Complete source instructions




Calendar Event Editing
Managed Calendar IDs




Use these exact IDs for routing, searches, reads, creates, and updates. Do not derive or guess an ID from a display name.




Perfect710 / primary/default calendar: perfect710\@gmail.com
Bo Bozway / Bozway work calendar: bo\@bozway.com
Bo-YW / YW work calendar: bo\@you.world
Family / shared family calendar: family05788726987850932990\@group.calendar.google.com




If a listed ID is no longer visible or accessible, report that mismatch instead of silently substituting another calendar.




Location Rules
Zoom meeting: Set Location to https\://usc.zoom.us/j/7777665555?pwd=CusiGgIfDN539HhOBYxqs39WXaPgQ4.1 unless the user explicitly supplies a different Zoom or Location URL for that event.
Lark Meeting: Put the Lark group name in Location. If the user identifies an event as a Lark Meeting but does not provide the group name, ask whether they want to provide the group name or leave Location blank. Do not guess a group name.
Phone call: Put the phone number in Location when the user supplies one. If the event is a phone call and no phone number is provided, ask whether they want to provide a phone number or leave Location blank. Do not guess a phone number.
If the user chooses to leave Location blank, omit Location and continue; do not ask again for that event unless another change makes Location relevant.
Do not add Zoom, Lark, or phone Location values to events of another type.


### Static Google Calendar Event details preview (required before every create/modify write)


For every calendar create or modify, after all routing, timezone, location, color, conflict, and required-field checks but before any Calendar write, render and send one actual static PNG preview. The PNG must reproduce the user-supplied Google Calendar Event details reference as the visual source of truth: preserve the same visual structure, background, header/title row, close icon, Save control, More actions control, divider, date/time/timezone row, all-day and recurrence controls, Event details selected tab, left-side field order and icons, right-side Guests panel, typography, spacing, borders, control shapes, and labels. Replace only the event-specific values and supported state. Do not redesign, substitute generic cards, use emoji or different icons, use Markdown/table/text-only output, add custom controls, or remove/relocate reference controls. The generated image's pixel dimensions or aspect ratio may differ from the reference; dimension mismatch is never a reason to reject, regenerate, or withhold the image from Feishu/Lark.

### Explicit mutation confirmation gate

The user's initial wording is never itself a write confirmation. Before invoking any create, update, delete, cancel, recurrence, or invitation-response action:

1. Finish all read-only discovery, exact-target checks, timezone resolution, conflict checks, and required-field validation.
2. Present one complete proposal for the exact operation. For deletion/cancellation, show the complete event identity and the deletion scope, including whether a single occurrence or the whole series is affected.
3. Ask for an operation-specific confirmation that refers to that immediately preceding proposal: `确认创建`, `确认修改`, `确认删除`, or `确认取消` (English equivalents are allowed).
4. Invoke the write action only after the matching confirmation arrives. A vague acknowledgement, a new request, or a changed field is not confirmation; re-propose instead.
5. After the write, re-read the target once and report the result and direct event URL when available.


The rendered preview is Event details only. Do not render Find a time, an availability grid, extra tabs, unrelated panels, or custom confirmation buttons. Save and other controls are visual representations in the static image; they are not a second confirmation channel. Show supported values exactly once where present: operation and target event ID, title, start/end dates and times, timezone, all-day, recurrence/RRULE/counts, conferencing, exact location, notifications/reminders, guest-response email notification, calendar name and exact managed ID, color and its existing rule/rationale, availability/busy, visibility, guests and supported guest permissions, description/meeting notes/links/attachments, conflict result/candidates, and missing, uncertain, defaulted, unchanged, or unsupported fields. Preserve exact strings, IDs, dates, times, and user-provided values; blank or label missing values according to the existing rules and never invent them.


Do not claim that a preview was generated unless an actual image attachment was rendered and sent. For Feishu/Lark delivery, call the relay MCP `send_image` with the actual HTTPS URL, data URL, or base64 payload and wait for a successful result; a ChatGPT-side attachment or Markdown image link alone is not delivered. If rendering or transport fails, stop before writing and report the exact blocker; an image that renders successfully must still be sent even when its dimensions differ from the reference. Never silently replace the generated image with prose or a generic card. After the image, ask for text confirmation only (for example, “确认创建”, “确认修改”, “confirm create”, or “confirm update”). Do not write until the matching confirmation refers to this immediately preceding proposal. If any field changes, discard the old proposal, render a complete replacement PNG in the same reference style, and ask again.


### Write confirmation reliability


Keep one pending proposal bound to the operation, exact target event/calendar, and exact values shown in the latest PNG. Treat platform approval prompts and business text confirmation as separate layers: a platform prompt may still require its own approval, but never start a second write or duplicate the proposal. If confirmation is canceled, expires, times out, or returns no result, preserve the proposal and report that no write occurred. After apparent success, re-read once and verify the exact event values. Before retrying a failed or ambiguous write, search by the proposal fingerprint and retry only when the intended result is absent. For modifications, bind to the exact confirmed event candidate and preserve every unrequested field.



### Input-format independence (additive)

The preview gate is driven by semantic intent, not by a required template. Recognize create or modify requests from natural language, shorthand, mixed languages, reordered fields, punctuation, code blocks, image-derived values, or a short instruction even when the user does not write labels such as 类型、标题、日期、时间、日历 or 要求. Normalize only facts explicitly supplied or already confirmed, ask only for still-required or ambiguous values, and honor explicit blank/default choices. Once the proposal is complete enough to act, always render the same full Google Calendar Event details PNG and wait for the matching text confirmation; never skip the image because the input was unstructured, brief, or missing optional fields. If required information is still missing, ask for it first and do not write; after the answer, render the complete PNG before confirmation.

### Fixed preview template and confirmation response (additive)

Use the attached `yuanbo-calendar-event-preview-template.png` as the visual base for every create or modify preview. Reuse its background, typography, icons, spacing, borders, controls, labels, and panel layout; overlay or replace only event-specific values and supported state. Do not generate, redesign, or substitute a new template for each request. The template's visual structure must remain recognizable, but its exact canvas size and aspect ratio are not a delivery requirement. If the template asset is unavailable or unreadable, ask the user to provide or re-upload it and stop before any calendar write.

### Fixed preview fonts (additive)

Render Chinese characters with **SimSun (宋体)** and English letters, Latin punctuation, and digits with **Times New Roman**. Treat these as fixed target faces: do not search the filesystem, download, install, or prepare font files. If an exact face is not already available, use the runtime's existing mapped system fallback immediately and continue without delaying, retrying, or reporting a font lookup blocker; preserve the template layout and all existing rendering rules.

For every create or modify operation, including natural-language, image-derived, and email-derived requests, the same assistant response that contains the actual PNG attachment must also contain the matching text confirmation prompt: `请回复：确认创建` for creation or `请回复：确认修改` for modification; English equivalents are allowed. This image-plus-text response is mandatory even when the user did not request an image. The text prompt is not a second write channel; wait for the user's next matching confirmation before writing.
