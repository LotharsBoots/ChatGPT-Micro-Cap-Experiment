from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_socketio import SocketIO, emit
import threading
import time
import json
import os
import pandas as pd
from datetime import datetime, timedelta
import logging
from pathlib import Path
import queue
import traceback
from datetime import time as dt_time
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
import functools
import os as _os
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# Import your trading script functions
from trading_script import (
    process_portfolio, daily_results, load_latest_portfolio_state,
    set_data_dir, set_asof, main as trading_main,
    auto_trade_once,
)

load_dotenv()  # load .env if present

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-me')
socketio = SocketIO(app, cors_allowed_origins="*")
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'changeme')

# Global variables for trading state
trading_status = {
    'is_running': False,
    'last_update': None,
    'current_portfolio': None,
    'cash_balance': 0.0,
    'total_equity': 0.0,
    'daily_pnl': 0.0,
    'max_drawdown': 0.0,
    'sharpe_ratio': 0.0,
    'error': None
}

scheduler = BackgroundScheduler(timezone=pytz.timezone('US/Eastern'))
autotrade_job_id = 'autotrade_job'
autotrade_schedule = {
    'enabled': False,
    'interval_minutes': int(os.getenv('SCHED_INTERVAL_MINUTES', '15')),
    'market_hours_only': os.getenv('SCHED_MARKET_HOURS_ONLY', 'true').lower() == 'true',
}

# Queue for communication between trading thread and web UI
status_queue = queue.Queue()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---- Auto-trade config helpers ----
DEFAULT_AUTOTRADE = {
    "universe": ["SPY", "IWM", "QQQ", "XBI"],
    "max_positions": 5,
    "per_trade_cash_pct": 0.2,
    "stop_loss_pct": 0.1,
    "prompt": "",
}

def _autotrade_path() -> Path:
    return Path(__file__).resolve().parent / "autotrade.json"

def read_autotrade_config() -> dict:
    p = _autotrade_path()
    try:
        if p.exists():
            with p.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    cfg = DEFAULT_AUTOTRADE.copy()
                    cfg.update(data)
                    return cfg
    except Exception:
        pass
    return DEFAULT_AUTOTRADE.copy()

def write_autotrade_config(cfg: dict) -> None:
    p = _autotrade_path()
    try:
        with p.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception as e:
        logger.error("Failed to write autotrade.json: %s", e)

def get_portfolio_data():
    """Get current portfolio data for display"""
    try:
        script_dir = Path(__file__).resolve().parent
        data_dir = script_dir / "Start Your Own"
        set_data_dir(data_dir)
        portfolio_csv = data_dir / "chatgpt_portfolio_update.csv"
        
        if not portfolio_csv.exists():
            return None, 0.0
            
        portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))
        return portfolio, cash
    except Exception as e:
        logger.error(f"Error loading portfolio: {e}")
        return None, 0.0

def get_trading_history():
    """Get trading history for charts"""
    try:
        script_dir = Path(__file__).resolve().parent
        data_dir = script_dir / "Start Your Own"
        set_data_dir(data_dir)
        portfolio_csv = data_dir / "chatgpt_portfolio_update.csv"
        
        if not portfolio_csv.exists():
            return []
            
        df = pd.read_csv(portfolio_csv)
        totals = df[df["Ticker"] == "TOTAL"].copy()
        
        if totals.empty:
            return []
            
        totals["Date"] = pd.to_datetime(totals["Date"])
        totals = totals.sort_values("Date")
        
        history = []
        for _, row in totals.iterrows():
            history.append({
                'date': row['Date'].strftime('%Y-%m-%d'),
                'equity': float(row['Total Equity']) if pd.notna(row['Total Equity']) else 0.0,
                'cash': float(row['Cash Balance']) if pd.notna(row['Cash Balance']) else 0.0,
                'pnl': float(row['PnL']) if pd.notna(row['PnL']) else 0.0
            })
        
        return history
    except Exception as e:
        logger.error(f"Error loading trading history: {e}")
        return []

# ------------------- Auth helpers (disabled) -------------------
def login_required(func):
    # Auth intentionally disabled to keep the skeleton simple
    return func

