"""
Trading Bot Autónomo - Mean Reversion Strategy (v2)
Calcula indicadores internamente, opera en Bybit automáticamente.
Diseñado para correr en Docker/Coolify con gunicorn.

Cambios principales respecto a v1:
- Cruce EMA usa velas cerradas (iloc[-2] vs iloc[-3])
- RSI en velas 4H en vez de diarias (más reactivo)
- Señal basada en sesgo EMA (no requiere cruce exacto)
- Circuit breaker con recuperación (pausa 24h)
- Thread safety con Lock
- Dashboard HTML con auto-refresh
- Configuración dinámica vía /config (protegida con auth)
- Cooldown entre trades por símbolo
- Cache de balance/posiciones para /health
- Riesgo por defecto 1.5%
- Autenticación por token en todos los endpoints sensibles
"""

import os
import json
import time
import logging
import secrets
import hashlib
import threading
import functools
import ccxt
import pandas as pd
import pandas_ta as ta
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, Response, redirect, make_response
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# AUTENTICACIÓN — Token para proteger dashboard y API
# ============================================================

# Si no se define BOT_AUTH_TOKEN, se genera uno aleatorio y se muestra en logs
AUTH_TOKEN = os.getenv("BOT_AUTH_TOKEN", "")
if not AUTH_TOKEN:
    AUTH_TOKEN = secrets.token_urlsafe(32)
    # Se imprime al inicio — el logger aún no existe, se muestra con print
    print(f"\n{'='*60}")
    print(f"  TOKEN DE ACCESO GENERADO AUTOMATICAMENTE:")
    print(f"  {AUTH_TOKEN}")
    print(f"  Guardalo y configuralo como BOT_AUTH_TOKEN para que persista.")
    print(f"{'='*60}\n")

# Cookie name y duración (7 días)
AUTH_COOKIE_NAME = "bot_session"
AUTH_COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 días en segundos

# Hash del token para comparación segura (evita timing attacks)
_TOKEN_HASH = hashlib.sha256(AUTH_TOKEN.encode()).hexdigest()


def _verify_token(token):
    """Comparar token de forma segura contra timing attacks."""
    if not token:
        return False
    return hashlib.sha256(token.encode()).hexdigest() == _TOKEN_HASH


def _is_authenticated():
    """
    Verificar si el request actual está autenticado.
    Acepta 3 métodos:
    1. Cookie 'bot_session' (seteada tras login)
    2. Query param '?token=xxx'
    3. Header 'Authorization: Bearer xxx'
    """
    # 1. Cookie
    cookie_token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if _verify_token(cookie_token):
        return True

    # 2. Query param
    query_token = request.args.get("token", "")
    if _verify_token(query_token):
        return True

    # 3. Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]
        if _verify_token(bearer_token):
            return True

    return False


