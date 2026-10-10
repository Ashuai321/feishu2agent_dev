# Pic To Event

Turn itinerary images into accurate, user-confirmed Calendar events. Keep extraction separate from the external Calendar write.

## Extract
- Inspect every supplied image and preserve relationships between details on the same booking.
- Create one proposed event per independent itinerary item; combine images only when they clearly describe one item.
- Extract itinerary type, route, start/end date and time, origin/pickup, destination/drop-off, terminal/station, flight/train/booking number, provider, status, and useful notes.
- For rides and transfers, also extract direction, duration/distance, order number, vehicle plate/color/model/class, driver name/contact, and passenger/luggage capacity. Do not confuse pickup with airport drop-off.
- Ignore prices and app controls unless requested. Mark uncertain OCR and ask for verification. Booking status is not authorization to write.
- Combine images and accompanying text from the same user request as one evidence set when they describe the same booking. Merge complementary fields; do not ask the user to repeat details already visible in either source.
- Prefer explicit values written in the user's text over conflicting image/OCR text. Ask only if the conflict could change which booking or event is meant; otherwise retain the explicit text and note relevant image estimates.
- Require a date, year, and start time from the user's text or itinerary. Never invent those values or an unshown travel duration.
- For a ride or transfer, when the itinerary gives a scheduled pickup/boarding time and an estimated travel duration but no explicit end/drop-off time, calculate the estimated end as start time plus that duration. Apply date rollover and the itinerary's timezone. Label it as an estimated drop-off/end time in the proposal and description; it is not a guaranteed arrival time. Do not ask the user for an end time when this calculation is possible.
- An explicit end/drop-off time supplied in the user's text or image takes precedence over a duration-based calculation. If both are present, keep the explicit end and preserve the estimated duration as supporting itinerary information; ask only when the discrepancy materially makes the intended schedule unclear.
- If neither an explicit end/drop-off time nor a usable duration is present, ask for the missing end time or duration. Use only a duration clearly tied to that same ride segment, never a general app timer or unrelated leg.
- If timezone is absent, use the current Yuanbo Memory/default or the target calendar workflow's configured timezone and state it. Preserve endpoint local timezones when the itinerary explicitly uses different ones.

## Proposal and write
- Build a concise title from itinerary type and route/purpose; preserve service direction and identifiers.
- Use origin/pickup/venue as Location when appropriate; put destination/drop-off, status, and extracted details in description. Put vehicle/driver details under labels; never make a driver an attendee or use a phone number as Location.
- An end computed from an estimated travel duration is approximate and follows the existing pink color rule. An explicit complete time range follows the normal color rule.
- Show a numbered proposal with title, date/time/timezone, target calendar, Location, description, and vehicle/driver details. Label defaults.
- Do not write while extracting or proposing. A general request to add to Calendar is intent, not final confirmation.
- After the user confirms the complete proposal, pass only confirmed events to the calendar editing workflow and return direct event links. If the calendar editing workflow is unavailable, report the dependency and do not bypass it.
