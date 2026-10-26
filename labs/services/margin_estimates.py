"""Cached, explicitly estimated MCX capital. Margin queries never place orders."""
from datetime import datetime, timezone, timedelta
import json
import math
import threading
import uuid
from config.labs_config import STATE_DIR

CACHE_PATH = STATE_DIR / 'mcx_margin_estimates.json'
UNITS = {'CRUDEOILM':10, 'GOLD':100, 'GOLDM':10}
IST = timezone(timedelta(hours=5,minutes=30))
_refresh_lock = threading.Lock()
_cached = (None,{})

def cached_quotes():
    global _cached
    try:
        stamp = (str(CACHE_PATH),CACHE_PATH.stat().st_mtime_ns)
        if _cached[0] != stamp:
            _cached = (stamp,json.loads(CACHE_PATH.read_text(encoding='utf-8')))
        return _cached[1]
    except (OSError,ValueError):
        return {}

def estimate_margin(underlying,entry_price,quantity,direction):
    quote = cached_quotes().get('quotes',{}).get(underlying)
    side = 'SELL' if str(direction).upper() in ('SHORT','SELL') else 'BUY'
    if not isinstance(quote,dict) or not quote.get('asof') or not quote.get('contract') or entry_price is None or quantity is None:
        return None
    rate = quote.get('sides',{}).get(side,{}).get('rate')
    if not isinstance(rate,(float,int)) or not math.isfinite(rate) or rate <= 0:
        return None
    value = rate*entry_price*quantity
    if not math.isfinite(value) or value <= 0:
        return None
    return dict(value=round(value,2),asof=quote['asof'],contract=quote['contract'],
                basis=f"Estimated NRML margin; {quote['contract']} quote {quote['asof'][:16]} IST, scaled to entry notional")

def refresh_quotes(kite=None,now=None):
    """Explicit refresh only; rendering reads the cache without broker requests."""
    now = now or datetime.now(IST)
    with _refresh_lock:
        previous = cached_quotes()
        try:age = (now-datetime.fromisoformat(previous['asof'])).total_seconds()
        except (KeyError,TypeError,ValueError):age = float('inf')
        if 0 <= age < 900 and set(UNITS) <= set(previous.get('quotes',{})):
            return previous
        if kite is None:
            from auth.session_manager import get_kite
            kite = get_kite()
        selected = {}
        instruments = kite.instruments('MCX')
        for name in UNITS:
            eligible = [r for r in instruments if r.get('name') == name and r.get('instrument_type') == 'FUT'
                        and str(r.get('expiry'))[:10] >= now.date().isoformat()]
            if eligible:selected[name] = min(eligible,key=lambda r:str(r['expiry']))
        prices = kite.quote(['MCX:'+r['tradingsymbol'] for r in selected.values()])
        quotes,errors = dict(previous.get('quotes',{})),[]
        for name,instrument in selected.items():
            try:
                symbol = instrument['tradingsymbol']
                price = float(prices['MCX:'+symbol]['last_price'])
                lot = int(instrument.get('lot_size') or 1)
                if not math.isfinite(price) or price <= 0 or lot <= 0:raise ValueError('No usable price')
                sides = {}
                for side in ('BUY','SELL'):
                    hypothetical = dict(exchange='MCX',tradingsymbol=symbol,transaction_type=side,
                                        variety='regular',product='NRML',order_type='MARKET',quantity=lot)
                    result = kite.basket_order_margins([hypothetical],consider_positions=False)
                    margin = float(result['final']['total'])
                    if not math.isfinite(margin) or margin <= 0:raise ValueError('No usable margin')
                    sides[side] = dict(margin=margin,rate=margin/(price*UNITS[name]*lot))
                quotes[name] = dict(contract=symbol,asof=now.isoformat(timespec='seconds'),
                                    price=price,quote_quantity=lot,physical_units=UNITS[name]*lot,sides=sides)
            except Exception:errors.append(name)
        errors.extend(name for name in UNITS if name not in selected)
        payload = dict(asof=now.isoformat(timespec='seconds'),quotes=quotes,unavailable=errors)
        CACHE_PATH.parent.mkdir(parents=True,exist_ok=True)
        temporary = CACHE_PATH.with_name(CACHE_PATH.name+'.'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(payload,indent=2),encoding='utf-8')
        temporary.replace(CACHE_PATH)
        return payload