def require_auth(f):
    """Decorador: redirige a /login si no está autenticado."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not _is_authenticated():
            # Si es API (JSON), devolver 401
            if request.path.startswith("/api/"):
                return jsonify({"error": "No autorizado. Usa header 'Authorization: Bearer <token>'"}), 401
            # Si es HTML, redirigir a login
            return redirect(f"/login?next={request.path}")
        return f(*args, **kwargs)
    return decorated


# Validación de rangos para config (seguridad — evitar valores peligrosos)
CONFIG_LIMITS = {
    "RISK_PERCENT":     (0.1, 5.0),
    "LEVERAGE":         (1, 20),
    "RSI_OVERSOLD":     (10, 45),
    "RSI_OVERBOUGHT":   (55, 90),
    "SL_ATR_MULT":      (0.5, 5.0),
    "TP_ATR_MULT":      (0.5, 15.0),
    "MAX_DRAWDOWN_PCT": (5.0, 50.0),
    "CHECK_INTERVAL_SEC": (60, 3600),
    "EMA_FAST":         (5, 50),
    "EMA_SLOW":         (20, 200),
    "RSI_PERIOD":       (5, 30),
    "ATR_PERIOD":       (5, 30),
    "COOLDOWN_SEC":     (60, 3600),
}


def _validate_config_value(key, value):
    """Validar que un valor de config esté dentro de rangos seguros."""
    if key in CONFIG_LIMITS:
        min_val, max_val = CONFIG_LIMITS[key]
        if value < min_val or value > max_val:
            return False, f"{key} debe estar entre {min_val} y {max_val}"
    return True, ""


# ============================================================
# CONFIGURACIÓN (dataclass con valores por defecto)
# ============================================================

CONFIG_FILE = "/app/data/config.json"


@dataclass
class Config:
    """Parámetros ajustables del bot. Se cargan de env vars y luego de config.json."""
    RISK_PERCENT: float = 1.5
    LEVERAGE: int = 3
    RSI_OVERSOLD: float = 35.0
    RSI_OVERBOUGHT: float = 65.0
    SL_ATR_MULT: float = 1.5
    TP_ATR_MULT: float = 3.75
    MAX_DRAWDOWN_PCT: float = 15.0
    CHECK_INTERVAL_SEC: int = 300       # 5 minutos — alineado con velas 1H
    EMA_FAST: int = 21
    EMA_SLOW: int = 50
    RSI_PERIOD: int = 14
    ATR_PERIOD: int = 14
    COOLDOWN_SEC: int = 300             # 5 minutos entre trades del mismo símbolo

    def load_from_env(self):
        """Sobreescribir con variables de entorno si existen."""
        for fld in self.__dataclass_fields__:
            env_val = os.getenv(f"BOT_{fld}")
            if env_val is not None:
                tipo = type(getattr(self, fld))
                try:
                    setattr(self, fld, tipo(env_val))
                except ValueError:
                    pass

    def load_from_file(self):
        """Sobreescribir con config.json si existe."""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                for k, v in data.items():
                    if hasattr(self, k):
                        tipo = type(getattr(self, k))
                        setattr(self, k, tipo(v))
        except Exception:
            pass

    def save_to_file(self):
        """Persistir config actual a disco."""
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(asdict(self), f, indent=2)
        except Exception:
            pass


# Instancia global de configuración
cfg = Config()
cfg.load_from_env()
cfg.load_from_file()

# ============================================================
# CREDENCIALES Y CONSTANTES NO CONFIGURABLES
# ============================================================

API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]

# ============================================================
# LOGGING
# ============================================================

os.makedirs("/app/data", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/app/data/trades.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# ESTADO COMPARTIDO (protegido con Lock)
# ============================================================

STATE_FILE = "/app/data/state.json"
state_lock = threading.Lock()


def _default_state():
    return {
        "equity_peak": 0,
        "trades": [],
        "paused_until": None,
        # Cache para /health — evita llamar a Bybit
        "cached_balance": 0.0,
        "cached_positions": {},
        "cache_ts": None,
        # Últimos indicadores por símbolo (para dashboard)
        "last_indicators": {},
        # Timestamp del último chequeo completo
        "last_check_ts": None,
        # Estado del bot
        "bot_running": False,
    }


def load_state():
    default = _default_state()
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)
            # Mezclar: valores guardados sobre los defaults
            default.update(saved)
    except Exception as e:
        logger.warning(f"Error cargando estado: {e}")
    return default


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(st, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error guardando estado: {e}")


state = load_state()

# Cooldown por símbolo: {symbol: datetime_hasta_cuando}
cooldowns: dict = {}

# ============================================================
# EXCHANGE
# ============================================================


def create_exchange():
    ex = ccxt.bybit({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "options": {"defaultType": "linear"},
    })
    if TESTNET:
        ex.set_sandbox_mode(True)
    return ex


exchange = create_exchange()

# ============================================================
# VALIDACIÓN DE INICIO
# ============================================================


def validate_startup():
    """Verificar que las API keys funcionan al iniciar."""
    try:
        bal = exchange.fetch_balance({"type": "swap"})
        total = float(bal.get("USDT", {}).get("total", 0))
        logger.info(f"Validación OK — Balance: ${total:.2f} USDT")
        return True
    except ccxt.AuthenticationError as e:
        logger.error(f"ERROR DE AUTENTICACIÓN: API keys inválidas — {e}")
        return False
    except Exception as e:
        logger.error(f"Error validando conexión: {e}")
        return False

# ============================================================
# DATOS DE MERCADO
# ============================================================


def fetch_ohlcv(symbol, timeframe, limit):
    """Obtener velas OHLCV del exchange."""
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        logger.error(f"Error obteniendo datos {symbol} {timeframe}: {e}")
        return None


def calculate_indicators(symbol):
    """
    Calcular indicadores:
    - RSI en velas 4H (más reactivo que diario)
    - EMA fast/slow en velas 1H (sesgo estructural + cruce reciente)
    - ATR en velas 1H (sizing de SL/TP)

    Cruce EMA: se evalúa sobre velas CERRADAS (iloc[-2] vs iloc[-3]).
    iloc[-1] se usa solo para precio y ATR actuales.
    """
    # --- Velas 4H para RSI ---
    df_4h = fetch_ohlcv(symbol, "4h", 60)
    if df_4h is None or len(df_4h) < cfg.RSI_PERIOD + 1:
        return None

    # --- Velas 1H para EMAs y ATR ---
    df_1h = fetch_ohlcv(symbol, "1h", 100)
    if df_1h is None or len(df_1h) < cfg.EMA_SLOW + 1:
        return None

    # RSI 4H
    df_4h["rsi"] = ta.rsi(df_4h["close"], length=cfg.RSI_PERIOD)

    # EMAs 1H
    df_1h["ema_fast"] = ta.ema(df_1h["close"], length=cfg.EMA_FAST)
    df_1h["ema_slow"] = ta.ema(df_1h["close"], length=cfg.EMA_SLOW)

    # ATR 1H
    df_1h["atr"] = ta.atr(
        df_1h["high"], df_1h["low"], df_1h["close"], length=cfg.ATR_PERIOD
    )

    # --- Vela actual (sin cerrar) para precio y ATR ---
    current = df_1h.iloc[-1]

    # --- Velas CERRADAS para detección de cruce EMA ---
    closed_last = df_1h.iloc[-2]      # última vela cerrada
    closed_prev = df_1h.iloc[-3]      # penúltima vela cerrada

    # Sesgo EMA: ¿fast está por encima o debajo de slow? (vela cerrada)
    ema_bullish_bias = closed_last["ema_fast"] > closed_last["ema_slow"]
    ema_bearish_bias = closed_last["ema_fast"] < closed_last["ema_slow"]

    # Cruce reciente: ¿hubo cruce en las últimas 3 velas cerradas?
    recent_bullish_cross = False
    recent_bearish_cross = False
    for i in range(-2, -5, -1):  # -2, -3, -4 (últimas 3 cerradas)
        if abs(i) >= len(df_1h):
            break
        c = df_1h.iloc[i]
        p = df_1h.iloc[i - 1] if abs(i - 1) < len(df_1h) else None
        if p is not None:
            if p["ema_fast"] <= p["ema_slow"] and c["ema_fast"] > c["ema_slow"]:
                recent_bullish_cross = True
            if p["ema_fast"] >= p["ema_slow"] and c["ema_fast"] < c["ema_slow"]:
                recent_bearish_cross = True

    return {
        "rsi_4h": df_4h["rsi"].iloc[-1],
        "ema_fast": closed_last["ema_fast"],
        "ema_slow": closed_last["ema_slow"],
        "ema_bullish_bias": ema_bullish_bias,
        "ema_bearish_bias": ema_bearish_bias,
        "recent_bullish_cross": recent_bullish_cross,
        "recent_bearish_cross": recent_bearish_cross,
        "atr": current["atr"],
        "price": current["close"],
        "timestamp": str(current["timestamp"]),
    }

# ============================================================
# TRADING
# ============================================================


def get_balance():
    """Obtener balance total USDT."""
    try:
        bal = exchange.fetch_balance({"type": "swap"})
        return float(bal.get("USDT", {}).get("total", 0))
    except Exception as e:
        logger.error(f"Error obteniendo balance: {e}")
        return 0.0


def get_positions_all():
    """Obtener posiciones abiertas de todos los símbolos."""
    result = {}
    for sym in SYMBOLS:
        try:
            positions = exchange.fetch_positions([sym])
            for pos in positions:
                size = float(pos.get("contracts", 0))
                if size > 0:
                    result[sym] = {
                        "side": pos["side"],
                        "size": size,
                        "notional": float(pos.get("notional", 0)),
                        "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                        "entry_price": float(pos.get("entryPrice", 0)),
                    }
        except Exception as e:
            logger.error(f"Error obteniendo posición {sym}: {e}")
    return result


def get_position(symbol):
    """Obtener posición abierta de un símbolo."""
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            size = float(pos.get("contracts", 0))
            if size > 0:
                return {
                    "side": pos["side"],
                    "size": size,
                    "notional": float(pos.get("notional", 0)),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                    "entry_price": float(pos.get("entryPrice", 0)),
                }
        return None
    except Exception as e:
        logger.error(f"Error obteniendo posición {symbol}: {e}")
        return None


def set_leverage(symbol):
    """Configurar leverage para un símbolo."""
    try:
        exchange.set_leverage(cfg.LEVERAGE, symbol)
    except Exception:
        pass  # Ya estaba configurado


def is_in_cooldown(symbol):
    """Verificar si el símbolo está en cooldown tras un trade reciente."""
    now = datetime.now(timezone.utc)
    if symbol in cooldowns:
        if now < cooldowns[symbol]:
            remaining = (cooldowns[symbol] - now).total_seconds()
            logger.info(f"{symbol}: en cooldown — {remaining:.0f}s restantes")
            return True
        else:
            del cooldowns[symbol]
    return False


def set_cooldown(symbol):
    """Activar cooldown para un símbolo tras ejecutar un trade."""
    cooldowns[symbol] = datetime.now(timezone.utc) + timedelta(seconds=cfg.COOLDOWN_SEC)


def execute_trade(symbol, side, price, atr_value):
    """
    Ejecutar trade con SL y TP.
    - price: precio de la vela para calcular SL/TP
    - Después de ejecutar, se loguea el fill price real de la orden
    """
    balance = get_balance()
    if balance <= 0:
        logger.warning("Balance insuficiente")
        return None

    sl_distance = cfg.SL_ATR_MULT * atr_value
    tp_distance = cfg.TP_ATR_MULT * atr_value

    # Position sizing basado en riesgo
    risk_amount = balance * (cfg.RISK_PERCENT / 100)
    qty = risk_amount / sl_distance

    # Redondear al mínimo del mercado
    market = exchange.market(symbol)
    min_qty = market.get("limits", {}).get("amount", {}).get("min", 0)

    qty = float(exchange.amount_to_precision(symbol, qty))
    if qty < min_qty:
        logger.warning(f"{symbol}: qty {qty} < min {min_qty}")
        return None

    # Calcular SL y TP basados en precio de vela
    if side == "buy":
        sl_price = price - sl_distance
        tp_price = price + tp_distance
    else:
        sl_price = price + sl_distance
        tp_price = price - tp_distance

    sl_price = float(exchange.price_to_precision(symbol, sl_price))
    tp_price = float(exchange.price_to_precision(symbol, tp_price))

    set_leverage(symbol)

    try:
        order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=qty,
            params={
                "stopLoss": {"triggerPrice": sl_price, "type": "market"},
                "takeProfit": {"triggerPrice": tp_price, "type": "market"},
            }
        )

        # Precio real de fill (si el exchange lo devuelve)
        fill_price = float(order.get("average", 0) or order.get("price", 0) or price)

        trade_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": round(fill_price, 6),
            "candle_price": round(price, 6),
            "sl": sl_price,
            "tp": tp_price,
            "risk_usd": round(risk_amount, 2),
            "potential_usd": round(risk_amount * (cfg.TP_ATR_MULT / cfg.SL_ATR_MULT), 2),
            "balance": round(balance, 2),
            "order_id": order.get("id", "N/A"),
            "result": "abierto",
        }

        # Activar cooldown
        set_cooldown(symbol)

        # Guardar en estado (protegido con lock)
        with state_lock:
            state["trades"].append(trade_record)
            save_state(state)

        logger.info(
            f"TRADE {side.upper()} {symbol} | qty={qty} | "
            f"fill={fill_price:.2f} | SL={sl_price} | TP={tp_price} | "
            f"riesgo=${risk_amount:.2f}"
        )
        return trade_record

    except Exception as e:
        logger.error(f"Error ejecutando {side} {symbol}: {e}")
        return None

# ============================================================
# CIRCUIT BREAKER CON RECUPERACIÓN
# ============================================================


def check_circuit_breaker(balance):
    """
    Verificar drawdown. Si supera el máximo:
    - Cerrar todas las posiciones
    - Pausar 24 horas (paused_until)
    Después de la pausa, se resetea equity_peak al balance actual.
    Retorna True si el bot debe estar pausado.
    """
    now = datetime.now(timezone.utc)

    with state_lock:
        # ¿Estamos en pausa?
        if state.get("paused_until"):
            paused_until = datetime.fromisoformat(state["paused_until"])
            if now < paused_until:
                remaining = (paused_until - now).total_seconds() / 3600
                logger.warning(
                    f"Circuit breaker ACTIVO — reanuda en {remaining:.1f}h"
                )
                return True
            else:
                # Pausa terminó — resetear equity peak al balance actual
                logger.info(
                    f"Circuit breaker EXPIRADO — reseteando equity peak a ${balance:.2f}"
                )
                state["equity_peak"] = balance
                state["paused_until"] = None
                save_state(state)
                return False

    if balance <= 0:
        return True

    with state_lock:
        if state["equity_peak"] == 0:
            state["equity_peak"] = balance

        state["equity_peak"] = max(state["equity_peak"], balance)
        drawdown = (state["equity_peak"] - balance) / state["equity_peak"] * 100

    if drawdown >= cfg.MAX_DRAWDOWN_PCT:
        logger.warning(
            f"CIRCUIT BREAKER ACTIVADO: drawdown {drawdown:.1f}% >= {cfg.MAX_DRAWDOWN_PCT}%"
        )
        # Cerrar todas las posiciones
        for sym in SYMBOLS:
            pos = get_position(sym)
            if pos:
                close_side = "sell" if pos["side"] == "long" else "buy"
                try:
                    exchange.create_order(
                        sym, "market", close_side, pos["size"],
                        params={"reduceOnly": True}
                    )
                    logger.info(f"Cerrada posición {sym}")
                except Exception as e:
                    logger.error(f"Error cerrando {sym}: {e}")

        # Pausar 24 horas
        pause_end = now + timedelta(hours=24)
        with state_lock:
            state["paused_until"] = pause_end.isoformat()
            save_state(state)

        logger.info(f"Bot pausado hasta {pause_end.isoformat()}")
        return True

    return False

# ============================================================
# LOOP PRINCIPAL
# ============================================================


def update_cache(balance, positions):
    """Actualizar cache de balance y posiciones en el estado."""
    with state_lock:
        state["cached_balance"] = round(balance, 2)
        state["cached_positions"] = positions
        state["cache_ts"] = datetime.now(timezone.utc).isoformat()


def check_signals():
    """Evaluar señales para todos los símbolos."""
    logger.info("Evaluando señales...")

    # Obtener balance y posiciones (actualizar cache)
    balance = get_balance()
    positions = get_positions_all()
    update_cache(balance, positions)

    # Circuit breaker
    if check_circuit_breaker(balance):
        logger.warning("Circuit breaker activo — sin operaciones")
        with state_lock:
            state["last_check_ts"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
        return

    for symbol in SYMBOLS:
        try:
            # ¿Ya hay posición abierta?
            if symbol in positions:
                pos = positions[symbol]
                logger.info(
                    f"{symbol}: posición {pos['side']} abierta | "
                    f"PnL: ${pos['unrealized_pnl']:.2f}"
                )
                continue

            # ¿Está en cooldown?
            if is_in_cooldown(symbol):
                continue

            # Calcular indicadores
            indicators = calculate_indicators(symbol)
            if indicators is None:
                logger.warning(f"{symbol}: no se pudieron calcular indicadores")
                continue

            rsi = indicators["rsi_4h"]
            atr = indicators["atr"]
            price = indicators["price"]

            # Guardar indicadores en estado para el dashboard
            with state_lock:
                state["last_indicators"][symbol] = {
                    "rsi_4h": round(rsi, 2),
                    "ema_fast": round(indicators["ema_fast"], 2),
                    "ema_slow": round(indicators["ema_slow"], 2),
                    "atr": round(atr, 4),
                    "price": round(price, 2),
                    "ema_bullish_bias": indicators["ema_bullish_bias"],
                    "ema_bearish_bias": indicators["ema_bearish_bias"],
                    "recent_bullish_cross": indicators["recent_bullish_cross"],
                    "recent_bearish_cross": indicators["recent_bearish_cross"],
                    "timestamp": indicators["timestamp"],
                }

            bias_str = "ALCISTA" if indicators["ema_bullish_bias"] else "BAJISTA" if indicators["ema_bearish_bias"] else "NEUTRO"
            logger.info(
                f"{symbol}: RSI_4H={rsi:.1f} | "
                f"EMA {bias_str} | "
                f"ATR={atr:.2f} | Precio={price:.2f}"
            )

            # --- SEÑAL LONG ---
            # RSI sobreventa + sesgo EMA alcista
            if rsi < cfg.RSI_OVERSOLD and indicators["ema_bullish_bias"]:
                cross_msg = ""
                if indicators["recent_bullish_cross"]:
                    cross_msg = " + CRUCE ALCISTA RECIENTE (señal fuerte)"
                logger.info(
                    f"SEÑAL LONG {symbol}: RSI={rsi:.1f} + sesgo alcista{cross_msg}"
                )
                execute_trade(symbol, "buy", price, atr)

            # --- SEÑAL SHORT ---
            # RSI sobrecompra + sesgo EMA bajista
            elif rsi > cfg.RSI_OVERBOUGHT and indicators["ema_bearish_bias"]:
                cross_msg = ""
                if indicators["recent_bearish_cross"]:
                    cross_msg = " + CRUCE BAJISTA RECIENTE (señal fuerte)"
                logger.info(
                    f"SEÑAL SHORT {symbol}: RSI={rsi:.1f} + sesgo bajista{cross_msg}"
                )
                execute_trade(symbol, "sell", price, atr)

            else:
                logger.info(f"{symbol}: sin señal")

        except Exception as e:
            logger.error(f"Error procesando {symbol}: {e}")

    with state_lock:
        state["last_check_ts"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

# ============================================================
# FLASK APP — DASHBOARD Y API
# ============================================================

app = Flask(__name__)


# ---------- DASHBOARD HTML ----------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>Trading Bot Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0d1117; color: #c9d1d9; font-family: 'Courier New', monospace;
    padding: 20px; font-size: 14px;
  }
  h1 { color: #58a6ff; margin-bottom: 5px; font-size: 22px; }
  h2 { color: #8b949e; margin: 20px 0 10px 0; font-size: 16px; border-bottom: 1px solid #30363d; padding-bottom: 5px; }
  .header { display: flex; align-items: center; gap: 15px; margin-bottom: 15px; flex-wrap: wrap; }
  .badge {
    display: inline-block; padding: 3px 10px; border-radius: 12px;
    font-size: 12px; font-weight: bold;
  }
  .badge-running { background: #238636; color: #fff; }
  .badge-paused { background: #da3633; color: #fff; }
  .badge-testnet { background: #d29922; color: #000; }
  .badge-mainnet { background: #da3633; color: #fff; }
  .stats { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 15px; }
  .stat-box {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 12px 18px; min-width: 160px;
  }
  .stat-label { color: #8b949e; font-size: 11px; text-transform: uppercase; }
  .stat-value { color: #f0f6fc; font-size: 20px; margin-top: 4px; }
  .stat-value.green { color: #3fb950; }
  .stat-value.red { color: #f85149; }
  .drawdown-bar {
    width: 100%; height: 8px; background: #21262d; border-radius: 4px;
    margin-top: 6px; overflow: hidden;
  }
  .drawdown-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }
  table {
    width: 100%; border-collapse: collapse; margin-bottom: 15px;
    background: #161b22;
  }
  th {
    background: #21262d; color: #8b949e; padding: 8px 10px; text-align: left;
    font-size: 11px; text-transform: uppercase; border: 1px solid #30363d;
  }
  td {
    padding: 7px 10px; border: 1px solid #30363d; font-size: 13px;
  }
  tr:hover { background: #1c2128; }
  .pnl-pos { color: #3fb950; }
  .pnl-neg { color: #f85149; }
  .signal-label { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; }
  .signal-long { background: #238636; color: #fff; }
  .signal-short { background: #da3633; color: #fff; }
  .signal-none { background: #30363d; color: #8b949e; }
  .config-section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; margin-bottom: 15px; }
  .config-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px; }
  .config-item { display: flex; justify-content: space-between; padding: 4px 0; }
  .config-key { color: #8b949e; }
  .config-val { color: #58a6ff; }
  .footer { color: #484f58; font-size: 11px; margin-top: 20px; text-align: center; }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
  <div class="header">
    <h1>Trading Bot</h1>
    <span class="badge {status_class}">{status_text}</span>
    <span class="badge {net_class}">{net_text}</span>
  </div>

  <div class="stats">
    <div class="stat-box">
      <div class="stat-label">Balance USDT</div>
      <div class="stat-value">${balance}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Equity Peak</div>
      <div class="stat-value">${equity_peak}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Drawdown</div>
      <div class="stat-value {dd_class}">{drawdown_pct}%</div>
      <div class="drawdown-bar">
        <div class="drawdown-fill" style="width:{drawdown_bar}%;background:{dd_color};"></div>
      </div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Total Trades</div>
      <div class="stat-value">{total_trades}</div>
    </div>
  </div>

  <h2>Posiciones Abiertas</h2>
  {positions_table}

  <h2>Indicadores Actuales</h2>
  {indicators_table}

  <h2>Ultimos 20 Trades</h2>
  {trades_table}

  <h2>Configuracion <a href="/config">[editar]</a></h2>
  <div class="config-section">
    <div class="config-grid">
      {config_items}
    </div>
  </div>

  <div class="footer">
    Ultimo chequeo: {last_check} &mdash; Auto-refresh cada 60s &mdash; <a href="/health">API /health</a> | <a href="/config">Configuracion</a> | <a href="/logout" style="color:#f85149;">Cerrar sesion</a>
  </div>
</body>
</html>"""


