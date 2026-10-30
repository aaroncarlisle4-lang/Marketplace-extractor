import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workers'))
from vinted_watchlist import handle_message, screen_watch


class WatchlistTests(unittest.TestCase):
    def setUp(self):
        self.state = {'items': [], 'pending': None, 'offset': 0}

    def message(self, text):
        return handle_message(self.state, text)

    def test_add_list_remove_and_empty_stays_empty(self):
        self.message('Add item')
        self.message('Nike air max')
        self.assertIn('price', self.message('Nike air max').lower())
        self.message('£35.50')
        self.assertEqual(self.state['items'][0]['max_price_minor'], 3550)
        self.assertIn('Nike air max', self.message('Watch list'))
        self.message('Remove item')
        self.message('1')
        self.assertEqual(self.state['items'], [])

    def test_invalid_price_cancel_and_duplicates(self):
        self.message('Add item')
        self.message('Nike shoes')
        for invalid in ['0', '-1', 'nan', '5.001', 'abc']:
            self.message(invalid)
            self.assertEqual(self.state['items'], [])
        self.message('Cancel')
        self.assertIsNone(self.state['pending'])
        self.state['items'] = [{'query': 'Nike shoes', 'max_price_minor': 2000}]
        self.message('Add item')
        self.message('NIKE SHOES')
        self.assertIsNone(self.state['pending'])

    def test_custom_screen_requires_all_words_price_currency_and_freshness(self):
        watch = {'query': 'Nike air max', 'max_price_minor': 4000}
        listing = {'title': 'Nike Air Max shoes', 'priceMinor': 3500, 'currency': 'GBP', 'publishedAtMs': 990000}
        self.assertTrue(screen_watch(listing, watch, 1000000)['eligibleForNotify'])
        for changes in [{'title': 'Nike trainers'}, {'priceMinor': 4500}, {'currency': 'EUR'}, {'publishedAtMs': 0}]:
            self.assertFalse(screen_watch({**listing, **changes}, watch, 4000000)['eligibleForNotify'])


if __name__ == '__main__':
    unittest.main()


class PersistenceTests(unittest.TestCase):
    def test_empty_saved_list_is_not_reseeded(self):
        from unittest.mock import Mock
        from vinted_watchlist import load_state
        client = Mock()
        client.select.return_value = [{'state': {'items': [], 'pending': None, 'offset': 12}}]
        self.assertEqual(load_state(client, [{'query': 'patagonia'}])['items'], [])

    def test_only_configured_chat_can_change_list_and_offset_is_saved(self):
        from unittest.mock import Mock, patch
        from vinted_watchlist import poll_updates
        state = {'items': [], 'pending': None, 'offset': 0}
        response = Mock(ok=True)
        response.json.return_value = {'ok': True, 'result': [
            {'update_id': 10, 'message': {'chat': {'id': 999}, 'text': 'Remove item'}},
            {'update_id': 11, 'message': {'chat': {'id': 123}, 'text': 'Add item'}},
        ]}
        sent = Mock(ok=True)
        sent.json.return_value = {'ok': True}
        client = Mock()
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': '123'}), patch('vinted_watchlist.requests.post', side_effect=[response, sent]) as post:
            poll_updates(client, state)
        self.assertEqual(state['pending'], 'query')
        self.assertEqual(state['offset'], 12)
        self.assertEqual(post.call_count, 2)
        self.assertIn('Send the item', post.call_args.kwargs['json']['text'])
        self.assertEqual(client.update.call_count, 2)

    def test_cycle_uses_saved_queries_and_price_and_skips_empty_list(self):
        from unittest.mock import patch
        import run_cycle
        for items in [[], [{'query': 'Nike shoes', 'max_price_minor': 3550}]]:
            with patch.dict('os.environ', {'SUPABASE_URL': 'fake', 'SUPABASE_SERVICE_ROLE_KEY': 'fake', 'VINTED_POLL_TELEGRAM': '0'}), patch('vinted_watchlist.configured_state', return_value=(None, {'items': items})), patch.object(run_cycle, 'scrape_vinted_listings_with_stats', return_value={'listings': [], 'stats': {}}) as scrape, patch('builtins.print'):
                self.assertEqual(run_cycle.main(), 0)
                self.assertEqual(scrape.call_count, len(items))
                if items:
                    self.assertEqual(scrape.call_args.kwargs['query'], 'Nike shoes')
                    self.assertEqual(scrape.call_args.kwargs['max_price_gbp'], 36)
                    self.assertFalse(scrape.call_args.kwargs['strict_target_only'])
