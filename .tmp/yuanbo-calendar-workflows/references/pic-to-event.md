# Pic To Event

Turn itinerary images into accurate, user-confirmed Calendar events. Keep extraction separate from the external Calendar write.

## Extract
- Inspect every supplied image and preserve relationships between details on the same booking.
- Create one proposed event per independent itinerary item; combine images only when they clearly describe one item.
- Extract itinerary type, route, start/end date and time, origin/pickup, destination/drop-off, terminal/station, flight/train/booking number, provider, status, and useful notes.
- For rides and transfers, also extract direction, duration/distance, order number, vehicle plate/color/model/class, driver name/contact, and passenger/luggage capacity. Do not confuse pickup with airport drop-off.
- Ignore prices and app controls unless requested. Mark uncertain OCR and ask for verification. Booking status is not authorization to write.
- Require date, year, start time, and end time or duration. Ask for all missing required fields together; never silently choose a year or duration.
- If timezone is absent, use the current Yuanbo Memory/default or the target calendar workflow's configured timezone and state it. Preserve endpoint local timezones when the itinerary explicitly uses different ones.

## Proposal and write
- Build a concise title from itinerary type and route/purpose; preserve service direction and identifiers.
- Use origin/pickup/venue as Location when appropriate; put destination/drop-off, status, and extracted details in description. Put vehicle/driver details under labels; never make a driver an attendee or use a phone number as Location.
- Show a numbered proposal with title, date/time/timezone, target calendar, Location, description, and vehicle/driver details. Label defaults.
- Do not write while extracting or proposing. A general request to add to Calendar is intent, not final confirmation.
- After the user confirms the complete proposal, pass only confirmed events to the calendar editing workflow and return direct event links. If the calendar editing workflow is unavailable, report the dependency and do not bypass it.
