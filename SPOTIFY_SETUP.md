Spotify API setup
==================

1. Create a Spotify developer app at https://developer.spotify.com/dashboard
2. Add a redirect URI such as http://localhost:8888/callback
3. Set these environment variables before starting Future:

   set SPOTIFY_CLIENT_ID=your_client_id
   set SPOTIFY_CLIENT_SECRET=your_client_secret
   set SPOTIFY_REDIRECT_URI=http://localhost:8888/callback

4. Start Future and open the auth URL returned by the integration helper.
5. Paste the callback code into the exchange flow when you are ready to enable full API control.
