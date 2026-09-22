import os

from dotenv import load_dotenv
from polymarket_us import PolymarketUS

load_dotenv()

client = PolymarketUS(
    key_id=os.environ["POLYMARKET_KEY_ID"],
    secret_key=os.environ["POLYMARKET_SECRET_KEY"],
)

balances = client.account.balances()
positions = client.portfolio.positions()
open_orders = client.orders.list()

print(balances, positions, open_orders)