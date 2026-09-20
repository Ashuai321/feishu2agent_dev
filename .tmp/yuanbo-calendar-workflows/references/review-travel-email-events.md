# Review Travel Email Events

Use only for an explicit request to scan travel email or reservations. Review first and write only after numbered user decisions. A Gmail connector and the calendar editing workflow must be available; if either is missing, explain the exact gap and stop.

## Review email
- Use the explicitly selected Gmail account and bounded lookback window; do not substitute another mailbox.
- Retrieve received messages across ordinary Gmail categories, excluding spam/trash, using actual received timestamps. Do not rely on subject keywords alone.
- Treat a message as travel-related only when it establishes a concrete reservation, itinerary segment, check-in/out, departure/arrival, pickup/drop-off, booked activity, cancellation, or material schedule change.
- Exclude promotions, ideas, fare alerts, and generic account notices. When confidence is low, list the item as uncertain.
- Normalize distinct candidates: one per timed flight/rail segment; one per continuous hotel stay or vehicle rental; merge duplicate confirmations; flag cancellations and conflicting schedule changes without deleting or rewriting events.

## Compare
- After finding a candidate, load the calendar editing workflow and search all relevant Yuanbo calendars in a bounded window.
- Compare local date/time/timezone, flight/train number, provider/property/activity, route/venue, reservation ID, title, duration, and Location.
- Classify each as Exact match, Likely match, Conflict, or No match. Identify the calendar and discrepancies; do not treat same-city/day as a match without stronger evidence.

## Numbered review
- Make no calendar changes during discovery.
- Present one numbered row per candidate with item/title, local schedule/timezone/route, concise email evidence, calendar match details, and exactly one proposed write: Create new event, Modify existing event, or No write.
- State what a confirmation decision would do, end with "No calendar changes made.", and ask for numbered actions.
- If required fields remain ambiguous, ask a concise question for that item and do not mark it ready.

## Apply decisions
- Apply only explicit numbered decisions; do not batch unmentioned candidates or modify events on other accounts.
- New/pending: create on the target personal calendar with its normal/default color.
- Confirm: ensure the approved event is on the target personal calendar and apply the target workflow's confirmed color if configured; if a match exists, change only disclosed fields.
- Ignore: recolor an exact existing target-calendar match only when the target workflow defines an ignore color; never create an event merely to record ignore.
- No decision: no write.
- Resolve the live palette before each color operation. If a required named color is unavailable, report it and ask before changing color.
- Return each result against its original number with the direct calendar link.

## Invariants
Never auto-create, auto-modify, auto-delete, or auto-recolor during review. Never use a similar-looking account/calendar. Preserve each numbered choice independently.
