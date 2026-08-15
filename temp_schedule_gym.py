import time
import tools.integrations as integrations

print('Starting Google Calendar authorization if needed...')
reply = integrations.request_google_calendar_authorization('Authorize Future to access your Google Calendar.')
print(reply)

start = time.time()
while time.time() - start < 180:
    if integrations.GOOGLE_CALENDAR_ACCESS_TOKEN:
        print('Access token received')
        break
    time.sleep(1)
else:
    print('Timed out waiting for the callback. Please complete authorization in the browser.')
    raise SystemExit(1)

print('Access token available:', bool(integrations.GOOGLE_CALENDAR_ACCESS_TOKEN))
print('Refresh token available:', bool(integrations.GOOGLE_CALENDAR_REFRESH_TOKEN))

command = 'schedule gym at 7pm today'
print('Sending calendar command:', command)
reply = integrations.handle_calendar_command(command)
print('Reply:', reply)
print('Token file persisted:', integrations.GOOGLE_CALENDAR_TOKEN_FILE)
