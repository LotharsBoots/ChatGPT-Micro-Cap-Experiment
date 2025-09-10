"""Wrapper for the shared trading script using local data directory."""

from pathlib import Path
import sys

# Allow importing the shared module from the repository root
sys.path.append(str(Path(__file__).resolve().parents[1]))

from trading_script import main#, set_asof

# set_asof("2025-08-28")


if __name__ == "__main__":
    data_dir = Path(__file__).resolve().parents[1] / "Start Your Own"
    main(str(data_dir / "chatgpt_portfolio_update.csv"), data_dir)


