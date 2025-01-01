#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import ccxt
import time
import pandas as pd
import logging
import argparse
import subprocess
import sys
import threading
import random
import math

logging.basicConfig(filename='bot.log', level=logging.INFO, format='%(asctime)s - %(message)s')

API_KEY = 'YOUR API KEY HERE'
API_SECRET = 'YOUR API SECRET KEY HERE'
REFRESH_INTERVAL = 60
MAX_POSITION_SIZE = 0.1
STOP_LOSS_PERCENT = 2.0
MIN_TRADE_AMOUNT = 3  # Montant minimum de trade mis à jour
STATE_FILE = 'bot_state.txt'
LOG_CLEAR_INTERVAL = 400  # Intervalle pour nettoyer les logs (en secondes)
MAX_FAILED_TRADES = 2  # Nombre maximum d'échecs consécutifs avant de changer de paire

# Initial RSI thresholds
RSI_BUY_THRESHOLD = 40
RSI_SELL_THRESHOLD = 60

def read_bot_state():
    try:
        with open(STATE_FILE, 'r') as file:
            return file.read() == 'True'
    except FileNotFoundError:
        return False

def write_bot_state(state):
    with open(STATE_FILE, 'w') as file:
        file.write(str(state))

def print_logo():
    commands = """
Available Commands:
    --start         Start the trading bot
    --monitor       Monitor the bot activities
    --monitorlive   Monitor the bot activities in real-time
    --status        Check if the bot is active
    --stop          Stop the trading bot
    --clearlog      Clear the bot log file
    """
    print(commands)
    print("AVHIRAL TE@M - BOT COINBASE PRIVATE V1.7+")
    print("BOT COINBASE ACTIF EN ARRIER PLAN")

