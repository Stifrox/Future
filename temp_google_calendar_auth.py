import time
import tools.integrations as integrations
import requests
from requests.exceptions import HTTPError

print('Starting Google Calendar auth...')
print(integrations.request_google_calendar_authorization('Authorize Future to access your Google Calendar.'))
start = time.time()
while time.time() - start < 180:
    if integrations.GOOGLE_CALENDAR_ACCESS_TOKEN:
        print('Access token received')
        break
    time.sleep(1)
else:
    print('Timed out waiting for callback. Token not received.')
    raise SystemExit(1)

print('ACCESS_TOKEN_AVAILABLE:', bool(integrations.GOOGLE_CALENDAR_ACCESS_TOKEN))
print('REFRESH_TOKEN_AVAILABLE:', bool(integrations.GOOGLE_CALENDAR_REFRESH_TOKEN))

payload = {
    'summary': 'Future test event',
    'description': 'Created by automation',
    'start': {'dateTime': '2026-07-02T15:00:00-05:00'},
    'end': {'dateTime': '2026-07-02T16:00:00-05:00'},
}
headers = {'Authorization': f'Bearer {integrations.GOOGLE_CALENDAR_ACCESS_TOKEN}'}
try:
    r = requests.post('https://www.googleapis.com/calendar/v3/calendars/primary/events', headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    print('Created event:', r.json().get('id'))
except HTTPError as e:
    print('Event creation failed:', e)
    print('Status code:', r.status_code)
    print('Response:', r.text)
