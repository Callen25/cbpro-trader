#
# bitcoin-trade.py
# Mike Cardillo
#
# Main program for interacting with Coinbase Pro websocket and managing trade data

import cbpro
import period
import indicators
import engine
import yaml
import queue
import time
import interface
import logging
import datetime
from decimal import Decimal
from websocket import WebSocketConnectionClosedException

with open("config.yml", 'r') as ymlfile:
    config = yaml.load(ymlfile)
logger = logging.getLogger('trader-logger')
logger.setLevel(logging.DEBUG)
if config['logging']:
    logger.addHandler(logging.FileHandler("debug.log"))
if config['frontend'] == 'debug':
    logger.addHandler(logging.StreamHandler())
error_logger = logging.getLogger('error-logger')
error_logger.addHandler(logging.FileHandler("error.log"))

# Periods to update indicators for
indicator_period_list = []
# Periods to actively trade on (typically 1 per product)
trade_period_list = {}
# List of products that we are actually monitoring
product_list = set()
fiat_currency = config['fiat']

if config['sandbox']:
    api_url = "https://api-public.sandbox.pro.coinbase.com"
else:
    api_url = "https://api.pro.coinbase.com"

auth_client = cbpro.AuthenticatedClient(config['key'], config['secret'], config['passphrase'], api_url=api_url)

for cur_period in config['periods']:
    if cur_period.get('meta'):
        new_period = period.MetaPeriod(period_size=(60 * cur_period['length']), fiat=fiat_currency,
                                       product=cur_period['product'], name=cur_period['name'], cbpro_client=auth_client)
    else:
        new_period = period.Period(period_size=(60 * cur_period['length']),
                                   product=cur_period['product'], name=cur_period['name'], cbpro_client=auth_client)
    indicator_period_list.append(new_period)
    product_list.add(cur_period['product'])
    if cur_period['trade']:
        if trade_period_list.get(cur_period['product']) is None:
            trade_period_list[cur_period['product']] = []
        trade_period_list[cur_period['product']].append(new_period)

max_slippage = Decimal(str(config['max_slippage']))
trade_engine = engine.TradeEngine(auth_client, product_list=product_list, fiat=fiat_currency, is_live=config['live'], max_slippage=max_slippage)
cbpro_websocket = engine.TradeAndHeartbeatWebsocket(fiat=fiat_currency, sandbox=config['sandbox'])
cbpro_websocket.start()
indicator_period_list[0].verbose_heartbeat = True
indicator_subsys = indicators.IndicatorSubsystem(indicator_period_list)
last_indicator_update = time.time()

if config['frontend'] == 'curses':
    curses_enable = True
else:
    curses_enable = False
interface = interface.cursesDisplay(enable=curses_enable)
while(True):
    try:
        if cbpro_websocket.error:
            raise cbpro_websocket.error
        msg = cbpro_websocket.websocket_queue.get(timeout=15)
        for product in trade_engine.products:
            product.order_book.process_message(msg)
        if msg.get('type') == "match":
            for cur_period in indicator_period_list:
                cur_period.process_trade(msg)
            if time.time() - last_indicator_update >= 1.0:
                for cur_period in indicator_period_list:
                    indicator_subsys.recalculate_indicators(cur_period)
                for product_id, period_list in trade_period_list.items():
                    trade_engine.determine_trades(product_id, period_list, indicator_subsys.current_indicators)
                last_indicator_update = time.time()
        elif msg.get('type') == "heartbeat":
            for cur_period in indicator_period_list:
                cur_period.process_heartbeat(msg)
            for product_id, period_list in trade_period_list.items():
                if len(indicator_subsys.current_indicators[cur_period.name]) > 0:
                    trade_engine.determine_trades(product_id, period_list, indicator_subsys.current_indicators)
            trade_engine.print_amounts()
        interface.update(trade_engine, indicator_subsys.current_indicators,
                         indicator_period_list, msg)
    except KeyboardInterrupt:
        trade_engine.close(exit=True)
        cbpro_websocket.close()
        interface.close()
        break
    except Exception as e:
        error_logger.exception(datetime.datetime.now())
        trade_engine.close()
        cbpro_websocket.close()
        cbpro_websocket.error = None
        # Period data cannot be trusted. Re-initialize
        for cur_period in indicator_period_list:
            cur_period.initialize()
        time.sleep(10)
        cbpro_websocket.start()