class TradingBot:
    def __init__(self):
        print_logo()
        self.exchange = self.initialize_exchange()
        self.crypto_pairs = []
        self.balances = {}
        self.selected_pair = None
        self.failed_trades = 0  # Compteur d'échecs de trades
        self.total_gain = 0  # Gain total
        self.rsi_buy_threshold = RSI_BUY_THRESHOLD
        self.rsi_sell_threshold = RSI_SELL_THRESHOLD
        write_bot_state(True)
        logging.info("Bot Coinbase Started")

    def initialize_exchange(self):
        try:
            exchange = ccxt.coinbase({
                'apiKey': API_KEY,
                'secret': API_SECRET
            })
            exchange.load_markets()
            logging.info("Exchange initialized successfully.")
            return exchange
        except Exception as e:
            logging.error(f"Failed to initialize exchange: {str(e)}")
            return None

    def fetch_crypto_pairs(self):
        """
        Récupère les paires de crypto-monnaies actives avec un solde positif.
        """
        try:
            balance = self.exchange.fetch_balance()
            self.balances = {currency: bal for currency, bal in balance['total'].items() if bal > 0}
            logging.info(f"Balances: {self.balances}")

            markets = self.exchange.load_markets()
            self.crypto_pairs = [pair for pair, market in markets.items() if market['active'] and self.balances.get(market['base'], 0) > 0]
            logging.info(f"Active crypto pairs with positive balance: {self.crypto_pairs}")
        except Exception as e:
            logging.error(f"Failed to fetch crypto pairs: {str(e)}")
            self.crypto_pairs = []  # Vide la liste pour éviter des erreurs en aval

    def evaluate_pair(self, pair):
        try:
            # Validation des données
            ohlcv_1h = self.exchange.fetch_ohlcv(pair, '1h')
            ohlcv_1d = self.exchange.fetch_ohlcv(pair, '1d')
            df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_1d = pd.DataFrame(ohlcv_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            if not self.validate_data_length(pair, df_1h, df_1d):
                return float('-inf')

            df_1h['MA30'] = df_1h['close'].rolling(window=30).mean()
            df_1h['change'] = df_1h['close'].diff()
            df_1h['gain'] = df_1h['change'].apply(lambda x: x if x > 0 else 0)
            df_1h['loss'] = df_1h['change'].apply(lambda x: -x if x < 0 else 0)
            df_1h['avg_gain'] = df_1h['gain'].rolling(window=14).mean()
            df_1h['avg_loss'] = df_1h['loss'].rolling(window=14).mean()
            df_1h['rs'] = df_1h['avg_gain'] / df_1h['avg_loss']
            df_1h['rsi'] = 100 - (100 / (1 + df_1h['rs']))

            last_price_1h = df_1h['close'].iloc[-1]
            ma30_1h = df_1h['MA30'].iloc[-1]
            rsi_1h = df_1h['rsi'].iloc[-1]

            score = 0
            if last_price_1h > ma30_1h and rsi_1h < self.rsi_buy_threshold:
                score += (self.rsi_buy_threshold - rsi_1h)  # Higher score for lower RSI below threshold
            elif last_price_1h < ma30_1h and rsi_1h > self.rsi_sell_threshold:
                score += (rsi_1h - self.rsi_sell_threshold)  # Higher score for higher RSI above threshold

            # Additional checks with other indicators
            df_1h['MA20'] = df_1h['close'].rolling(window=20).mean()
            df_1h['STDDEV'] = df_1h['close'].rolling(window=20).std()
            df_1h['UpperBand'] = df_1h['MA20'] + (df_1h['STDDEV'] * 2)
            df_1h['LowerBand'] = df_1h['MA20'] - (df_1h['STDDEV'] * 2)
            upper_band = df_1h['UpperBand'].iloc[-1]
            lower_band = df_1h['LowerBand'].iloc[-1]

            if last_price_1h < lower_band:
                score += 1  # Increase score if price is below lower Bollinger band
            elif last_price_1h > upper_band:
                score -= 1  # Decrease score if price is above upper Bollinger band

            df_1h['EMA12'] = df_1h['close'].ewm(span=12, adjust=False).mean()
            df_1h['EMA26'] = df_1h['close'].ewm(span=26, adjust=False).mean()
            df_1h['MACD'] = df_1h['EMA12'] - df_1h['EMA26']
            df_1h['Signal'] = df_1h['MACD'].ewm(span=9, adjust=False).mean()
            macd = df_1h['MACD'].iloc[-1]
            signal = df_1h['Signal'].iloc[-1]

            if macd > signal:
                score += 1  # Increase score if MACD is above signal line
            elif macd < signal:
                score -= 1  # Decrease score if MACD is below signal line

            logging.info(f"Evaluated pair {pair}: score={score}")
            return score

        except Exception as e:
            logging.error(f"Failed to evaluate pair {pair}: {str(e)}")
            return float('-inf')

    def select_best_pair(self):
        self.fetch_crypto_pairs()
        best_pair = None
        best_score = float('-inf')
    
        for pair in self.crypto_pairs:
            score = self.evaluate_pair(pair)
            if score > best_score:
                best_score = score
                best_pair = pair

        self.selected_pair = best_pair
        logging.info(f"Selected best pair: {self.selected_pair}")

    def validate_data_length(self, pair, df_1h, df_1d):
        """
        Vérifie si les données de 1h et 1d contiennent suffisamment de points pour les calculs.
        """
        if len(df_1h) < 30 or len(df_1d) < 30:  # Vérifie un minimum de 30 points pour les calculs
            logging.warning(f"Not enough data for {pair} to calculate indicators.")
            return False  # Indique que les données sont insuffisantes
        return True  # Indique que les données sont suffisantes

    def place_buy_order(self, pair, amount):
        """
        Place un ordre d'achat sur une paire donnée avec une gestion des exceptions.
        """
        try:
            order = self.exchange.create_market_buy_order(pair, amount)
            logging.info(f"Placed buy order for {amount} in pair {pair}.")
            return order
        except ccxt.NetworkError as e:
            logging.error(f"Network error during buy order: {str(e)}")
            self.failed_trades += 1
        except ccxt.ExchangeError as e:
            logging.error(f"Exchange error during buy order: {str(e)}")
            self.failed_trades += 1
        except Exception as e:
            logging.error(f"Unexpected error during buy order: {str(e)}")
            self.failed_trades += 1
        return None

    def calculate_trade_gain(self, order, amount, side):
        """
        Calcule le gain ou la perte d'un trade en fonction du type d'ordre (buy ou sell).
        """
        try:
            price = order.get('price', 0)
            symbol = order.get('symbol', 'unknown')
            trade_gain = (amount * price) if side == 'sell' else (-amount * price)
            self.total_gain += trade_gain
            logging.info(f"Trade gain for {symbol}: {trade_gain}")
            logging.info(f"Total gain updated: {self.total_gain}")
            return trade_gain
        except Exception as e:
            logging.error(f"Error calculating trade gain: {str(e)}")
            return 0

    def adjust_rsi_thresholds(self):
        if self.failed_trades > 0:
            self.rsi_buy_threshold = max(20, RSI_BUY_THRESHOLD - self.failed_trades * 2)
            self.rsi_sell_threshold = min(80, RSI_SELL_THRESHOLD + self.failed_trades * 2)
        else:
            self.rsi_buy_threshold = RSI_BUY_THRESHOLD
            self.rsi_sell_threshold = RSI_SELL_THRESHOLD

        logging.info(f"Adjusted RSI thresholds: Buy = {self.rsi_buy_threshold}, Sell = {self.rsi_sell_threshold}")

    def trade(self):
        """
        Exécute la logique de trading pour la paire sélectionnée.
        """
        if not self.selected_pair:
            logging.info("No pair selected for trading.")
            return

        pair = self.selected_pair
        try:
            # Récupération des données pour les périodes de 1h et 1d
            ohlcv_1d = self.exchange.fetch_ohlcv(pair, '1d')
            ohlcv_1h = self.exchange.fetch_ohlcv(pair, '1h')
            df_1d = pd.DataFrame(ohlcv_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            # Conversion des timestamps
            df_1d['timestamp'] = pd.to_datetime(df_1d['timestamp'], unit='ms')
            df_1h['timestamp'] = pd.to_datetime(df_1h['timestamp'], unit='ms')

            # Mise en index
            df_1d.set_index('timestamp', inplace=True)
            df_1h.set_index('timestamp', inplace=True)

            # Validation des données
            if not self.validate_data_length(pair, df_1h, df_1d):
                logging.warning(f"Skipping trade for {pair} due to insufficient data.")
                return

            # Calcul des indicateurs techniques
            df_1h['MA30'] = df_1h['close'].rolling(window=30).mean()
            df_1h['change'] = df_1h['close'].diff()
            df_1h['gain'] = df_1h['change'].apply(lambda x: x if x > 0 else 0)
            df_1h['loss'] = df_1h['change'].apply(lambda x: -x if x < 0 else 0)
            df_1h['avg_gain'] = df_1h['gain'].rolling(window=14).mean()
            df_1h['avg_loss'] = df_1h['loss'].rolling(window=14).mean()
            df_1h['rs'] = df_1h['avg_gain'] / df_1h['avg_loss']
            df_1h['rsi'] = 100 - (100 / (1 + df_1h['rs']))

            # Dernières valeurs des indicateurs
            last_price_1h = df_1h['close'].iloc[-1]
            ma30_1h = df_1h['MA30'].iloc[-1]
            rsi_1h = df_1h['rsi'].iloc[-1]

            logging.info(f"Last price for {pair} (1h): {last_price_1h}, MA30 (1h): {ma30_1h}, RSI (1h): {rsi_1h}")

            # Vérification des balances disponibles
            base_currency = pair.split('/')[0]
            available_balance = self.balances.get(base_currency, 0)

            # Ajustement dynamique des seuils RSI
            self.adjust_rsi_thresholds()

            # Ajustement de la taille de la position en fonction des gains totaux
            position_size_factor = min(max(math.log(max(self.total_gain + 1, 1)), 1), 10)
            adjusted_max_position_size = MAX_POSITION_SIZE * position_size_factor

            # Logique principale de trading
            if last_price_1h > ma30_1h and rsi_1h < self.rsi_buy_threshold and available_balance >= MIN_TRADE_AMOUNT:
                position_size = min(available_balance * adjusted_max_position_size, available_balance)
                order = self.place_order(pair, position_size, 'buy')
                if order:
                    self.calculate_trade_gain(order, position_size, 'buy')

            elif last_price_1h < ma30_1h and rsi_1h > self.rsi_sell_threshold and available_balance >= MIN_TRADE_AMOUNT:
                order = self.place_order(pair, available_balance, 'sell')
                if order:
                    self.calculate_trade_gain(order, available_balance, 'sell')

            else:
                # Logique additionnelle : Bandes de Bollinger
                df_1h['MA20'] = df_1h['close'].rolling(window=20).mean()
                df_1h['STDDEV'] = df_1h['close'].rolling(window=20).std()
                df_1h['UpperBand'] = df_1h['MA20'] + (df_1h['STDDEV'] * 2)
                df_1h['LowerBand'] = df_1h['MA20'] - (df_1h['STDDEV'] * 2)

                upper_band = df_1h['UpperBand'].iloc[-1]
                lower_band = df_1h['LowerBand'].iloc[-1]

                if last_price_1h < lower_band and available_balance >= MIN_TRADE_AMOUNT:
                    position_size = min(available_balance * adjusted_max_position_size, available_balance)
                    order = self.place_order(pair, position_size, 'buy')
                    if order:
                        self.calculate_trade_gain(order, position_size, 'buy')

                elif last_price_1h > upper_band and available_balance >= MIN_TRADE_AMOUNT:
                    order = self.place_order(pair, available_balance, 'sell')
                    if order:
                        self.calculate_trade_gain(order, available_balance, 'sell')

                else:
                    # Logique additionnelle : MACD
                    df_1h['EMA12'] = df_1h['close'].ewm(span=12, adjust=False).mean()
                    df_1h['EMA26'] = df_1h['close'].ewm(span=26, adjust=False).mean()
                    df_1h['MACD'] = df_1h['EMA12'] - df_1h['EMA26']
                    df_1h['Signal'] = df_1h['MACD'].ewm(span=9, adjust=False).mean()

                    macd = df_1h['MACD'].iloc[-1]
                    signal = df_1h['Signal'].iloc[-1]

                    if macd > signal and available_balance >= MIN_TRADE_AMOUNT:
                        position_size = min(available_balance * adjusted_max_position_size, available_balance)
                        order = self.place_order(pair, position_size, 'buy')
                        if order:
                            self.calculate_trade_gain(order, position_size, 'buy')

                    elif macd < signal and available_balance >= MIN_TRADE_AMOUNT:
                        order = self.place_order(pair, available_balance, 'sell')
                        if order:
                            self.calculate_trade_gain(order, available_balance, 'sell')

                    else:
                        logging.info(f"Skipping trade for {pair} as conditions not met.")
                        self.failed_trades += 1  # Incrémentation du compteur d'échecs
        except Exception as e:
            logging.error(f"Error in trading function for {pair}: {str(e)}")

    def place_order(self, pair, amount, side, retries=3):
        """
        Place un ordre de marché (buy ou sell) avec gestion des échecs et des tentatives.
        """
        attempt = 0
        while attempt < retries:
            try:
                base_currency = pair.split('/')[0]
                if side == 'buy':
                    order = self.exchange.create_market_buy_order(pair, amount)
                    logging.info(f"Buy order placed: {amount} {base_currency} in {pair}.")
                elif side == 'sell':
                    order = self.exchange.create_market_sell_order(pair, amount)
                    logging.info(f"Sell order placed: {amount} {base_currency} in {pair}.")
                else:
                    raise ValueError(f"Invalid side for order: {side}")
            
                self.fetch_crypto_pairs()  # Mise à jour des balances après le trade
                return order
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logging.warning(f"Attempt {attempt + 1}/{retries} failed for {side} order: {str(e)}")
                attempt += 1
                time.sleep(2)  # Pause avant de retenter
            except Exception as e:
                logging.error(f"Unexpected error during {side} order: {str(e)}")
                break
        logging.error(f"Failed to place {side} order for {pair} after {retries} attempts.")
        return None

def clear_log_periodically():
    """
    Efface périodiquement les logs du fichier `bot.log`.
    """
    while True:
        time.sleep(LOG_CLEAR_INTERVAL)
        try:
            with open('bot.log', 'w') as f:
                f.truncate(0)  # Nettoie le fichier
            logging.info("Bot logs cleared.")
        except Exception as e:
            logging.error(f"Failed to clear logs: {str(e)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Coinbase Trading Bot')
    parser.add_argument('--start', action='store_true', help='Start the trading bot')
    parser.add_argument('--monitor', action='store_true', help='Monitor the bot activities')
    parser.add_argument('--monitorlive', action='store_true', help='Monitor the bot activities in real-time')
    parser.add_argument('--status', action='store_true', help='Check if the bot is active')
    parser.add_argument('--stop', action='store_true', help='Stop the trading bot')
    parser.add_argument('--clearlog', action='store_true', help='Clear the bot log file')
    args = parser.parse_args()

    if args.start:
        pid = os.fork()
        if pid > 0:
            # Parent process
            print("Trading bot started in the background.")
            sys.exit()
        else:
            # Child process
            bot = TradingBot()
            log_clear_thread = threading.Thread(target=clear_log_periodically)
            log_clear_thread.daemon = True  # Ensure this thread does not block program exit
            log_clear_thread.start()
            while True:
                if not read_bot_state():
                    logging.info("Bot stopped manually.")
                    break
                bot.select_best_pair()
                bot.trade()
                time.sleep(REFRESH_INTERVAL)
    elif args.monitor:
        try:
            with open('bot.log', 'r') as f:
                print(f.read())
        except FileNotFoundError:
            print("Log file not found.")
    elif args.monitorlive:
        try:
            subprocess.call(['tail', '-f', 'bot.log'])
        except FileNotFoundError:
            print("Log file not found.")
    elif args.status:
        if read_bot_state():
            print("Bot is active.")
        else:
            print("Bot is not active.")
    elif args.stop:
        write_bot_state(False)
        logging.info("Bot stopping...")
        print("Bot stopped.")
    elif args.clearlog:
        with open('bot.log', 'w'):
            pass  # Clear the log file
        print("Bot logs cleared.")