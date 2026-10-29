"""One-time: create the YouTube OAuth refresh token for GitHub Actions.

Run this on YOUR computer (it opens a browser to sign in):

    pip install google-auth-oauthlib
    python scripts/get_refresh_token.py path/to/client_secret.json

Sign in with the Google account that owns the channel, and pick the channel
(brand account) when asked. Copy the three printed values into GitHub secrets.

Your OAuth consent screen must be set to "In production" (it can stay
unverified). In "Testing" mode Google expires refresh tokens after 7 days and
the pipeline silently stops uploading.
"""
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/get_refresh_token.py client_secret.json")
    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    info = json.load(open(sys.argv[1]))
    client = info.get("installed") or info.get("web")
    print("\nAdd these as GitHub Actions secrets:\n")
    print(f"YT_CLIENT_ID      = {client['client_id']}")
    print(f"YT_CLIENT_SECRET  = {client['client_secret']}")
    print(f"YT_REFRESH_TOKEN  = {creds.refresh_token}")


if __name__ == "__main__":
    main()
