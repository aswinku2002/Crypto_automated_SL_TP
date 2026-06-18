import os
import time
import threading
import logging
from flask import Flask, jsonify
from delta_rest_client import DeltaRestClient
import pandas as pd
import requests

# --- Configuration ---
# These will be set as Environment Variables on Render
API_KEY = os.environ.get('DELTA_API_KEY')
API_SECRET = os.environ.get('DELTA_API_SECRET')
# Use testnet for safety, set to 'https://api.delta.exchange' for production
BASE_URL = os.environ.get('DELTA_BASE_URL', 'https://testnet-api.delta.exchange')
ATR_PERIOD = 14
TIMEFRAME = '15m'  # 15-minute candle
CHECK_INTERVAL = 60  # seconds

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Initialize Delta Client ---
try:
    delta_client = DeltaRestClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        api_secret=API_SECRET
    )
    logger.info("Delta client initialized successfully.")
except Exception as e:
    logger.error(f"Failed to initialize Delta client: {e}")
    delta_client = None

# --- Core Function: Calculate ATR for ANY symbol ---
def calculate_atr(symbol):
    """Fetches 15-minute klines and calculates the ATR(14) for any symbol."""
    if delta_client is None:
        logger.error("Delta client not available.")
        return None

    try:
        # Fetch the last (ATR_PERIOD + 1) 15-minute candles
        response = delta_client.get_klines(symbol=symbol, resolution=15, limit=ATR_PERIOD + 1)

        # Parse the response
        if isinstance(response, list) and len(response) > 0:
            if isinstance(response[0], list):
                df = pd.DataFrame(response, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            elif isinstance(response[0], dict):
                df = pd.DataFrame(response)
            else:
                logger.error(f"Unexpected data format from get_klines for {symbol}.")
                return None
        else:
            logger.error(f"Could not parse klines data for {symbol}.")
            return None

        # Ensure columns are numeric
        df['high'] = pd.to_numeric(df['high'])
        df['low'] = pd.to_numeric(df['low'])
        df['close'] = pd.to_numeric(df['close'])

        # Calculate True Range (TR)
        df['prev_close'] = df['close'].shift(1)
        df['tr1'] = df['high'] - df['low']
        df['tr2'] = (df['high'] - df['prev_close']).abs()
        df['tr3'] = (df['low'] - df['prev_close']).abs()
        df['TR'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)

        # Calculate ATR using Wilder's smoothing
        df = df.dropna()
        if len(df) < ATR_PERIOD:
             logger.warning(f"Not enough data to calculate ATR for {symbol}. Have {len(df)} rows.")
             return None

        atr = df['TR'].ewm(alpha=1/ATR_PERIOD, adjust=False).mean().iloc[-1]
        logger.info(f"Calculated ATR({ATR_PERIOD}) for {symbol}: {atr}")
        return atr

    except Exception as e:
        logger.error(f"Error calculating ATR for {symbol}: {e}")
        return None

# --- Core Function: Check if SL/TP Orders Already Exist ---
def orders_already_exist(symbol, side, sl_price, tp_price):
    """Check if SL and TP orders already exist for this position."""
    try:
        open_orders = delta_client.get_orders()
        if not open_orders:
            return False
        
        sl_exists = False
        tp_exists = False
        
        for order in open_orders:
            order_symbol = order.get('product_symbol') or order.get('symbol')
            if order_symbol != symbol:
                continue
            
            order_side = order.get('side')
            order_type = order.get('order_type')
            order_price = float(order.get('price', 0))
            
            # Check for Stop Loss order (opposite side)
            if order_side == ('SELL' if side == 'BUY' else 'BUY'):
                if 'stop' in order_type.lower():
                    # Check if the stop price is close to our calculated SL
                    stop_price = float(order.get('stop_price', 0))
                    if abs(stop_price - sl_price) < 0.01:  # Small tolerance
                        sl_exists = True
                # Check for Take Profit order (limit order)
                elif 'limit' in order_type.lower():
                    if abs(order_price - tp_price) < 0.01:  # Small tolerance
                        tp_exists = True
        
        return sl_exists and tp_exists
        
    except Exception as e:
        logger.error(f"Error checking existing orders: {e}")
        return False

# --- Core Function: Manage Positions ---
def manage_positions():
    """Fetches ALL open positions and sets SL/TP for each one."""
    if delta_client is None:
        return

    try:
        # Get all open positions
        positions = delta_client.get_positions()
        if not positions:
            logger.info("No open positions found.")
            return

        processed_count = 0
        for position in positions:
            # Get position details
            pos_symbol = position.get('product_symbol') or position.get('symbol')
            if not pos_symbol:
                continue
                
            size = float(position.get('size', 0))
            if size == 0:
                continue

            entry_price = float(position.get('entry_price', 0))
            if entry_price == 0:
                logger.warning(f"Could not fetch entry price for position {position.get('id')}")
                continue

            # Determine side
            side = 'BUY' if size > 0 else 'SELL'
            logger.info(f"Found open position: {side} {abs(size)} {pos_symbol} @ {entry_price}")

            # Calculate ATR for this specific symbol
            atr_value = calculate_atr(pos_symbol)
            if atr_value is None:
                logger.warning(f"Skipping {pos_symbol} due to ATR calculation failure.")
                continue

            # Calculate SL and TP prices
            if side == 'BUY':
                stop_loss_price = entry_price - (2 * atr_value)
                take_profit_price = entry_price + (1.5 * atr_value)
            else:  # SELL
                stop_loss_price = entry_price + (2 * atr_value)
                take_profit_price = entry_price - (1.5 * atr_value)

            logger.info(f"Calculated levels for {pos_symbol} {side}: SL={stop_loss_price:.2f}, TP={take_profit_price:.2f}")

            # Check if orders already exist
            if orders_already_exist(pos_symbol, side, stop_loss_price, take_profit_price):
                logger.info(f"SL/TP orders already exist for {pos_symbol}. Skipping.")
                continue

            # --- PLACE THE ORDERS ---
            try:
                # Place Stop Loss order
                logger.info(f"Placing Stop-Loss for {pos_symbol}...")
                # sl_order = delta_client.place_order(
                #     symbol=pos_symbol,
                #     side='SELL' if side == 'BUY' else 'BUY',
                #     order_type='stop_market',
                #     size=abs(size),
                #     stop_price=stop_loss_price
                # )
                # logger.info(f"Stop-Loss placed: {sl_order}")

                # Place Take Profit order
                logger.info(f"Placing Take-Profit for {pos_symbol}...")
                # tp_order = delta_client.place_order(
                #     symbol=pos_symbol,
                #     side='SELL' if side == 'BUY' else 'BUY',
                #     order_type='limit',
                #     size=abs(size),
                #     limit_price=take_profit_price
                # )
                # logger.info(f"Take-Profit placed: {tp_order}")
                
                logger.info(f"Order placement simulated for {pos_symbol}. Uncomment `place_order` calls.")
                processed_count += 1

            except Exception as e:
                logger.error(f"Failed to place orders for {pos_symbol}: {e}")

        if processed_count > 0:
            logger.info(f"Processed {processed_count} positions.")
        else:
            logger.info("No new positions needed SL/TP setup.")

    except Exception as e:
        logger.error(f"Error in position management loop: {e}")

# --- Bot Thread Function ---
def bot_worker():
    """The main loop for the trading bot."""
    logger.info("Bot worker thread started - monitoring ALL positions.")
    while True:
        try:
            manage_positions()
        except Exception as e:
            logger.error(f"Unhandled error in bot worker: {e}")
        time.sleep(CHECK_INTERVAL)

# --- Flask Web App ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return jsonify({
        "status": "running", 
        "message": "Delta bot is monitoring ALL positions across ALL currencies.",
        "check_interval": f"{CHECK_INTERVAL} seconds"
    })

@app.route('/status')
def status():
    return jsonify({
        "status": "ok",
        "atr_period": ATR_PERIOD,
        "timeframe": TIMEFRAME
    })

# --- Entrypoint for Render ---
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        logger.error("API Key or Secret not set. Please set DELTA_API_KEY and DELTA_API_SECRET environment variables.")
    else:
        # Start the bot in a background thread
        bot_thread = threading.Thread(target=bot_worker, daemon=True)
        bot_thread.start()
        logger.info("Bot thread started - monitoring ALL trading pairs.")

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
