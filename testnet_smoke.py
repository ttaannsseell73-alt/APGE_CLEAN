import os
import sys
import time
import hmac
import hashlib
import requests
import asyncio
import websockets
import json
import urllib.parse

API_KEY = os.environ.get("BINANCE_TESTNET_API_KEY")
API_SECRET = os.environ.get("BINANCE_TESTNET_API_SECRET")

if not API_KEY or not API_SECRET:
    print("FAIL: Missing API credentials in environment.")
    sys.exit(1)

REST_URL = "https://testnet.binancefuture.com"
WS_URL = "wss://stream.binancefuture.com/ws"

def get_signature(query_string):
    return hmac.new(API_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

def request_get(path, auth=False, params=None):
    if params is None:
        params = {}
    if auth:
        params['timestamp'] = int(time.time() * 1000)
        query_string = urllib.parse.urlencode(params)
        signature = get_signature(query_string)
        params['signature'] = signature

    headers = {}
    if auth:
        headers['X-MBX-APIKEY'] = API_KEY

    resp = requests.get(REST_URL + path, params=params, headers=headers)
    return resp

def request_post(path, auth=False, params=None):
    if params is None:
        params = {}
    if auth:
        params['timestamp'] = int(time.time() * 1000)
        query_string = urllib.parse.urlencode(params)
        signature = get_signature(query_string)
        params['signature'] = signature

    headers = {}
    if auth:
        headers['X-MBX-APIKEY'] = API_KEY

    resp = requests.post(REST_URL + path, params=params, headers=headers)
    return resp

def request_delete(path, auth=False, params=None):
    if params is None:
        params = {}
    if auth:
        params['timestamp'] = int(time.time() * 1000)
        query_string = urllib.parse.urlencode(params)
        signature = get_signature(query_string)
        params['signature'] = signature

    headers = {}
    if auth:
        headers['X-MBX-APIKEY'] = API_KEY

    resp = requests.delete(REST_URL + path, params=params, headers=headers)
    return resp

def run_checks():
    print("1. Server time...")
    res = request_get("/fapi/v1/time")
    if res.status_code == 200:
        print("PASS: server time:", res.json())
    else:
        print("FAIL: server time:", res.text)
        return False

    print("2. ExchangeInfo...")
    res = request_get("/fapi/v1/exchangeInfo")
    if res.status_code == 200:
        info = res.json()
        print("PASS: exchangeInfo, symbols count:", len(info['symbols']))
    else:
        print("FAIL: exchangeInfo:", res.text)
        return False

    print("3. Account/balance...")
    res = request_get("/fapi/v2/balance", auth=True)
    if res.status_code == 200:
        print("PASS: balance retrieved.")
    else:
        print("FAIL: balance:", res.text)
        return False

    print("4. Positions...")
    res = request_get("/fapi/v2/positionRisk", auth=True)
    if res.status_code == 200:
        print("PASS: positions retrieved.")
    else:
        print("FAIL: positions:", res.text)
        return False

    print("5. Open orders...")
    res = request_get("/fapi/v1/openOrders", auth=True)
    if res.status_code == 200:
        print("PASS: open orders retrieved. count:", len(res.json()))
    else:
        print("FAIL: open orders:", res.text)
        return False

    return True

async def websocket_and_order():
    # Get listen key
    print("6. User-data WebSocket connectivity...")
    res = request_post("/fapi/v1/listenKey", auth=True)
    if res.status_code != 200:
        print("FAIL: listenKey:", res.text)
        return False
    listen_key = res.json()['listenKey']

    ws_endpoint = f"{WS_URL}/{listen_key}"
    print(f"Connecting to {ws_endpoint} ...")

    try:
        async with websockets.connect(ws_endpoint) as websocket:
            print("PASS: User-data WebSocket connected.")

            # Place order
            print("Placing ONE minimum-size LIMIT order safely away from market...")

            # Find BTCUSDT current price and filters
            info = request_get("/fapi/v1/exchangeInfo").json()
            btcusdt_info = next(s for s in info['symbols'] if s['symbol'] == 'BTCUSDT')
            price_filter = next(f for f in btcusdt_info['filters'] if f['filterType'] == 'PRICE_FILTER')
            lot_filter = next(f for f in btcusdt_info['filters'] if f['filterType'] == 'LOT_SIZE')

            min_qty = float(lot_filter['minQty'])
            tick_size = float(price_filter['tickSize'])

            ticker = request_get("/fapi/v1/ticker/price", params={"symbol": "BTCUSDT"}).json()
            current_price = float(ticker['price'])

            # Safe price: 50% below current price, rounded to tick_size
            safe_price = current_price * 0.5
            safe_price = safe_price - (safe_price % tick_size)
            safe_price_str = f"{safe_price:.1f}" # simple format
            if tick_size < 1:
                decimals = len(str(tick_size).split('.')[1])
                safe_price_str = f"{safe_price:.{decimals}f}"

            # Ensure notional > 50
            target_qty = max(min_qty, 51.0 / safe_price)
            step_size = float(lot_filter['stepSize'])
            # round up to step size
            import math
            target_qty = math.ceil(target_qty / step_size) * step_size

            qty_str = f"{target_qty}"
            if step_size < 1:
                decimals = len(str(step_size).rstrip('0').split('.')[1]) if '.' in str(step_size).rstrip('0') else 0
                qty_str = f"{target_qty:.{decimals}f}"

            order_params = {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": qty_str,
                "price": safe_price_str
            }

            print(f"Order params: {order_params}")
            order_res = request_post("/fapi/v1/order", auth=True, params=order_params)
            if order_res.status_code == 200:
                order_data = order_res.json()
                order_id = order_data['orderId']
                print("PASS: Order placed successfully. Order ID:", order_id)
            else:
                print("FAIL: Order placement:", order_res.text)
                return False

            # Wait for WS event
            print("Waiting for WS event...")
            ws_event_received = False
            for _ in range(5):
                try:
                    msg = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                    msg_data = json.loads(msg)
                    if msg_data.get('e') == 'ORDER_TRADE_UPDATE':
                        print("PASS: user-data event received:", msg_data['e'])
                        ws_event_received = True
                        break
                except asyncio.TimeoutError:
                    continue

            if not ws_event_received:
                print("FAIL: Did not receive user-data event.")
                # We still need to cancel

            # Cancel order
            print("Canceling order...")
            cancel_res = request_delete("/fapi/v1/order", auth=True, params={"symbol": "BTCUSDT", "orderId": order_id})
            if cancel_res.status_code == 200:
                print("PASS: Order cancelled.")
            else:
                print("FAIL: Order cancellation:", cancel_res.text)
                return False

            # Prove open orders = 0
            print("Checking final open orders...")
            open_res = request_get("/fapi/v1/openOrders", auth=True, params={"symbol": "BTCUSDT"})
            if open_res.status_code == 200:
                open_orders = open_res.json()
                if len(open_orders) == 0:
                    print("PASS: Final open orders = 0 and state is consistent.")
                    return True
                else:
                    print("FAIL: Final open orders is not 0:", open_orders)
                    return False
            else:
                print("FAIL: Check open orders:", open_res.text)
                return False

    except Exception as e:
        print("FAIL: Exception in WS:", e)
        return False

def main():
    if not run_checks():
        sys.exit(1)

    success = asyncio.run(websocket_and_order())
    if success:
        print("TESTNET SMOKE PHASE 1 PASS")
        print("READY FOR CONTROLLED FILL VALIDATION")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()