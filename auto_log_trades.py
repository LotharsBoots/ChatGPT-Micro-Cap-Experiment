import re
from pathlib import Path
import builtins
import sys

# ---- Import trading_script ----
sys.path.append(str(Path(__file__).resolve().parents[1]))
from trading_script import main as trading_main

# ---- Path to manual orders ----
txt_file = Path(
    r"C:\Users\TGWol\Downloads\ChatGPT-Micro-Cap-Experiment-Auto-log-data\ChatGPT-Micro-Cap-Experiment-Auto-log-data\Start Your Own\CopyDataInto.txt"
)

# ---- Parse orders from the .txt file ----
def parse_orders(txt_file):
    with open(txt_file, "r", encoding="utf-8") as f:
        content = f.read()

    # Split by "Order 1", "Order 2", etc.
    blocks = re.split(r"Order\s+\d+", content, flags=re.IGNORECASE)
    orders = []

    for block in blocks:
        if not block.strip():
            continue
        order = {}
        for line in block.splitlines():
            if ":" in line:
                key, val = [x.strip() for x in line.split(":", 1)]
                order[key.lower()] = val
        orders.append(order)
    return orders

# ---- Extract first numeric value only (safe) ----
def extract_number(s, default="0"):
    """Return the first numeric value in string s, or default if none."""
    if not s:
        return default
    match = re.search(r"[-+]?\d*\.?\d+", s)
    return match.group() if match else default

# ---- Build input sequence for the trading script ----
def build_input_sequence(orders):
    sequence = []
    for o in orders:
        action = o.get("action", "").lower()
        ticker = o.get("ticker", "").strip()
        order_type = o.get("order type", "").lower()
        shares = o.get("shares", "").strip()
        stop_loss = extract_number(o.get("stop loss", "0"), default="0")
        limit_price = extract_number(o.get("limit price", "0"), default="0")

        # ---- Action ----
        if action == "buy":
            sequence.append("b")
        elif action == "sell":
            sequence.append("s")
        else:
            sequence.append("")  # default (continue)

        # ---- Ticker ----
        sequence.append(ticker)

        # ---- Pathways ----
        if action == "buy" and "market" in order_type:
            # Buy Market-On-Open
            sequence.append("m")
            sequence.append(shares if shares else "0")
            sequence.append(stop_loss)
        elif action == "buy" and "limit" in order_type:
            # Buy Limit
            sequence.append("l")
            sequence.append(shares if shares else "0")
            sequence.append(limit_price)
            sequence.append(stop_loss)
            sequence.append("")  # confirmation (press enter)
        elif action == "sell":
            # Sell Limit
            sequence.append(shares if shares else "0")
            sequence.append(limit_price)
            sequence.append("")  # confirmation (press enter)

    return sequence

# ---- Patch input() ----
class InputPatcher:
    def __init__(self, responses):
        self.responses = responses
        self.index = 0
        self.original_input = builtins.input

    def __enter__(self):
        builtins.input = self.mock_input
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        builtins.input = self.original_input

    def mock_input(self, prompt=""):
        if self.index < len(self.responses):
            response = self.responses[self.index]
            print(prompt + response)  # echo simulated input
            self.index += 1
            return response
        return ""  # fallback if we run out

# ---- Main execution ----
def main():
    orders = parse_orders(txt_file)
    if not orders:
        print(f"No orders found in {txt_file}")
        return

    input_sequence = build_input_sequence(orders)
    with InputPatcher(input_sequence):
        data_dir = Path(__file__).resolve().parent / "Start Your Own"
        trading_main(str(data_dir / "chatgpt_portfolio_update.csv"), data_dir)

if __name__ == "__main__":
    main()
