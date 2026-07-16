"""
Copy this file to secrets.py (same directory) and fill in the real key.

secrets.py is gitignored; config.py imports HIGHSCORE_API_KEY from it and
falls back to "" (submission then fails politely and scores stay queued).
The key is jacket-server's [server] api_key — the same one the jacket
client polls with.
"""

HIGHSCORE_API_KEY = "YOUR_API_KEY"