def render_dashboard():
    """Generar el HTML del dashboard con datos actuales del estado."""
    with state_lock:
        balance = state.get("cached_balance", 0)
        equity_peak = state.get("equity_peak", 0)
        positions = state.get("cached_positions", {})
        trades = state.get("trades", [])
        indicators = state.get("last_indicators", {})
        last_check = state.get("last_check_ts", "nunca")
        paused = state.get("paused_until") is not None
        bot_running = state.get("bot_running", False)

    # Estado
    if paused:
        status_class = "badge-paused"
        status_text = "PAUSADO (Circuit Breaker)"
    elif bot_running:
        status_class = "badge-running"
        status_text = "EJECUTANDO"
    else:
        status_class = "badge-paused"
        status_text = "INICIANDO"

    net_class = "badge-testnet" if TESTNET else "badge-mainnet"
    net_text = "TESTNET" if TESTNET else "MAINNET"

    # Drawdown
    dd_pct = 0
    if equity_peak > 0:
        dd_pct = (equity_peak - balance) / equity_peak * 100
    dd_class = "red" if dd_pct > 5 else "green"
    dd_color = "#f85149" if dd_pct > 10 else "#d29922" if dd_pct > 5 else "#3fb950"
    dd_bar = min(dd_pct / cfg.MAX_DRAWDOWN_PCT * 100, 100)

    # Tabla de posiciones
    if positions:
        rows = ""
        for sym, pos in positions.items():
            pnl_class = "pnl-pos" if pos.get("unrealized_pnl", 0) >= 0 else "pnl-neg"
            rows += f"""<tr>
                <td>{sym}</td>
                <td>{pos.get('side','')}</td>
                <td>{pos.get('size','')}</td>
                <td>{pos.get('entry_price','')}</td>
                <td class="{pnl_class}">${pos.get('unrealized_pnl', 0):.2f}</td>
            </tr>"""
        positions_table = f"""<table>
            <tr><th>Simbolo</th><th>Lado</th><th>Tamaño</th><th>Precio Entrada</th><th>PnL No Realizado</th></tr>
            {rows}
        </table>"""
    else:
        positions_table = "<p style='color:#484f58;'>Sin posiciones abiertas</p>"

    # Tabla de indicadores
    if indicators:
        rows = ""
        for sym, ind in indicators.items():
            # Determinar señal
            rsi = ind.get("rsi_4h", 50)
            bull_bias = ind.get("ema_bullish_bias", False)
            bear_bias = ind.get("ema_bearish_bias", False)
            if rsi < cfg.RSI_OVERSOLD and bull_bias:
                signal = '<span class="signal-label signal-long">LONG</span>'
            elif rsi > cfg.RSI_OVERBOUGHT and bear_bias:
                signal = '<span class="signal-label signal-short">SHORT</span>'
            else:
                signal = '<span class="signal-label signal-none">NEUTRO</span>'

            bias = "ALCISTA" if bull_bias else "BAJISTA" if bear_bias else "NEUTRO"
            cross = ""
            if ind.get("recent_bullish_cross"):
                cross = " (cruce reciente)"
            elif ind.get("recent_bearish_cross"):
                cross = " (cruce reciente)"

            rows += f"""<tr>
                <td>{sym}</td>
                <td>{ind.get('price','')}</td>
                <td>{ind.get('rsi_4h','')}</td>
                <td>{ind.get('ema_fast','')}</td>
                <td>{ind.get('ema_slow','')}</td>
                <td>{bias}{cross}</td>
                <td>{ind.get('atr','')}</td>
                <td>{signal}</td>
            </tr>"""
        indicators_table = f"""<table>
            <tr><th>Simbolo</th><th>Precio</th><th>RSI 4H</th><th>EMA Rapida</th><th>EMA Lenta</th><th>Sesgo EMA</th><th>ATR</th><th>Señal</th></tr>
            {rows}
        </table>"""
    else:
        indicators_table = "<p style='color:#484f58;'>Esperando primer chequeo...</p>"

    # Tabla de últimos 20 trades
    last_trades = trades[-20:][::-1]  # últimos 20, más reciente primero
    if last_trades:
        rows = ""
        for t in last_trades:
            rows += f"""<tr>
                <td>{t.get('timestamp','')[:19]}</td>
                <td>{t.get('symbol','')}</td>
                <td>{t.get('side','')}</td>
                <td>{t.get('entry_price', t.get('price',''))}</td>
                <td>{t.get('sl','')}</td>
                <td>{t.get('tp','')}</td>
                <td>${t.get('risk_usd','')}</td>
                <td>{t.get('result','')}</td>
            </tr>"""
        trades_table = f"""<table>
            <tr><th>Fecha</th><th>Simbolo</th><th>Lado</th><th>Entrada</th><th>SL</th><th>TP</th><th>Riesgo $</th><th>Resultado</th></tr>
            {rows}
        </table>"""
    else:
        trades_table = "<p style='color:#484f58;'>Sin trades registrados</p>"

    # Configuración actual
    config_items = ""
    for k, v in asdict(cfg).items():
        config_items += f'<div class="config-item"><span class="config-key">{k}</span><span class="config-val">{v}</span></div>'

    return (DASHBOARD_HTML
        .replace("{status_class}", status_class)
        .replace("{status_text}", status_text)
        .replace("{net_class}", net_class)
        .replace("{net_text}", net_text)
        .replace("{balance}", f"{balance:.2f}")
        .replace("{equity_peak}", f"{equity_peak:.2f}")
        .replace("{drawdown_pct}", f"{dd_pct:.1f}")
        .replace("{dd_class}", dd_class)
        .replace("{dd_color}", dd_color)
        .replace("{drawdown_bar}", f"{dd_bar:.0f}")
        .replace("{total_trades}", str(len(trades)))
        .replace("{positions_table}", positions_table)
        .replace("{indicators_table}", indicators_table)
        .replace("{trades_table}", trades_table)
        .replace("{config_items}", config_items)
        .replace("{last_check}", str(last_check))
    )


