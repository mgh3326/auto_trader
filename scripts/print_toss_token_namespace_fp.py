"""Print only the non-secret Toss token namespace fingerprint."""

import os

from app.services.brokers.token_keys import toss_client_fingerprint


def main() -> None:
    client_id = os.environ.get("TOSS_API_CLIENT_ID")
    if client_id is None or not client_id.strip():
        raise SystemExit("TOSS_API_CLIENT_ID is required")
    print(toss_client_fingerprint(client_id))


if __name__ == "__main__":
    main()
