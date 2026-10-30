"""Persistent Vinted searches and Telegram menu, shared by both worker hosts."""
import os
import re
from decimal import Decimal, InvalidOperation

import requests

MENU = {'keyboard': [['Add item', 'Watch list'], ['Remove item', 'Cancel']], 'resize_keyboard': True}
TABLE = 'vinted_bot_state'


def handle_message(state, text):
    text = text.strip()
    command = text.lower()
    items = state['items']
    if command in {'/start', '/help', 'cancel'}:
        state['pending'] = None
        return 'Use Add item to save a search, Watch list to review it, or Remove item to delete it.'
    if command in {'watch list', '/watchlist', 'remove item'}:
        state['pending'] = 'remove' if command == 'remove item' and items else None
        lines = [f"{i}. {w['query']} — £{w['max_price_minor'] / 100:.2f}" for i, w in enumerate(items, 1)]
        return ('\n'.join(lines) + ('\nSend the number to remove, or Cancel.' if state['pending'] else '')) if lines else 'Your watch list is empty. Use Add item.'
    if command in {'add item', '/add'}:
        if len(items) >= 30:
            return 'Your watch list is full (30 searches). Remove an item first.'
        state['pending'] = 'query'
        return 'Send the item or search terms to watch (up to 100 characters).'
    pending = state.get('pending')
    if pending == 'query':
        if not 2 <= len(text) <= 100 or not re.search(r'[a-zA-Z0-9]', text):
            return 'Send search terms between 2 and 100 characters.'
        if any(w['query'].casefold() == text.casefold() for w in items):
            state['pending'] = None
            return 'That search is already on your watch list.'
        state['pending'] = {'query': text}
        return 'What is the maximum price in GBP? For example: 35 or £35.50.'
    if isinstance(pending, dict):
        try:
            price = Decimal(text.removeprefix('£').strip())
            if not price.is_finite() or not 0 < price <= 100000 or price * 100 != (price * 100).to_integral_value():
                raise ValueError()
        except (InvalidOperation, ValueError):
            return 'Enter a valid price in GBP with up to two decimal places, or Cancel.'
        items.append({'query': pending['query'], 'max_price_minor': int(price * 100)})
        state['pending'] = None
        return f"Added {pending['query']} to your watch list. It will be searched next cycle."
    if pending == 'remove':
        if not text.isdigit() or not 1 <= int(text) <= len(items):
            return 'Send a number from the watch list, or Cancel.'
        removed = items.pop(int(text) - 1)
        state['pending'] = None
        return f"Removed {removed['query']}."
    return 'Use Add item, Watch list or Remove item.'


def screen_watch(listing, watch, now_ms):
    from supabase_backup import _age_minutes, _freshness
    blob = ' '.join(str(listing.get(k) or '') for k in ['title', 'brand', 'description']).casefold()
    words = re.findall(r'\w+', watch['query'].casefold())
    listing_words = set(re.findall(r'\w+', blob))
    age = _age_minutes(listing.get('publishedAtMs'), now_ms)
    price = listing.get('priceMinor')
    match = (bool(words) and all(w in listing_words for w in words)
             and listing.get('currency') == 'GBP' and isinstance(price, (int, float))
             and 0 <= price <= watch['max_price_minor'] and age < 1440)
    return {'isMatch': match, 'eligibleForNotify': match and age < 60,
            'ageMinutes': age, 'freshnessBucket': _freshness(age, 10),
            'score': 60 if match else 0, 'reasons': [] if match else ['watch_filters_not_met']}


def load_state(client, defaults):
    seed = {'items': defaults, 'pending': None, 'offset': 0}
    client.insert_if_absent(TABLE, {'id': 1, 'state': seed}, on_conflict='id')
    return client.select(TABLE, {'id': 'eq.1', 'select': 'state'})[0]['state']


def poll_updates(client, state):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return
    base = f'https://api.telegram.org/bot{token}/'
    response = requests.post(base + 'getUpdates', json={'offset': state['offset'], 'timeout': 0, 'allowed_updates': ['message']}, timeout=15)
    if not response.ok or not response.json().get('ok'):
        raise RuntimeError('Telegram watch-list polling failed; check bot token and ensure no webhook is configured')
    for update in response.json()['result']:
        message = update.get('message', {})
        text = message.get('text')
        allowed = str(message.get('chat', {}).get('id')) == str(chat_id)
        reply = handle_message(state, text) if allowed and isinstance(text, str) else None
        state['offset'] = update['update_id'] + 1
        client.update(TABLE, {'id': 'eq.1'}, {'state': state})
        if reply:
            result = requests.post(base + 'sendMessage', json={'chat_id': chat_id, 'text': reply, 'reply_markup': MENU}, timeout=15)
            if not result.ok or not result.json().get('ok'):
                raise RuntimeError('Telegram watch-list reply failed')


def configured_state():
    from supabase_backup import SupabaseClient
    from run_cycle import DEFAULT_VINTED_QUERIES, VINTED_MAX_PRICE_MINOR, MAX_PRICE_GBP, parse_list_env
    client = SupabaseClient(os.environ['SUPABASE_URL'].strip(), os.environ['SUPABASE_SERVICE_ROLE_KEY'].strip())
    queries = parse_list_env(os.getenv('VINTED_QUERIES'))
    if not queries:
        queries = [os.environ['VINTED_QUERY']] if os.getenv('VINTED_QUERY') else DEFAULT_VINTED_QUERIES
    defaults = [{'query': q, 'max_price_minor': VINTED_MAX_PRICE_MINOR if 'patagonia' in q.lower() else MAX_PRICE_GBP * 100, 'legacy': True} for q in queries]
    return client, load_state(client, defaults)


def poll_configured():
    client, state = configured_state()
    poll_updates(client, state)
