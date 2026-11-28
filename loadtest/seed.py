"""Create funded accounts for the load tests through the real API.

    uv run --project ../services/payments-api python seed.py --api-key <key> --accounts 10000

Writes accounts.json (ignored by Git): `accounts` for random transfers, and `hot` accounts with
large balances for the contention scenario. Every balance comes from a deposit, so the ledger
invariant holds for seeded data too.
"""

import argparse
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

BALANCE_MINOR = 1_000_000  # $10,000 per account: enough that random transfers never run dry
HOT_BALANCE_MINOR = 1_000_000_000


def create_funded_account(client: httpx.Client, index: int, balance: int) -> str:
    response = client.post(
        "/v1/accounts", json={"owner_name": f"loadtest-{index}", "currency": "USD"}
    )
    response.raise_for_status()
    account_id = response.json()["id"]
    client.post(
        "/v1/deposits",
        headers={"Idempotency-Key": f"seed-{uuid.uuid4()}"},
        json={"account_id": account_id, "amount_minor": balance, "currency": "USD"},
    ).raise_for_status()
    return account_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed accounts for k6.")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--accounts", type=int, default=10_000)
    parser.add_argument("--hot", type=int, default=5)
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("accounts.json"))
    args = parser.parse_args()

    with (
        httpx.Client(base_url=args.base_url, headers={"X-API-Key": args.api_key}) as client,
        ThreadPoolExecutor(max_workers=16) as pool,
    ):
        accounts = list(
            pool.map(
                lambda i: create_funded_account(client, i, BALANCE_MINOR), range(args.accounts)
            )
        )
        hot = list(
            pool.map(
                lambda i: create_funded_account(client, -i - 1, HOT_BALANCE_MINOR),
                range(args.hot),
            )
        )
    args.out.write_text(json.dumps({"accounts": accounts, "hot": hot}))
    print(f"Seeded {len(accounts)} accounts and {len(hot)} hot accounts into {args.out}")


if __name__ == "__main__":
    main()
