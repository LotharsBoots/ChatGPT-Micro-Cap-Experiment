import os
from dotenv import load_dotenv
from alpaca_trade_api import REST
load_dotenv()
api = REST(os.getenv("ALPACA_KEY_ID"), os.getenv("ALPACA_SECRET_KEY"), os.getenv("ALPACA_BASE_URL"))
acct = api.get_account()
print("Account status:", acct.status)
print("Paper:", acct.account_number.startswith("PA"))