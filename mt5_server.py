import MetaTrader5 as mt5
from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import threading
import time
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

# Global state
mt5_connected = False
mt5_account_info = {}
last_trades = []
trade_callback = None  # Will be set by web app

@app.route('/api/mt5/connect', methods=['POST'])
def connect_mt5():
    global mt5_connected, mt5_account_info
    
    data = request.json
    login = data.get('login')
    password = data.get('password')
    server = data.get('server')
    
    if not login or not password or not server:
        return jsonify({'success': False, 'error': 'Missing credentials'}), 400
    
    # Initialize MT5
    if not mt5.initialize():
        return jsonify({'success': False, 'error': 'MT5 initialization failed'}), 500
    
    # Login
    authorized = mt5.login(
        login=int(login),
        password=password,
        server=server
    )
    
    if authorized:
        mt5_connected = True
        account_info = mt5.account_info()
        if account_info:
            mt5_account_info = {
                'login': account_info.login,
                'name': account_info.name,
                'server': account_info.server,
                'balance': account_info.balance,
                'equity': account_info.equity,
                'currency': account_info.currency
            }
        
        # Start monitoring trades
        start_trade_monitor()
        
        return jsonify({
            'success': True,
            'account': mt5_account_info
        })
    else:
        return jsonify({
            'success': False,
            'error': f'Login failed: {mt5.last_error()}'
        }), 401

@app.route('/api/mt5/disconnect', methods=['POST'])
def disconnect_mt5():
    global mt5_connected
    mt5_connected = False
    mt5.shutdown()
    return jsonify({'success': True, 'message': 'Disconnected'})

@app.route('/api/mt5/status', methods=['GET'])
def get_status():
    if mt5_connected:
        return jsonify({
            'connected': True,
            'account': mt5_account_info
        })
    return jsonify({'connected': False})

@app.route('/api/mt5/history', methods=['POST'])
def get_history():
    if not mt5_connected:
        return jsonify({'error': 'Not connected to MT5'}), 401
    
    data = request.json
    days = data.get('days', 7)
    
    # Get positions history
    from_date = datetime.now() - timedelta(days=days)
    to_date = datetime.now()
    
    # Get deals history
    deals = mt5.history_deals_get(from_date, to_date)
    
    if deals is None:
        return jsonify({'trades': [], 'error': 'No history found'})
    
    trades = []
    for deal in deals:
        # Only get closed trades
        if deal.type in [mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL]:
            # Find the position for this deal
            position = mt5.history_positions_get(deal.position_id)
            if position:
                pos = position[0]
                trade = {
                    'symbol': deal.symbol,
                    'direction': 'Long' if deal.type == mt5.DEAL_TYPE_BUY else 'Short',
                    'entry': pos.price_open,
                    'stop': pos.sl or 0,
                    'exit': deal.price,
                    'size': deal.volume,
                    'profit': deal.profit,
                    'time': deal.time,
                    'notes': f'Imported from MT5 history'
                }
                trades.append(trade)
    
    return jsonify({'trades': trades})

@app.route('/api/mt5/positions', methods=['GET'])
def get_positions():
    if not mt5_connected:
        return jsonify({'error': 'Not connected to MT5'}), 401
    
    positions = mt5.positions_get()
    if positions is None:
        return jsonify({'positions': []})
    
    pos_list = []
    for pos in positions:
        pos_list.append({
            'symbol': pos.symbol,
            'type': 'Long' if pos.type == mt5.POSITION_TYPE_BUY else 'Short',
            'volume': pos.volume,
            'price_open': pos.price_open,
            'sl': pos.sl,
            'tp': pos.tp,
            'price_current': pos.price_current,
            'profit': pos.profit
        })
    
    return jsonify({'positions': pos_list})

@app.route('/api/mt5/trade', methods=['POST'])
def place_trade():
    if not mt5_connected:
        return jsonify({'error': 'Not connected to MT5'}), 401
    
    data = request.json
    symbol = data.get('symbol')
    action = data.get('action')  # 'buy' or 'sell'
    volume = data.get('volume', 0.01)
    sl = data.get('sl', 0)
    tp = data.get('tp', 0)
    
    # Get symbol info
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        return jsonify({'error': f'Symbol {symbol} not found'}), 404
    
    # Prepare request
    request_data = {
        'action': mt5.TRADE_ACTION_DEAL,
        'symbol': symbol,
        'volume': volume,
        'deviation': 10,
        'magic': 123456,
        'comment': 'Web Trade',
        'type_time': mt5.ORDER_TIME_GTC,
        'type_filling': mt5.ORDER_FILLING_IOC,
    }
    
    if action == 'buy':
        request_data['type'] = mt5.ORDER_TYPE_BUY
        request_data['price'] = mt5.symbol_info_tick(symbol).ask
    else:
        request_data['type'] = mt5.ORDER_TYPE_SELL
        request_data['price'] = mt5.symbol_info_tick(symbol).bid
    
    if sl > 0:
        request_data['sl'] = sl
    if tp > 0:
        request_data['tp'] = tp
    
    # Send order
    result = mt5.order_send(request_data)
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return jsonify({'error': f'Order failed: {result.comment}'}), 400
    
    return jsonify({
        'success': True,
        'order': {
            'ticket': result.order,
            'price': result.price
        }
    })

def monitor_trades():
    """Monitor for new trades in the background"""
    global last_trades, trade_callback
    
    last_check = datetime.now() - timedelta(seconds=5)
    
    while mt5_connected:
        try:
            # Get new deals since last check
            deals = mt5.history_deals_get(last_check, datetime.now())
            
            if deals and len(deals) > 0:
                for deal in deals:
                    # Check if it's a new trade
                    if deal.type in [mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL]:
                        # Get position info
                        position = mt5.history_positions_get(deal.position_id)
                        if position and len(position) > 0:
                            pos = position[0]
                            trade_data = {
                                'symbol': deal.symbol,
                                'direction': 'Long' if deal.type == mt5.DEAL_TYPE_BUY else 'Short',
                                'entry': pos.price_open,
                                'stop': pos.sl or 0,
                                'exit': deal.price,
                                'size': deal.volume,
                                'profit': deal.profit,
                                'time': deal.time.isoformat(),
                                'notes': f'Live trade from MT5'
                            }
                            
                            # Send to web app callback if set
                            if trade_callback:
                                trade_callback(trade_data)
                            
                            last_trades.append(trade_data)
            
            last_check = datetime.now()
            time.sleep(2)  # Check every 2 seconds
            
        except Exception as e:
            print(f'Monitor error: {e}')
            time.sleep(5)

def start_trade_monitor():
    thread = threading.Thread(target=monitor_trades, daemon=True)
    thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
