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

Use yuanbo-calendar-onboarding only when its existing trigger requires missing durable defaults; do not restate or replace that Skill. Existing Yuanbo Memory, the connected Google Calendar account, and the target agent's original instructions remain authoritative for calendar identity, timezone, defaults, and persistence. Do not import another agent's account names, calendar IDs, mailbox, timezone, or defaults. If a required app or dependency is not attached, explain the exact gap and stop rather than substituting another account or writing directly.

Shared safeguards:
- Keep discovery and proposals read-only.
- Ask only for missing or ambiguous fields; never invent event details.
- Before every calendar write or invitation response, identify and read the exact target, check relevant conflicts, show the complete change, and get explicit confirmation.
- Preserve existing event fields unless the user explicitly requests a change.
- Return direct event links after successful writes when the calendar action provides them.
- Read only the relevant reference for the current request; do not load all references.
