#!/usr/bin/env python
"""
Standalone Spotify OAuth callback server.
Runs persistently on localhost:8888, listening for OAuth redirects.
"""
import sys
import time
from tools.integrations import run_spotify_callback_server, SPOTIFY_CALLBACK_SERVER

if __name__ == "__main__":
    try:
        print("Starting Spotify callback server on localhost:8888...")
        server = run_spotify_callback_server()
        print("✓ Spotify callback server is running and listening on http://localhost:8888")
        print("Waiting for OAuth callback... (Press Ctrl+C to stop)")
        
        # Keep the server running
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down callback server...")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
