"""One-off Gmail OAuth 2.0 authorisation (``uv run task auth``).

Runs Google's installed-app flow for the ``gmail.send`` scope only and writes
the resulting token (including the refresh token) to ``GMAIL_TOKEN_FILE``
with owner-only permissions. Run it on a machine with a browser, then provide
the token file to the headless deployment (see README, "Gmail OAuth setup").
"""

from __future__ import annotations

import argparse
import sys

from email_service.config import Settings
from email_service.providers.gmail import SCOPES, save_token_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the authorisation URL instead of opening a browser",
    )
    parser.add_argument("--port", type=int, default=0, help="local redirect port (0 = random)")
    args = parser.parse_args(argv)

    settings = Settings()
    if not settings.gmail_credentials_file.is_file():
        print(
            f"OAuth client file not found: {settings.gmail_credentials_file}\n"
            "Download it from Google Cloud Console (APIs & Services > Credentials > "
            "OAuth client ID, type 'Desktop app') and save it there.",
            file=sys.stderr,
        )
        return 2

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(settings.gmail_credentials_file), SCOPES)
    credentials = flow.run_local_server(
        port=args.port, open_browser=not args.no_browser, access_type="offline", prompt="consent"
    )
    save_token_file(settings.gmail_token_file, credentials.to_json())
    print(f"Gmail token saved to {settings.gmail_token_file} (scope: gmail.send only).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