# ---------- CONFIG HTML ----------

CONFIG_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot - Configuracion</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #0d1117; color: #c9d1d9; font-family: 'Courier New', monospace;
    padding: 20px; font-size: 14px;
  }
  h1 { color: #58a6ff; margin-bottom: 20px; font-size: 22px; }
  .form-container {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 20px; max-width: 600px;
  }
  .form-group { margin-bottom: 12px; display: flex; align-items: center; gap: 10px; }
  label { color: #8b949e; width: 200px; font-size: 12px; text-transform: uppercase; }
  input[type="number"], input[type="text"] {
    background: #0d1117; border: 1px solid #30363d; color: #f0f6fc;
    padding: 6px 10px; border-radius: 4px; font-family: 'Courier New', monospace;
    font-size: 14px; width: 150px;
  }
  input:focus { border-color: #58a6ff; outline: none; }
  button {
    background: #238636; color: #fff; border: none; padding: 8px 20px;
    border-radius: 6px; cursor: pointer; font-family: 'Courier New', monospace;
    font-size: 14px; margin-top: 10px;
  }
  button:hover { background: #2ea043; }
  button.danger { background: #da3633; }
  button.danger:hover { background: #f85149; }
  .msg { padding: 10px; border-radius: 6px; margin-bottom: 15px; }
  .msg-ok { background: #0d2818; border: 1px solid #238636; color: #3fb950; }
  .msg-err { background: #2d1012; border: 1px solid #da3633; color: #f85149; }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .back { margin-bottom: 15px; display: inline-block; }
</style>
</head>
<body>
  <a class="back" href="/">&larr; Volver al Dashboard</a>
  <h1>Configuracion del Bot</h1>
  {message}
  <div class="form-container">
    <form method="POST" action="/config">
      {form_fields}
      <button type="submit">Guardar Configuracion</button>
    </form>
  </div>
  <br>
  <div class="form-container">
    <h2 style="color:#f85149;margin-bottom:10px;">Circuit Breaker</h2>
    <p style="color:#8b949e;margin-bottom:10px;">
      Estado: {cb_status}
    </p>
    <form method="POST" action="/reset-circuit-breaker">
      <button type="submit" class="danger">Resetear Circuit Breaker</button>
    </form>
  </div>
</body>
</html>"""


def render_config_page(message=""):
    """Generar HTML del formulario de configuración."""
    fields = ""
    for k, v in asdict(cfg).items():
        input_type = "number"
        step = "any" if isinstance(v, float) else "1"
        # Mostrar rango permitido como hint
        hint = ""
        if k in CONFIG_LIMITS:
            lo, hi = CONFIG_LIMITS[k]
            hint = f' <span style="color:#484f58;font-size:11px;">({lo} - {hi})</span>'
            min_attr = f' min="{lo}" max="{hi}"'
        else:
            min_attr = ""
        fields += f"""<div class="form-group">
            <label for="{k}">{k}{hint}</label>
            <input type="{input_type}" step="{step}" id="{k}" name="{k}" value="{v}"{min_attr}>
        </div>"""

    with state_lock:
        paused = state.get("paused_until")
    if paused:
        cb_status = f'<span style="color:#f85149;">ACTIVO — pausado hasta {paused}</span>'
    else:
        cb_status = '<span style="color:#3fb950;">INACTIVO</span>'

    return (CONFIG_HTML
        .replace("{message}", message)
        .replace("{form_fields}", fields)
        .replace("{cb_status}", cb_status)
    )


# ---------- LOGIN PAGE HTML ----------

def _render_login(error="", next_url="/"):
    """Generar HTML de login sin .format() para evitar conflictos con CSS."""
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot - Login</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: #0d1117; color: #c9d1d9; font-family: 'Courier New', monospace;
    display: flex; justify-content: center; align-items: center; min-height: 100vh;
  }}
  .login-box {{
    background: #161b22; border: 1px solid #30363d; border-radius: 12px;
    padding: 40px; width: 360px; text-align: center;
  }}
  h1 {{ color: #58a6ff; font-size: 20px; margin-bottom: 8px; }}
  p {{ color: #8b949e; font-size: 12px; margin-bottom: 24px; }}
  input[type="password"] {{
    background: #0d1117; border: 1px solid #30363d; color: #f0f6fc;
    padding: 10px 14px; border-radius: 6px; font-family: 'Courier New', monospace;
    font-size: 14px; width: 100%; margin-bottom: 16px;
  }}
  input:focus {{ border-color: #58a6ff; outline: none; }}
  button {{
    background: #238636; color: #fff; border: none; padding: 10px 24px;
    border-radius: 6px; cursor: pointer; font-family: 'Courier New', monospace;
    font-size: 14px; width: 100%;
  }}
  button:hover {{ background: #2ea043; }}
  .error {{ color: #f85149; font-size: 12px; margin-bottom: 12px; }}
</style>
</head>
<body>
  <div class="login-box">
    <h1>Trading Bot</h1>
    <p>Ingresa tu token de acceso</p>
    {error}
    <form method="POST" action="/login">
      <input type="hidden" name="next" value="{next_url}">
      <input type="password" name="token" placeholder="Token de acceso" autofocus required>
      <button type="submit">Entrar</button>
    </form>
  </div>
</body>
</html>"""


# ---------- RUTAS ----------

@app.route("/login", methods=["GET"])
def login_get():
    """Formulario de login."""
    next_url = request.args.get("next", "/")
    return Response(
        _render_login(error="", next_url=next_url),
        content_type="text/html; charset=utf-8"
    )


@app.route("/login", methods=["POST"])
def login_post():
    """Procesar login — verificar token y setear cookie."""
    token = request.form.get("token", "")
    next_url = request.form.get("next", "/")

    if _verify_token(token):
        resp = make_response(redirect(next_url))
        resp.set_cookie(
            AUTH_COOKIE_NAME,
            token,
            max_age=AUTH_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
            # secure=True se activa automáticamente si estás detrás de HTTPS (Coolify)
            secure=request.is_secure,
        )
        return resp
    else:
        return Response(
            _render_login(
                error='<p class="error">Token incorrecto</p>',
                next_url=next_url
            ),
            content_type="text/html; charset=utf-8",
            status=401
        )


@app.route("/logout", methods=["GET"])
def logout():
    """Cerrar sesión — borrar cookie."""
    resp = make_response(redirect("/login"))
    resp.delete_cookie(AUTH_COOKIE_NAME)
    return resp


@app.route("/", methods=["GET"])
@require_auth
def index():
    """Dashboard HTML principal."""
    return Response(render_dashboard(), content_type="text/html; charset=utf-8")


@app.route("/health", methods=["GET"])
def health():
    """
    Endpoint de salud — público (Docker healthcheck lo necesita).
    Solo muestra estado básico, SIN datos sensibles.
    Si está autenticado, muestra datos completos.
    """
    with state_lock:
        balance = state.get("cached_balance", 0)
        peak = state.get("equity_peak", balance)
        paused = state.get("paused_until")
        cache_ts = state.get("cache_ts")

    dd = ((peak - balance) / peak * 100) if peak > 0 else 0

    # Datos básicos (siempre visibles — para Docker healthcheck)
    data = {
        "status": "pausado" if paused else "ejecutando",
        "testnet": TESTNET,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Si está autenticado, agregar datos completos
    if _is_authenticated():
        with state_lock:
            positions = state.get("cached_positions", {})
            total_trades = len(state.get("trades", []))
            last_trades = state.get("trades", [])[-5:]

        data.update({
            "balance_usdt": round(balance, 2),
            "equity_peak": round(peak, 2),
            "drawdown_pct": round(dd, 1),
            "circuit_breaker": paused is not None,
            "paused_until": paused,
            "leverage": cfg.LEVERAGE,
            "risk_pct": cfg.RISK_PERCENT,
            "symbols": SYMBOLS,
            "open_positions": positions,
            "total_trades": total_trades,
            "last_5_trades": last_trades,
            "cache_ts": cache_ts,
        })

    return jsonify(data)


@app.route("/trades", methods=["GET"])
@require_auth
def trades_endpoint():
    """Lista de todos los trades (protegido)."""
    with state_lock:
        return jsonify(state.get("trades", []))


@app.route("/config", methods=["GET"])
@require_auth
def config_get():
    """Formulario HTML de configuración (protegido)."""
    return Response(render_config_page(), content_type="text/html; charset=utf-8")


@app.route("/config", methods=["POST"])
@require_auth
def config_post():
    """Actualizar configuración desde formulario (protegido + validado)."""
    updated = []
    errors = []
    for k in asdict(cfg).keys():
        val = request.form.get(k)
        if val is not None:
            try:
                tipo = type(getattr(cfg, k))
                parsed = tipo(val)
                # Validar rango
                ok, err_msg = _validate_config_value(k, parsed)
                if ok:
                    setattr(cfg, k, parsed)
                    updated.append(k)
                else:
                    errors.append(err_msg)
            except (ValueError, TypeError):
                errors.append(f"{k}: valor inválido '{val}'")

    cfg.save_to_file()

    if errors:
        msg = f'<div class="msg msg-err">Errores: {"; ".join(errors)}</div>'
        if updated:
            msg += f'<div class="msg msg-ok">Parametros actualizados: {", ".join(updated)}</div>'
    else:
        msg = f'<div class="msg msg-ok">Configuracion guardada. Parametros actualizados: {len(updated)}</div>'
    return Response(render_config_page(message=msg), content_type="text/html; charset=utf-8")


@app.route("/reset-circuit-breaker", methods=["POST"])
@require_auth
def reset_circuit_breaker():
    """Resetear circuit breaker manualmente."""
    with state_lock:
        balance = state.get("cached_balance", 0)
        state["paused_until"] = None
        state["equity_peak"] = balance if balance > 0 else state.get("equity_peak", 0)
        save_state(state)

    logger.info(f"Circuit breaker RESETEADO manualmente. Equity peak = ${balance:.2f}")

    msg = '<div class="msg msg-ok">Circuit breaker reseteado. El bot reanudara operaciones en el proximo ciclo.</div>'
    return Response(render_config_page(message=msg), content_type="text/html; charset=utf-8")


@app.route("/api/config", methods=["GET"])
@require_auth
def api_config_get():
    """API JSON para obtener configuración actual (protegido)."""
    return jsonify(asdict(cfg))


@app.route("/api/config", methods=["POST"])
@require_auth
def api_config_post():
    """API JSON para actualizar configuración (protegido + validado)."""
    data = request.get_json(silent=True) or {}
    updated = []
    errors = []
    for k, v in data.items():
        if hasattr(cfg, k):
            try:
                tipo = type(getattr(cfg, k))
                parsed = tipo(v)
                ok, err_msg = _validate_config_value(k, parsed)
                if ok:
                    setattr(cfg, k, parsed)
                    updated.append(k)
                else:
                    errors.append(err_msg)
            except (ValueError, TypeError):
                errors.append(f"{k}: valor inválido")
    cfg.save_to_file()
    return jsonify({"ok": len(errors) == 0, "updated": updated, "errors": errors, "config": asdict(cfg)})


@app.route("/api/reset-circuit-breaker", methods=["POST"])
@require_auth
def api_reset_circuit_breaker():
    """API JSON para resetear circuit breaker."""
    with state_lock:
        balance = state.get("cached_balance", 0)
        state["paused_until"] = None
        state["equity_peak"] = balance if balance > 0 else state.get("equity_peak", 0)
        save_state(state)
    logger.info(f"Circuit breaker RESETEADO via API. Equity peak = ${balance:.2f}")
    return jsonify({"ok": True, "equity_peak": balance})

# ============================================================
# MAIN
# ============================================================


def run_bot():
    """Loop del bot en thread daemon."""
    logger.info("=" * 50)
    logger.info("Bot autonomo iniciando...")
    logger.info(f"   Testnet: {TESTNET}")
    logger.info(f"   Leverage: {cfg.LEVERAGE}x")
    logger.info(f"   Riesgo: {cfg.RISK_PERCENT}%")
    logger.info(f"   SL: {cfg.SL_ATR_MULT}x ATR | TP: {cfg.TP_ATR_MULT}x ATR")
    logger.info(f"   RSI sobreventa: <{cfg.RSI_OVERSOLD} | sobrecompra: >{cfg.RSI_OVERBOUGHT}")
    logger.info(f"   EMA rapida: {cfg.EMA_FAST} | lenta: {cfg.EMA_SLOW}")
    logger.info(f"   Simbolos: {SYMBOLS}")
    logger.info(f"   Intervalo: cada {cfg.CHECK_INTERVAL_SEC}s")
    logger.info(f"   Cooldown entre trades: {cfg.COOLDOWN_SEC}s")
    logger.info("=" * 50)

    # Validar API keys al inicio
    if not validate_startup():
        logger.error("No se pudo validar conexion al exchange. Reintentando en 60s...")
        time.sleep(60)
        if not validate_startup():
            logger.error("Segunda validacion fallida. El bot continuara intentando en el loop.")

    with state_lock:
        state["bot_running"] = True

    # Primera ejecución inmediata
    try:
        check_signals()
    except Exception as e:
        logger.error(f"Error en primera ejecucion: {e}")

    # Loop principal
    while True:
        time.sleep(cfg.CHECK_INTERVAL_SEC)
        try:
            check_signals()
        except Exception as e:
            logger.error(f"Error en loop principal: {e}")
            time.sleep(30)


# Punto de entrada para gunicorn (importa bot:app)
os.makedirs("/app/data", exist_ok=True)

# Iniciar thread del bot al cargar el módulo
bot_thread = threading.Thread(target=run_bot, daemon=True)
bot_thread.start()

# Para ejecución directa con python bot.py (desarrollo)
if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
