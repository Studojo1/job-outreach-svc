/**
 * Format a UTC timestamp string for display in the user's timezone.
 *
 * Backend returns naive UTC datetimes (no Z suffix) like "2026-03-14T10:30:00".
 * We append "Z" so JS Date treats them as UTC before converting.
 *
 * Output format: "14 Mar • 10:19 (IST)"
 */
export function formatTimestamp(
  isoString: string | null | undefined,
  timezone: string,
): string {
  if (!isoString) return '';

  // Ensure the string is treated as UTC
  const utcString = isoString.endsWith('Z') || isoString.includes('+')
    ? isoString
    : isoString + 'Z';

  const date = new Date(utcString);
  if (isNaN(date.getTime())) return isoString;

  const day = date.toLocaleString('en-GB', { timeZone: timezone, day: 'numeric' });
  const month = date.toLocaleString('en-GB', { timeZone: timezone, month: 'short' });
  const time = date.toLocaleString('en-GB', {
    timeZone: timezone,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });

  // Get timezone abbreviation: "IST", "EST", etc.
  const tzAbbr = new Intl.DateTimeFormat('en-GB', {
    timeZone: timezone,
    timeZoneName: 'short',
  })
    .formatToParts(date)
    .find((p) => p.type === 'timeZoneName')?.value || '';

  return `${day} ${month} \u2022 ${time} (${tzAbbr})`;
}
