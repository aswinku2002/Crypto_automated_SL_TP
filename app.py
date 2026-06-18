import os
import time
import threading
import logging
from flask import Flask, jsonify
from delta_rest_client import DeltaRestClient
import pandas as pd

# --- Configuration ---
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

# --- Core Function: Get Product ID from Symbol ---
def get_product_id(symbol):
    """Fetches the numeric product_id for a given symbol."""
    try:
        products = delta_client.get_products()
        for product in products:
            if product.get('symbol') == symbol:
                return product.get('id')
        logger.error(f"Product ID not found for symbol: {symbol}")
        return None
    except Exception as e:
        logger.error(f"Error fetching product ID for {symbol}: {e}")
        return None

# --- Core Function: Calculate ATR for ANY symbol ---
def calculate_atr(symbol):
    """Fetches 15-minute klines and calculates the ATR(14) for any symbol."""
    if delta_client is None:
        return None

    try:
        response = delta_client.get_klines(symbol=symbol, resolution=15, limit=ATR_PERIOD + 1)

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

        df = df.dropna()
        if len(df) < ATR_PERIOD:
             logger.warning(f"Not enough data to calculate ATR for {symbol}.")
             return None

        atr = df['TR'].ewm(alpha=1/ATR_PERIOD, adjust=False).mean().iloc[-1]
        logger.info(f"Calculated ATR({ATR_PERIOD}) for {symbol}: {atr}")
        return atr

    except Exception as e:
        logger.error(f"Error calculating ATR for {symbol}: {e}")
        return None

# --- Core Function: Check if Bracket Orders Already Exist ---
def bracket_orders_exist(position_id):
    """Checks if any bracket orders (SL/TP) exist for the given position ID."""
    try:
        orders = delta_client.get_orders()
        if not orders:
            return False

        for order in orders:
            # Bracket orders are linked to the parent position/order. The exact linking field may vary.
            # Look for orders where the parent_order_id matches your position_id or similar logic.
            # This example assumes the position_id is the ID from get_positions().
            if order.get('parent_order_id') == position_id:
                return True
            # Alternative: if the position_id isn't the parent, you may need to
            # check against the product_id or a custom mapping.
            # The specific check depends on the `delta-rest-client` and API response.
        return False

    except Exception as e:
        logger.error(f"Error checking for bracket orders: {e}")
        return False

# --- Core Function: Place Bracket Orders ---
def place_bracket_orders(position, product_id, atr_value):
    """Places stop-loss and take-profit orders for a given position."""
    pos_symbol = position.get('product_symbol') or position.get('symbol')
    size = float(position.get('size', 0))
    entry_price = float(position.get('entry_price', 0))
    side = 'BUY' if size > 0 else 'SELL'

    # Calculate SL and TP prices
    if side == 'BUY':
        stop_loss_price = entry_price - (2 * atr_value)
        take_profit_price = entry_price + (1.5 * atr_value)
        sl_side = 'SELL'
        tp_side = 'SELL'
    else:  # SELL
        stop_loss_price = entry_price + (2 * atr_value)
        take_profit_price = entry_price - (1.5 * atr_value)
        sl_side = 'BUY'
        tp_side = 'BUY'

    logger.info(f"Placing bracket for {pos_symbol}: SL={stop_loss_price:.2f}, TP={take_profit_price:.2f}")

    try:
        # Use reduce-only flag to ensure these orders only close the position [citation:2]
        # Place Stop Loss order (uses place_stop_order with stop_price) [citation:9]
        sl_order = delta_client.place_stop_order(
            product_id=product_id,
            size=abs(size),
            side=sl_side,
            stop_price=stop_loss_price,
            order_type='MARKET',  # Use 'LIMIT' for a limit stop-loss
            reduce_only=True
        )
        logger.info(f"Stop-Loss placed: {sl_order}")

        # Place Take Profit order (uses place_order with limit_price)
        tp_order = delta_client.place_order(
            product_id=product_id,
            size=abs(size),
            side=tp_side,
            limit_price=take_profit_price,
            order_type='LIMIT',
            reduce_only=True
        )
        logger.info(f"Take-Profit placed: {tp_order}")
        
        return True
    except Exception as e:
        logger.error(f"Failed to place bracket orders for {pos_symbol}: {e}")
        return False

# --- Core Function: Manage Positions ---
def manage_positions():
    """Fetches ALL open positions and sets SL/TP for each one."""
    if delta_client is None:
        return

    try:
        positions = delta_client.get_positions()
        if not positions:
            logger.info("No open positions found.")
            return

        for position in positions:
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

            # Get the product ID for this symbol
            product_id = get_product_id(pos_symbol)
            if product_id is None:
                continue

            # Check if bracket orders already exist for this position
            # Pass the product_id or position ID to check linked orders
            if bracket_orders_exist(position.get('id')):
                logger.info(f"Bracket orders already exist for {pos_symbol}. Skipping.")
                continue

            # Calculate ATR for this specific symbol
            atr_value = calculate_atr(pos_symbol)
            if atr_value is None:
                logger.warning(f"Skipping {pos_symbol} due to ATR calculation failure.")
                continue

            # Place bracket orders
            place_bracket_orders(position, product_id, atr_value)

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
        logger.error("API Key or Secret not set.")
    else:
        bot_thread = threading.Thread(target=bot_worker, daemon=True)
        bot_thread.start()
        logger.info("Bot thread started - monitoring ALL trading pairs.")

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
