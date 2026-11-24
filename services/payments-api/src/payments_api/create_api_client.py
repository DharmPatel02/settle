import argparse
import secrets

from payments_api.db import get_sessionmaker
from payments_api.models import ApiClient
from payments_api.security import hash_api_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a Settle API client.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--admin", action="store_true")
    args = parser.parse_args()

    plaintext_key = secrets.token_urlsafe(32)
    with get_sessionmaker().begin() as session:
        client = ApiClient(
            name=args.name,
            key_hash=hash_api_key(plaintext_key),
            is_admin=args.admin,
        )
        session.add(client)
        session.flush()

    print(f"Client ID: {client.id}")
    print(f"API key: {plaintext_key}")
    print("Store this key now; only its SHA-256 hash is saved.")


if __name__ == "__main__":
    main()