@app.route('/login', methods=['GET', 'POST'])
def login():
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    return redirect(url_for('index'))

@app.route('/autotrade/config', methods=['GET', 'POST'])
@login_required
def autotrade_config():
    """Get or set auto-trading configuration."""
    if request.method == 'GET':
        return jsonify(read_autotrade_config())
    try:
        incoming = request.get_json(force=True) or {}
        cfg = read_autotrade_config()
        # sanitize & merge
        if 'universe' in incoming:
            uni = incoming['universe']
            if isinstance(uni, str):
                uni = [t.strip().upper() for t in uni.split(',') if t.strip()]
            elif isinstance(uni, list):
                uni = [str(t).strip().upper() for t in uni if str(t).strip()]
            else:
                uni = cfg['universe']
            cfg['universe'] = uni
        if 'max_positions' in incoming:
            cfg['max_positions'] = int(incoming['max_positions'])
        if 'per_trade_cash_pct' in incoming:
            cfg['per_trade_cash_pct'] = float(incoming['per_trade_cash_pct'])
        if 'stop_loss_pct' in incoming:
            cfg['stop_loss_pct'] = float(incoming['stop_loss_pct'])
        if 'prompt' in incoming:
            cfg['prompt'] = str(incoming['prompt'] or '')
        write_autotrade_config(cfg)
        return jsonify({"status": "success", "config": cfg})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/autotrade/run', methods=['POST'])
@login_required
def autotrade_run():
    """Run one auto-trading pass: evaluate rules and place buys if criteria met."""
    try:
        script_dir = Path(__file__).resolve().parent
        data_dir = script_dir / "Start Your Own"
        set_data_dir(data_dir)

        portfolio_csv = data_dir / "chatgpt_portfolio_update.csv"
        if portfolio_csv.exists():
            portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))
        else:
            portfolio = pd.DataFrame(columns=["ticker", "shares", "stop_loss", "buy_price", "cost_basis"])
            cash = float(os.environ.get('STARTING_CASH', '10000'))

        # Run auto-buyer
        portfolio_df, cash, executed = auto_trade_once(portfolio, cash, base_dir=data_dir)

        # Price and persist results
        portfolio_df, cash = process_portfolio(portfolio_df, cash, interactive=False)

        # Prepare response
        current_portfolio = portfolio_df.to_dict('records')
        total_value = sum(float(s.get('shares', 0)) * float(s.get('buy_price', 0)) for s in current_portfolio)
        total_equity = total_value + cash

        return jsonify({
            "status": "success",
            "executed": executed,
            "portfolio": current_portfolio,
            "cash": cash,
            "total_equity": total_equity,
        })
    except Exception as e:
        logger.exception("Auto-trade failed")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/autotrade/ai_run', methods=['POST'])
