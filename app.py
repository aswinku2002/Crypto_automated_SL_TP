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
SYMBOL = os.environ.get('DELTA_SYMBOL', 'BTCUSD')  # e.g., 'BTCUSD', 'ETHUSD'
ATR_PERIOD = 14
TIMEFRAME = '15m'  # 15-minute candle
CHECK_INTERVAL = 60  # seconds

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Initialize Delta Client ---
# The delta-rest-client library might have a different import structure.
# If the following fails, you may need to adjust the import based on the library's docs.
try:
    delta_client = DeltaRestClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        api_secret=API_SECRET
    )
    logger.info("Delta client initialized successfully.")
except Exception as e:
    logger.error(f"Failed to initialize Delta client: {e}")
    # The app will still start, but the bot thread will fail.
    delta_client = None

# --- Core Function: Calculate ATR ---
def calculate_atr():
    """Fetches 15-minute klines and calculates the ATR(14)."""
    if delta_client is None:
        logger.error("Delta client not available.")
        return None

    try:
        # Fetch the last (ATR_PERIOD + 1) 15-minute candles.
        # The 'resolution' parameter is in minutes.
        response = delta_client.get_klines(symbol=SYMBOL, resolution=15, limit=ATR_PERIOD + 1)

        # The structure of the response can vary. Adapt this based on the actual output.
        # It is often a list of lists: [ [timestamp, open, high, low, close, volume], ... ]
        # Or a list of dicts. The logic below assumes a list of lists or dicts.
        if isinstance(response, list) and len(response) > 0:
            if isinstance(response[0], list):
                # If it's a list of lists
                df = pd.DataFrame(response, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            elif isinstance(response[0], dict):
                # If it's a list of dicts
                df = pd.DataFrame(response)
            else:
                logger.error("Unexpected data format from get_klines.")
                return None
        else:
            logger.error("Could not parse klines data.")
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

        # Calculate ATR using Wilder's smoothing (EMA with alpha=1/period)
        # Drop the first row which will have NaN for TR
        df = df.dropna()
        if len(df) < ATR_PERIOD:
             logger.warning(f"Not enough data to calculate ATR for period {ATR_PERIOD}. Have {len(df)} rows.")
             return None

        atr = df['TR'].ewm(alpha=1/ATR_PERIOD, adjust=False).mean().iloc[-1]
        logger.info(f"Calculated ATR({ATR_PERIOD}) on {TIMEFRAME} chart: {atr}")
        return atr

    except Exception as e:
        logger.error(f"Error calculating ATR: {e}")
        return None

# --- Core Function: Manage Positions ---
def manage_positions():
    """Fetches open positions and sets SL/TP if they are missing."""
    if delta_client is None:
        return

    try:
        # Get all open positions
        positions = delta_client.get_positions()
        if not positions:
            logger.info("No open positions found.")
            return

        for position in positions:
            # Check if position is for our symbol and has size
            # The symbol might be in a field like 'product_symbol' or 'symbol'
            # Adjust based on the actual response structure.
            pos_symbol = position.get('product_symbol') or position.get('symbol')
            if pos_symbol != SYMBOL:
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
            logger.info(f"Found open position: {side} {abs(size)} {SYMBOL} @ {entry_price}")

            # Calculate ATR
            atr_value = calculate_atr()
            if atr_value is None:
                logger.warning("Skipping position management due to ATR calculation failure.")
                continue

            # Calculate SL and TP prices
            if side == 'BUY':
                stop_loss_price = entry_price - (2 * atr_value)
                take_profit_price = entry_price + (1.5 * atr_value)
            else:  # SELL
                stop_loss_price = entry_price + (2 * atr_value)
                take_profit_price = entry_price - (1.5 * atr_value)

            logger.info(f"Calculated levels for {side}: SL={stop_loss_price}, TP={take_profit_price}")

            # --- PLACE THE ORDERS ---
            # This is a critical part. You need to check if SL/TP orders already exist
            # to avoid duplication. The logic for this is highly dependent on the API.
            # This is a simplified placeholder.
            try:
                # Place a STOP order for Stop Loss
                # The order type 'stop_market' or 'stop_limit' can be used.
                # The 'stop_price' is the trigger price.
                # The actual implementation depends on the delta-rest-client version.
                logger.info("Attempting to place Stop-Loss order...")
                # sl_order = delta_client.place_order(
                #     symbol=SYMBOL,
                #     side='SELL' if side == 'BUY' else 'BUY',
                #     order_type='stop_market',
                #     size=abs(size),
                #     stop_price=stop_loss_price
                # )
                # logger.info(f"Stop-Loss order placed: {sl_order}")

                # Place a LIMIT order for Take Profit
                logger.info("Attempting to place Take-Profit order...")
                # tp_order = delta_client.place_order(
                #     symbol=SYMBOL,
                #     side='SELL' if side == 'BUY' else 'BUY',
                #     order_type='limit',
                #     size=abs(size),
                #     limit_price=take_profit_price
                # )
                # logger.info(f"Take-Profit order placed: {tp_order}")
                logger.info("Order placement simulated. Uncomment and adapt the `place_order` calls.")
                break # Process one position at a time for simplicity

            except Exception as e:
                logger.error(f"Failed to place orders for position {position.get('id')}: {e}")

    except Exception as e:
        logger.error(f"Error in position management loop: {e}")

# --- Bot Thread Function ---
def bot_worker():
    """The main loop for the trading bot."""
    logger.info("Bot worker thread started.")
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
    return jsonify({"status": "running", "message": f"Delta bot for {SYMBOL} is active."})

@app.route('/status')
def status():
    # Placeholder for a more detailed status endpoint
    return jsonify({"status": "ok"})

# --- Entrypoint for Render ---
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        logger.error("API Key or Secret not set. Please set DELTA_API_KEY and DELTA_API_SECRET environment variables.")
    else:
        # Start the bot in a background thread so the Flask app can run.
        bot_thread = threading.Thread(target=bot_worker, daemon=True)
        bot_thread.start()
        logger.info("Bot thread started.")

    port = int(os.environ.get("PORT", 10000))
    # Run the Flask app. Render expects the app to listen on 0.0.0.0
    app.run(host="0.0.0.0", port=port)
