"""List all Polymarket US deposits and withdrawals."""

import os
from decimal import Decimal

from dotenv import load_dotenv
from polymarket_us import PolymarketUS

load_dotenv()

CASH_TYPES = [
    "ACTIVITY_TYPE_ACCOUNT_DEPOSIT",
    "ACTIVITY_TYPE_ACCOUNT_ADVANCED_DEPOSIT",
    "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL",
    "ACTIVITY_TYPE_TRANSFER",
]


def fetch_transfers(client):
    """Page through the full activity history, cash movements only."""
    cursor, out = None, []
    while True:
        params = {"limit": 100, "types": CASH_TYPES, "sortOrder": "SORT_ORDER_DESCENDING"}
        if cursor:
            params["cursor"] = cursor
        page = client.portfolio.activities(params)
        out.extend(page.get("activities", []))
        cursor = page.get("nextCursor")
        if page.get("eof") or not cursor:
            return out


def main():
    client = PolymarketUS(
        key_id=os.environ["POLYMARKET_KEY_ID"],
        secret_key=os.environ["POLYMARKET_SECRET_KEY"],
    )

    rows = []
    for act in fetch_transfers(client):
        change = act.get("accountBalanceChange", {})
        rows.append(
            {
                "date": (change.get("createTime") or "")[:10],
                "type": act["type"].replace("ACTIVITY_TYPE_ACCOUNT_", "").replace("ACTIVITY_TYPE_", ""),
                "amount": Decimal(change.get("amount", {}).get("value", "0")),
                "status": change.get("status", "").replace("ACCOUNT_BALANCE_CHANGE_STATUS_", ""),
                "id": change.get("transactionId", ""),
                "description": change.get("description", ""),
            }
        )

    print(f"{'DATE':<12} {'TYPE':<10} {'AMOUNT':>12}  {'STATUS':<10} {'ID'}")
    print("-" * 68)
    for r in rows:
        print(f"{r['date']:<12} {r['type']:<10} {r['amount']:>12,.2f}  {r['status']:<10} {r['id']}")

    done = [r for r in rows if r["status"] == "COMPLETED"]
    dep = sum(r["amount"] for r in done if "DEPOSIT" in r["type"])
    wd = sum(r["amount"] for r in done if "WITHDRAWAL" in r["type"])
    print("-" * 68)
    print(f"{len(rows)} records ({len(done)} completed)")
    print(f"Deposits:    {dep:>12,.2f}")
    print(f"Withdrawals: {wd:>12,.2f}")
    print(f"Net funded:  {dep - wd:>12,.2f}")


if __name__ == "__main__":
    main()
