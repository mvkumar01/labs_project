"""Read-only, date-scoped performance contract for every /labs/live strategy.

Never calls a broker or rebuilds a book. Unknown quantities, prices and charges
stay unknown. Closed-trade metrics and marked open positions are kept separate.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class Source:
    table: str
    underlying: str = 'NIFTY'
    qty: float | None = None
    short: bool = False
    group: str | None = None
    where: str = ''
    date_column: str = 'trade_date'
    currency: str = 'INR'
    daily_only: bool = False


SOURCES = {
    'nifty': Source('paper_contract_trades', qty=65, where="variant='near_itm200'"),
    'sensex_alpha': Source('sensex_alpha_inverted_trades', 'SENSEX', 20),
    'theta_straddle': Source('theta_straddle_trades', qty=65, short=True),
    'theta_iron_fly': Source('theta_iron_fly_trades', qty=65),
    'crude_macd_st': Source('crude_macd_st_trades', 'CRUDEOIL', 100),
    'crudeoilm_ma20': Source('crudeoilm_ma20_trades', 'CRUDEOILM', 10),
    'crudeoilm_keltner': Source('crudeoilm_keltner_trades', 'CRUDEOILM', 10),
    'gold_cci': Source('gold_cci_trades', 'GOLD', 100, short=True),
    'crudem_combo': Source('crudem_combo_trades', 'CRUDEOILM', 10),
    'btc_rsi_roc': Source('btc_rsi_roc_trades', 'BTCUSDT', .01, short=True),
    'nifty_expiry_sale': Source('nifty_expiry_sale_trades', qty=65, short=True, group='book'),
    'nifty_tuesday_put': Source('nifty_tuesday_put_trades', qty=65),
    'expiry_strangle': Source('expiry_strangle_events', short=True,
                              where="variant='k2' AND fill='bidask'", date_column='expiry'),
    'infy_tcs_pair': Source('infy_tcs_pair_trades', 'INFY / TCS', date_column='entry_date'),
    'baskets': Source('basket_trades', group='basket'),
    'risk_core': Source('risk_core_days', 'Portfolio', daily_only=True),
    'crypto_xs': Source('crypto_xs_daily', 'Portfolio', date_column='decision_date', currency='USD', daily_only=True),
    'crypto_xs_c': Source('crypto_xs_c_daily', 'Portfolio', date_column='decision_date', currency='USD', daily_only=True),
}
for _key in ('alpha_v211b', 'alpha_v212', 'alpha_v212b10', 'alpha_v214',
             'alpha_v214c', 'alpha_v214d', 'alpha_v214e', 'alpha_cpr'):
    SOURCES[_key] = Source(_key + '_trades', qty=None if _key == 'alpha_cpr' else 65)
for _key in ('proposer_px', 'proposer_v3', 'proposer_v3_dt35', 'proposer_v4',
             'proposer_v5', 'proposer_v5_dt25', 'proposer_v3_dt35_clock'):
    SOURCES[_key] = Source(_key + '_trades', 'SENSEX', 2000)
SOURCES['proposer_nifty_v5'] = Source('proposer_nifty_v5_trades', 'NIFTY')
SOURCES['bn_alpha'] = Source('bn_alpha_trades', 'BANKNIFTY', 30)
PAGE_SIZES = (10, 20, 25, 50, 100, 150, 200)

def page_size(value):
    try:
        value = int(value)
    except (ValueError, TypeError):
        return 100
    return value if value in PAGE_SIZES else 100


DRY_SOURCES = {
    'gold_cci': Source('live_gold_trades', 'GOLDM', 10, short=True, where="book='dry'"),
    'crudem_combo': Source('live_crudem_trades', 'CRUDEOILM', 10, where="book='dry'"),
}


def number(value):
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def pick(row, *keys):
    return next((row[k] for k in keys if row.get(k) is not None), None)


def stamp(value, *, utc=False):
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc if utc else IST)
        return t.astimezone(IST)
    except ValueError:
        return None


def rows(conn, table, date_column, date_from, date_to, where=''):
    # All identifiers/where expressions come exclusively from the registry above.
    if not conn.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone():
        return None
    parts, params = ['1=1'], []
    for op, value in (('>=', date_from), ('<=', date_to)):
        if value:
            parts.append(f'"{date_column}" {op} ?')
            params.append(value)
    if where:
        parts.append('(' + where + ')')
    cur = conn.execute(f'SELECT * FROM "{table}" WHERE ' + ' AND '.join(parts), params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def normalize(row, spec, key):
    utc = key == 'btc_rsi_roc'
    entry = stamp(pick(row, 'entry_ts', 'entry_time', 'entry_minute', 'entry_date'), utc=utc)
    exit_ = stamp(pick(row, 'exit_ts', 'exit_time', 'exit_minute', 'exit_date'), utc=utc)
    status = str(row.get('status') or '').lower()
    if status == 'open':
        exit_ = None  # some books persist a mark timestamp in exit_ts while held
    closed = status not in ('open', 'skipped', 'pending') and exit_ is not None
    direction = str(pick(row, 'direction', 'position_side', 'side') or ('SHORT' if spec.short else 'LONG')).upper()
    if key == 'nifty_tuesday_put':
        direction = 'PUT'
    short = direction in ('SHORT', 'SELL')
    ep = number(pick(row, 'entry_price', 'entry_prem', 'entry_ask', 'entry_sell_bid', 'bought', 'credit'))
    xp = number(pick(row, 'exit_price', 'exit_prem', 'exit_bid', 'exit_buy_ask', 'sold', 'buyback'))
    if key == 'nifty_expiry_sale':
        ep = sum(number(row.get(x)) or 0 for x in ('call_sold', 'put_sold')) or None
        xp = sum(number(row.get(x)) or 0 for x in ('call_bought', 'put_bought')) or None
    qty = number(row.get('qty'))
    gross = number(pick(row, 'gross_rs', 'gross_pnl', 'option_gross_rs'))
    if qty is None and key == 'proposer_nifty_v5':
        from labs.services.capital_metrics import recorded_nifty_quantity
        qty = recorded_nifty_quantity(row)
    # Recover recorded execution quantity only where premium arithmetic reconciles exactly.
    if qty is None and ep is not None and xp is not None and gross is not None and xp != ep:
        implied = gross / ((ep - xp) if short else (xp - ep))
        if implied > 0 and abs(implied - round(implied)) < .02 and (key != 'proposer_nifty_v5' or round(implied) % 65 == 0):
            qty = float(round(implied))
    qty = qty if qty is not None else spec.qty
    charges = number(pick(row, 'charges_rs', 'charges_total', 'costs_rs'))
    slippage = number(row.get('slippage_rs'))
    net = number(pick(row, 'net_rs', 'net_pnl'))
    costs = charges + (slippage or 0) if charges is not None else None
    if net is None and gross is not None and costs is not None:
        net = gross - costs
    contract = pick(row, 'tradingsymbol', 'symbol')
    if not contract and row.get('strike') is not None:
        contract = ' '.join(str(x) for x in (spec.underlying, row.get('expiry_code') or '', row['strike'],
                                            pick(row, 'option_type', 'side') or '') if x != '')
    if key in ('nifty_expiry_sale', 'expiry_strangle'):
        contract = f"{row.get('und') or spec.underlying} {row.get('expiry_code') or row.get('expiry') or ''} CE + PE"
    if key == 'infy_tcs_pair':
        contract = f"Long {row.get('long_leg', '?')} / short {row.get('short_leg', '?')}"
        direction = 'PAIR'
    capital = number(pick(row, 'allocated_capital_rs', 'capital_required_rs', 'margin_rs', 'notional'))
    basis = 'Recorded capital / margin' if capital is not None else 'Not recorded'
    is_option = spec.underlying in ('NIFTY', 'SENSEX', 'BANKNIFTY')
    if capital is None and is_option and not short and ep is not None and qty is not None:
        capital, basis = ep * qty, 'Premium paid'
    if capital is None and key == 'btc_rsi_roc':
        capital = number(row.get('notional_rs'))
        basis = 'Recorded funded spot notional'
    exit_value = xp * qty if xp is not None and qty is not None else None
    if key == 'btc_rsi_roc' and exit_value is not None:
        fx = number(row.get('fx_rate'))
        exit_value = exit_value * fx if fx is not None else None
    spot_points = number(pick(row, 'spot_pnl_pts', 'pnl_pts', 'points'))
    es, xs = number(row.get('entry_spot')), number(row.get('exit_spot'))
    if spot_points is None and es is not None and xs is not None:
        spot_points = (xs - es) * (-1 if direction in ('PUT', 'PE', 'SHORT', 'SELL') else 1)
    day = str(row.get(spec.date_column) or pick(row, 'trade_date', 'entry_date', 'expiry') or (entry.date() if entry else ''))[:10]
    identity = {k:row.get(k) for k in ('trade_date','seq','id','trade_ref','leg','book','basket','side','entry_date','expiry','und','variant','fill','symbol','tradingsymbol')}
    ident = hashlib.sha256((key + spec.table + json.dumps(identity, sort_keys=True, default=str)).encode()).hexdigest()[:20]
    duration = None
    if entry and exit_:
        seconds = int((exit_ - entry).total_seconds())
        duration = f'{seconds // 86400}d {seconds % 86400 // 3600}h {seconds % 3600 // 60}m' if seconds >= 86400 else f'{seconds // 3600}h {seconds % 3600 // 60}m {seconds % 60}s'
    result = dict(id=ident, date=day, contract=contract, entry_ts=entry.isoformat() if entry else None,
                exit_ts=exit_.isoformat() if exit_ else None, duration=duration, direction=direction,
                qty=qty, capital=capital, capital_basis=basis, entry_price=ep, exit_price=xp if closed else None,
                exit_value=exit_value if closed else None, spot_points=spot_points,
                target=number(pick(row, 'target_price', 'target_rs', 'spot_target_pts')),
                gross=gross, charges=charges, slippage=slippage, costs=costs, net=net,
                pnl_pct=100 * net / capital if net is not None and capital and capital > 0 else None,
                exit_rule=pick(row, 'exit_reason', 'exit_rule', 'reason'), closed=closed,
                status='Closed' if closed else ('Open' if status == 'open' or entry and not exit_ else status.title()),
                peak=number(row.get('peak_pnl_rs')), post_peak=number(row.get('post_exit_peak_rs')),
                post_trough=number(row.get('post_exit_trough_rs')), underlying=spec.underlying,
                currency=spec.currency, group=(str(row.get('side') or '')+' / ' if key=='baskets' else '')+str(row.get(spec.group) or '') if spec.group else '',
                multi_leg=key in ('nifty_expiry_sale','expiry_strangle','baskets','infy_tcs_pair'),
                qty_text=None, source_key=key, strike=row.get('strike'), expiry_code=row.get('expiry_code'),
                option_type=str(row.get('option_type') or ('PE' if direction in ('PE','PUT') else 'CE')).upper())
    result['entry_has_time'] = len(str(pick(row,'entry_ts','entry_time','entry_minute','entry_date') or '')) > 10
    result['exit_has_time'] = len(str(pick(row,'exit_ts','exit_time','exit_minute','exit_date') or '')) > 10
    if closed and (not result['entry_has_time'] or not result['exit_has_time']):
        result['duration'] = f"{(exit_-entry).days} days (dates only)" if entry and exit_ else None
    result['target_kind'] = 'Price' if row.get('target_price') is not None else 'Amount' if row.get('target_rs') is not None else 'Spot points'
    if key == 'baskets':
        try:
            legs=json.loads(row.get('legs_json') or '[]')
        except (ValueError,TypeError):
            legs=[]
        result.update(contract=' + '.join(f"{leg.get('action','?')} {leg.get('strike','?')}{leg.get('otype','?')}" for leg in legs) or None,
                      entry_price=None,exit_price=None,qty=None,
                      qty_text=f'65 units / leg × {len(legs)}' if legs else None,
                      capital=None,capital_basis='Basket margin not recorded',pnl_pct=None,
                      exit_value=sum(float(leg['exit_px'])*65 for leg in legs) if legs and all(leg.get('exit_px') is not None for leg in legs) else None,
                      direction=f'Multi-leg ({len(legs)})',option_type=None)
    if key == 'baskets' and legs:
        result['legs'] = []
        for index, leg in enumerate(legs):
            ein, xout = number(leg.get('entry_px')), number(leg.get('exit_px'))
            child = dict(row, seq=str(row.get('seq'))+'-'+str(index), direction=leg.get('action'),
                         entry_price=ein, exit_price=xout, strike=leg.get('strike'),
                         option_type=leg.get('otype'), qty=65, tradingsymbol=None,
                         gross_rs=(xout-ein)*65*(-1 if leg.get('action')=='SELL' else 1)
                         if ein is not None and xout is not None else None)
            result['legs'].append(normalize(child,Source('basket_leg',qty=65),'basket_leg'))
    if key == 'nifty_expiry_sale':
        result['legs'] = []
        for cp, kind in [('call','CE'),('put','PE')]:
            ein,xout=number(row.get(cp+'_sold')),number(row.get(cp+'_bought'))
            child=dict(row,side=None,direction='SHORT',option_type=kind,qty=qty,
                       entry_price=ein,exit_price=xout,tradingsymbol=None,
                       gross_rs=(ein-xout)*qty if ein is not None and xout is not None and qty is not None else None)
            result['legs'].append(normalize(child,Source('expiry_sale_leg',qty=65),'expiry_sale_leg'))
    return result



def summarize(trades):
    closed = [t for t in trades if t['closed']]
    priced = [t for t in closed if t['net'] is not None]
    daily = defaultdict(float)
    for t in priced:
        daily[t['date']] += t['net']
    wins = sum(t['net'] > 0 for t in priced)
    day_wins = sum(v > 0 for v in daily.values())
    def total(field):
        values = [t[field] for t in closed if t[field] is not None]
        return sum(values) if values else (0.0 if not closed else None)
    known_gross = [t['gross'] for t in closed if t['gross'] is not None]
    return dict(closed=len(closed), priced=len(priced), unpriced=len(closed)-len(priced), wins=wins,
                trade_win_pct=100*wins/len(priced) if priced else None,
                days=len(daily), winning_days=day_wins,
                day_win_pct=100*day_wins/len(daily) if daily else None,
                gross=total('gross'), charges=total('charges'), slippage=total('slippage'),
                costs=total('costs'), net=total('net'),
                gross_profit=sum(max(0,v) for v in known_gross) if known_gross or not closed else None,
                gross_loss=sum(min(0,v) for v in known_gross) if known_gross or not closed else None,
                open_count=sum(t['status']=='Open' for t in trades),
                open_net=sum(t['net'] for t in trades if t['status']=='Open' and t['net'] is not None),
                missing_costs=sum(t['costs'] is None for t in closed),
                missing_gross=sum(t['gross'] is None for t in closed),
                breakeven=sum(t['net']==0 for t in priced),
                breakeven_days=sum(v==0 for v in daily.values()))


def normalized_trades(raw, spec, key):
    trades = [normalize(r,spec,key) for r in raw or [] if str(r.get('status','')).lower() != 'skipped']
    if key not in ('theta_straddle', 'theta_iron_fly'):
        return trades
    # A straddle or iron fly is one trade, not two/four independent wins.
    grouped = defaultdict(list)
    for t in trades:
        grouped[t['date']].append(t)
    combined = []
    for day, legs in grouped.items():
        t = dict(legs[0])
        for field in ('gross', 'charges', 'slippage', 'costs', 'net', 'capital', 'exit_value'):
            values = [leg[field] for leg in legs]
            t[field] = sum(values) if all(v is not None for v in values) else None
        t.update(id=hashlib.sha256(''.join(sorted(l['id'] for l in legs)).encode()).hexdigest()[:20],
                 contract=' + '.join(l['contract'] or '?' for l in legs),
                 direction=f'Multi-leg ({len(legs)})', qty=None,
                 qty_text=' / '.join(str(int(l['qty'])) if l['qty'] is not None else '?' for l in legs)+' units by leg',
                 closed=all(l['closed'] for l in legs), multi_leg=True, option_type=None,
                 entry_price=None, exit_price=None, spot_points=None,
                 exit_ts=max((l['exit_ts'] for l in legs if l['exit_ts']),default=None) if all(l['closed'] for l in legs) else None,
                 entry_ts=min((l['entry_ts'] for l in legs if l['entry_ts']),default=None),
                 exit_rule=' / '.join(sorted({str(l['exit_rule']) for l in legs if l['exit_rule']})))
        t['status'] = 'Closed' if t['closed'] else 'Open'
        t['pnl_pct'] = 100*t['net']/t['capital'] if t['net'] is not None and t['capital'] else None
        en, ex = stamp(t['entry_ts']), stamp(t['exit_ts'])
        t['duration'] = None
        if en and ex:
            t['duration'] = str(ex-en)
        t['legs'] = legs
        combined.append(t)
    return combined


def make_report(raw, spec, key, title, page=1, per_page=100):
    per_page = page_size(per_page)
    trades = normalized_trades(raw, spec, key)
    trades.sort(key=lambda t:(t['date'],t['entry_ts'] or '',t['id']),reverse=True)
    months=defaultdict(list)
    for t in trades:
        months[t['date'][:7]].append(t)
    return dict(key=key,title=title,currency=spec.currency,available=raw is not None,
                source_table=spec.table, stats=summarize(trades), months=[dict(month=m,**summarize(v)) for m,v in sorted(months.items(),reverse=True)],
                trades=trades[(page-1)*per_page:page*per_page], count=len(trades), page=page, per_page=per_page, page_sizes=PAGE_SIZES,
                pages=max(1,math.ceil(len(trades)/per_page)), daily_only=False,
                notes=['Metrics use closed, priced trades. Break-even trades/days stay in the denominators.',
                       'Entry capital uses recorded margin/allocation or the premium paid for long options. Unrecorded capital is unavailable.',
                       'Peak and post-exit values are gross hypothetical P&L from the original entry, not additional realized profit.'])


def daily_report(conn,key,spec,start,end,title):
    raw=rows(conn,spec.table,spec.date_column,None,None)
    daily=[]
    if key.startswith('crypto_xs'):
        account=key+'_account_daily'
        if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(account,)).fetchone():
            raw=[json.loads(r[0]) for r in conn.execute(f'SELECT payload FROM "{account}" ORDER BY decision_date')]
        previous=100000.0
        for r in sorted(raw or [],key=lambda r:r['decision_date']):
            if not r.get('complete'):
                continue
            net=number(r.get('net'))
            equity=number(r.get('equity'))
            opening=equity/(1+net) if equity is not None and net is not None and net!=-1 else previous
            daily.append(dict(date=r['decision_date'], net=net*opening if net is not None else None,
                              gross=(number(r.get('gross'))+number(r.get('funding')))*opening if number(r.get('gross')) is not None and number(r.get('funding')) is not None else None,
                              costs=number(r.get('cost'))*opening if number(r.get('cost')) is not None else None))
            if equity is not None:
                previous=equity
    else:
        daily=[dict(date=r['trade_date'],net=number(r.get('net_rs')),gross=None,costs=None) for r in raw or []]
    daily=[r for r in daily if (not start or r['date']>=start) and (not end or r['date']<=end)]
    synthetic=[dict(date=r['date'],closed=True,status='Daily mark',net=r['net'],gross=r['gross'],costs=r['costs'],charges=r['costs'],slippage=None) for r in daily]
    report=make_report([],spec,key,title)
    report.update(available=raw is not None,daily_only=True,daily=sorted(daily,key=lambda r:r['date'],reverse=True),stats=summarize(synthetic))
    report['stats']['closed']=None
    report['stats']['trade_win_pct']=None
    months=defaultdict(list)
    for r in synthetic:
        months[r['date'][:7]].append(r)
    report['months']=[dict(month=m,**summarize(v)) for m,v in sorted(months.items(),reverse=True)]
    report['notes']=['This allocation strategy records daily portfolio marks, not discrete round-trip trades. Trade win rate and trade-only fields are unavailable.',
                     'Crypto money is USD; funding is included in gross P&L. Rupee and dollar books are never combined.']
    return report


def build_reports(conn,key,date_from=None,date_to=None,*,title=None,page=1,per_page=100,live_conn=None):
    spec=SOURCES.get(key)
    if spec is None:
        return []
    if spec.daily_only:
        return [daily_report(conn,key,spec,date_from,date_to,title or key)]
    raw=rows(conn,spec.table,spec.date_column,date_from,date_to,spec.where)
    # The older NIFTY schema can precede contract-variant ledgers.
    if raw is None and key=='nifty':
        spec=Source('paper_strategy_trades',qty=65)
        raw=rows(conn,spec.table,spec.date_column,date_from,date_to)
    if key=='baskets' and raw:
        raw=enrich_baskets(conn,raw,date_from,date_to)
    reports=[]
    if spec.group and raw:
        grouped=defaultdict(list)
        for r in raw:
            group=str(r.get(spec.group) or '')
            if key=='baskets':
                group=str(r.get('side') or '')+' / '+group
            grouped[group].append(r)
        for group,items in sorted(grouped.items()):
            reports.append(make_report(items,spec,key,(title or key)+' · '+group,page,per_page))
    else:
        reports.append(make_report(raw,spec,key,title or key,page,per_page))
    if key in DRY_SOURCES:
        own=live_conn is None
        try:
            if own:
                from config.labs_config import LIVE_DB_PATH
                live_conn=sqlite3.connect(LIVE_DB_PATH.resolve().as_uri()+'?mode=ro',uri=True)
            dry=DRY_SOURCES[key]
            data=rows(live_conn,dry.table,dry.date_column,date_from,date_to,dry.where)
            reports.append(make_report(data,dry,key,(title or key)+' · Real-time dry run',page,per_page))
        except sqlite3.Error:
            reports.append(make_report(None,DRY_SOURCES[key],key,(title or key)+' · Real-time dry run',page,per_page))
        finally:
            if own and live_conn is not None:
                live_conn.close()
    return reports


def filter_overview(conn, cards, start, end, titles):
    """Keep account status private; scope simulated card figures to the filter."""
    output=[]
    for original in cards:
        card=dict(original)
        key=card.get('tab',card['key'])
        if card.get('no_figures') or key not in SOURCES:
            output.append(card)
            continue
        panels=build_reports(conn,key,start,end,title=titles.get(key,key))
        selected=panels[0] if panels else None
        if card['key'].startswith('dry_'):
            selected=next((p for p in panels if p['title'].endswith('Real-time dry run')),None)
        elif key=='nifty_expiry_sale':
            suffix=card['key'].removeprefix('nifty_expiry_sale_')
            selected=next((p for p in panels if p['title'].endswith(' · '+suffix)),None)
        if selected is None or not selected['available']:
            card['error']='No ledger in selected period'
        else:
            stats=selected['stats']
            if stats['closed'] is None:
                card.pop('trades',None)
            card.update(total_net=stats['net'] or 0,win_pct=stats['day_win_pct'],
                        win_days=stats['winning_days'],active_days=stats['days'],
                        return_pct=None,
                        **({'trades':stats['closed']} if stats['closed'] is not None else {}),
                        note='P&L and win rates use the selected dates. Today and position status remain current.')
        output.append(card)
    return output


def enrich_baskets(conn, raw, start, end):
    parent=rows(conn,'paper_strategy_trades','trade_date',start,end) or []
    lookup={(r['trade_date'],r['seq'],r['side']):r for r in parent}
    days=rows(conn,'basket_daily','trade_date',start,end) or []
    expiries={(r['trade_date'],r['side'],r['basket']):r.get('expiry_code') for r in days}
    return [{**lookup.get((r['trade_date'],r['seq'],r['side']),{}),**r,
             'expiry_code':expiries.get((r['trade_date'],r['side'],r['basket']))} for r in raw]