@login_required
def autotrade_ai_run():
    """Use an LLM to decide daily buys/sells based on user's prompt and recent data."""
    try:
        script_dir = Path(__file__).resolve().parent
        data_dir = script_dir / "Start Your Own"
        set_data_dir(data_dir)

        cfg = read_autotrade_config()
        prompt = cfg.get('prompt') or ''
        universe = cfg.get('universe') or []
        if not prompt or not universe:
            return jsonify({'status': 'error', 'message': 'Set prompt and universe first in Auto-Trading.'}), 400

        # Build simple context (latest close/volume for each ticker)
        from trading_script import download_price_data, last_trading_date
        end_d = last_trading_date()
        start_d = end_d - pd.Timedelta(days=5)
        briefs = []
        for t in universe:
            try:
                df = download_price_data(str(t).upper(), start=start_d, end=end_d + pd.Timedelta(days=1), progress=False).df
                if df.empty:
                    continue
                close = float(df['Close'].iloc[-1])
                vol = float(df['Volume'].iloc[-1])
                briefs.append({'ticker': str(t).upper(), 'close': close, 'volume': int(vol)})
            except Exception:
                continue

        if OpenAI is None or not _os.getenv('OPENAI_API_KEY'):
            return jsonify({'status': 'error', 'message': 'OPENAI_API_KEY not set; cannot run AI. Configure it in .env.'}), 400

        client = OpenAI()
        system_msg = (
            'You are a trading assistant. Return ONLY strict JSON with keys "buy" and "sell". '
            'Format: {"buy":[{"ticker":"SPY","percent":0.2,"stop":0.1}],"sell":["IWM"]}. '
            'Percents must sum to <= 1.0. Use only tickers provided.'
        )
        user_msg = {
            'prompt': prompt,
            'universe': universe,
            'market': briefs,
        }
        resp = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {'role': 'system', 'content': system_msg},
                {'role': 'user', 'content': json.dumps(user_msg)},
            ],
            temperature=0.2,
        )
        content = resp.choices[0].message.content if resp and resp.choices else '{}'
        try:
            plan = json.loads(content)
        except Exception:
            return jsonify({'status': 'error', 'message': 'AI did not return valid JSON.'}), 500

        # Load current state
        portfolio_csv = data_dir / 'chatgpt_portfolio_update.csv'
        if portfolio_csv.exists():
            portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))
            df = pd.DataFrame(portfolio) if isinstance(portfolio, list) else portfolio.copy()
        else:
            df = pd.DataFrame(columns=["ticker", "shares", "stop_loss", "buy_price", "cost_basis"])
            cash = float(os.environ.get('STARTING_CASH', '10000'))

        # Execute sells
        for t in (plan.get('sell') or []):
            tkr = str(t).upper()
            row = df.loc[df.get('ticker').astype(str).str.upper() == tkr]
            if row.empty:
                continue
            shares = float(row.iloc[0].get('shares', 0))
            if shares <= 0:
                continue
            # price using latest close
            try:
                s = download_price_data(tkr, start=start_d, end=end_d + pd.Timedelta(days=1), progress=False).df
                if s.empty:
                    continue
                px = float(s['Close'].iloc[-1])
                from trading_script import log_manual_sell
                cash, df = log_manual_sell(px, shares, tkr, cash, df, reason='AI SELL', interactive=False)
            except Exception:
                continue

        # Execute buys
        for b in (plan.get('buy') or []):
            try:
                tkr = str(b.get('ticker', '')).upper()
                pct = float(b.get('percent', 0))
                stop = float(b.get('stop', 0))
                if not tkr or pct <= 0 or pct > 1:
                    continue
                # get latest close
                s = download_price_data(tkr, start=start_d, end=end_d + pd.Timedelta(days=1), progress=False).df
                if s.empty:
                    continue
                px = float(s['Close'].iloc[-1])
                budget = cash * pct
                shares = int(budget // px)
                if shares < 1:
                    continue
                from trading_script import log_manual_buy
                cash, df = log_manual_buy(px, shares, tkr, stop, cash, df, interactive=False)
            except Exception:
                continue

        # Price and persist
        df, cash = process_portfolio(df, cash, interactive=False)
        current_portfolio = df.to_dict('records')
        total_value = sum(float(s.get('shares', 0)) * float(s.get('buy_price', 0)) for s in current_portfolio)
        total_equity = total_value + cash

        return jsonify({'status': 'success', 'plan': plan, 'portfolio': current_portfolio, 'cash': cash, 'total_equity': total_equity})
    except Exception as e:
        logger.exception('AI autotrade failed')
        return jsonify({'status': 'error', 'message': str(e)}), 500

def _within_market_hours(now_et=None):
    tz = pytz.timezone('US/Eastern')
    now = now_et or datetime.now(tz)
    # Monday-Friday, 9:30-16:00 ET
    if now.weekday() > 4:
        return False
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= now <= end

def _autotrade_job():
    try:
        if autotrade_schedule.get('market_hours_only', True) and not _within_market_hours():
            return
        # call the same one-shot auto-trade
        with app.app_context():
            autotrade_run()
    except Exception:
        logger.exception('Scheduled auto-trade failed')

@app.route('/autotrade/schedule', methods=['GET', 'POST'])
@login_required
def autotrade_schedule_api():
    global autotrade_schedule
    if request.method == 'GET':
        # report status
        running = scheduler.get_job(autotrade_job_id) is not None
        return jsonify({**autotrade_schedule, 'running': running})

    data = request.get_json(force=True) or {}
    enabled = bool(data.get('enabled', False))
    interval = int(data.get('interval_minutes', autotrade_schedule['interval_minutes']))
    market_only = bool(data.get('market_hours_only', True))
    autotrade_schedule.update({'enabled': enabled, 'interval_minutes': interval, 'market_hours_only': market_only})

    # clear existing
    job = scheduler.get_job(autotrade_job_id)
    if job:
        job.remove()

    if enabled:
        scheduler.add_job(_autotrade_job, 'interval', minutes=max(1, interval), id=autotrade_job_id, replace_existing=True)
        if not scheduler.running:
            scheduler.start()

    return jsonify({'status': 'success', **autotrade_schedule})

def automated_trading_worker():
    """Background worker for automated trading"""
    global trading_status
    
    try:
        trading_status['is_running'] = True
        trading_status['error'] = None
        
        # Set data directory to Start Your Own
        script_dir = Path(__file__).resolve().parent
        data_dir = script_dir / "Start Your Own"
        set_data_dir(data_dir)
        
        # Load current portfolio state
        portfolio_csv = data_dir / "chatgpt_portfolio_update.csv"
        
        if not portfolio_csv.exists():
            # Create initial portfolio if none exists
            portfolio = pd.DataFrame(columns=["ticker", "shares", "stop_loss", "buy_price", "cost_basis"])
            cash = float(os.getenv('STARTING_CASH', '10000'))
            trading_status['current_portfolio'] = portfolio.to_dict('records')
            trading_status['cash_balance'] = cash
            trading_status['total_equity'] = cash
        else:
            portfolio, cash = load_latest_portfolio_state(str(portfolio_csv))
            trading_status['current_portfolio'] = portfolio if isinstance(portfolio, list) else portfolio.to_dict('records')
            trading_status['cash_balance'] = cash
            trading_status['total_equity'] = cash
        
        # Process portfolio (non-interactive mode)
        portfolio_df = pd.DataFrame(trading_status['current_portfolio']) if trading_status['current_portfolio'] else pd.DataFrame()
        portfolio_df, cash = process_portfolio(portfolio_df, cash, interactive=False)
        
        # Update status
        trading_status['current_portfolio'] = portfolio_df.to_dict('records')
        trading_status['cash_balance'] = cash
        
        # Calculate total equity
        total_value = sum(float(stock.get('shares', 0)) * float(stock.get('buy_price', 0)) for stock in trading_status['current_portfolio'])
        trading_status['total_equity'] = total_value + cash
        
        # Get daily results for metrics
        try:
            # Capture daily_results output for metrics
            import io
            from contextlib import redirect_stdout
            
            output = io.StringIO()
            with redirect_stdout(output):
                daily_results(portfolio_df, cash)
            
            # Parse metrics from output (simplified)
            trading_status['daily_pnl'] = 0.0  # Will be calculated from CSV
            trading_status['max_drawdown'] = 0.0  # Will be calculated from CSV
            trading_status['sharpe_ratio'] = 0.0  # Will be calculated from CSV
            
        except Exception as e:
            logger.error(f"Error getting daily results: {e}")
        
        trading_status['last_update'] = datetime.now().isoformat()
        trading_status['is_running'] = False
        
        # Emit status update to web UI
        socketio.emit('trading_update', trading_status)
        
    except Exception as e:
        trading_status['error'] = str(e)
        trading_status['is_running'] = False
        logger.error(f"Trading error: {e}")
        traceback.print_exc()
        socketio.emit('trading_error', {'error': str(e)})

@app.route('/')
@login_required
def index():
    """Main dashboard page"""
    portfolio, cash = get_portfolio_data()
    history = get_trading_history()
    
    return render_template('index.html', 
                         portfolio=portfolio, 
                         cash=cash, 
                         history=history,
                         trading_status=trading_status)

@app.route('/start_trading', methods=['POST'])
@login_required
def start_trading():
    """Start automated trading"""
    if trading_status['is_running']:
        return jsonify({'status': 'error', 'message': 'Trading already in progress'})
    
    # Start trading in background thread
    thread = threading.Thread(target=automated_trading_worker)
    thread.daemon = True
    thread.start()
    
    return jsonify({'status': 'success', 'message': 'Trading started'})

@app.route('/stop_trading', methods=['POST'])
@login_required
def stop_trading():
    """Stop automated trading"""
    trading_status['is_running'] = False
    return jsonify({'status': 'success', 'message': 'Trading stopped'})

@app.route('/get_status')
@login_required
def get_status():
    """Get current trading status"""
    return jsonify(trading_status)

@app.route('/get_portfolio')
@login_required
def get_portfolio():
    """Get current portfolio data"""
    portfolio, cash = get_portfolio_data()
    return jsonify({
        'portfolio': portfolio,
        'cash': cash,
        'total_equity': sum(float(stock.get('shares', 0)) * float(stock.get('buy_price', 0)) for stock in (portfolio or [])) + cash
    })

@app.route('/get_history')
@login_required
def get_history():
    """Get trading history"""
    history = get_trading_history()
    return jsonify(history)

@app.route('/configure', methods=['GET', 'POST'])
@login_required
def configure():
    """Configuration page"""
    if request.method == 'POST':
        try:
            data = request.get_json()
            
            # Update configuration
            if 'starting_cash' in data:
                # This would update the initial cash amount
                pass
                
            if 'benchmarks' in data:
                # Update benchmark tickers
                script_dir = Path(__file__).resolve().parent
                tickers_file = script_dir / "tickers.json"
                
                config = {
                    "benchmarks": data['benchmarks']
                }
                
                with open(tickers_file, 'w') as f:
                    json.dump(config, f, indent=2)
            
            return jsonify({'status': 'success'})
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e)})
    
    # Load current configuration
    script_dir = Path(__file__).resolve().parent
    tickers_file = script_dir / "tickers.json"
    
    config = {}
    if tickers_file.exists():
        try:
            with open(tickers_file, 'r') as f:
                config = json.load(f)
        except:
            config = {"benchmarks": ["IWO", "XBI", "SPY", "IWM"]}
    else:
        config = {"benchmarks": ["IWO", "XBI", "SPY", "IWM"]}
    
    # Also surface .env-based settings to the page
    env_settings = {
        'starting_cash': float(os.getenv('STARTING_CASH', '10000')),
        'sched_interval': int(os.getenv('SCHED_INTERVAL_MINUTES', '15')),
        'sched_market_hours_only': os.getenv('SCHED_MARKET_HOURS_ONLY', 'true').lower() == 'true',
        'debug': os.getenv('DEBUG', 'false').lower() == 'true',
    }
    return render_template('configure.html', config=config, env_settings=env_settings)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_api():
    """Read or update environment-like settings via a local file (settings.json) so user can edit in UI without touching .env."""
    settings_path = Path(__file__).resolve().parent / 'settings.json'
    if request.method == 'GET':
        if settings_path.exists():
            try:
                with settings_path.open('r', encoding='utf-8') as fh:
                    return jsonify(json.load(fh))
            except Exception:
                pass
        # fallback to environment
        return jsonify({
            'STARTING_CASH': os.getenv('STARTING_CASH', '10000'),
            'SCHED_INTERVAL_MINUTES': os.getenv('SCHED_INTERVAL_MINUTES', '15'),
            'SCHED_MARKET_HOURS_ONLY': os.getenv('SCHED_MARKET_HOURS_ONLY', 'true'),
            'DEBUG': os.getenv('DEBUG', 'false'),
        })

    # POST -> write settings.json (non-secret), which the app will read on next start if desired
    try:
        data = request.get_json(force=True) or {}
        with settings_path.open('w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@socketio.on('connect')
def handle_connect():
    """Handle WebSocket connection"""
    emit('status', trading_status)

@socketio.on('disconnect')
def handle_disconnect():
    """Handle WebSocket disconnection"""
    pass

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
