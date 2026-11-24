import json
from unittest import TestCase
from unittest.mock import MagicMock

from steampy.exceptions import ApiException
from steampy.market import SteamMarket
from steampy.models import Currency, GameOptions


class DummyResponse:
    def __init__(self, status_code=200, json_data=None, text=''):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


def make_listing(listingid, asset_id, market_hash_name='Item'):
    return {
        'listingid': str(listingid),
        'time_created': 1600000000,
        'price': 100,
        'fee': 5,
        'currencyid': '2001',
        'status': 1,
        'active': True,
        'asset': {
            'appid': 730,
            'contextid': '2',
            'id': str(asset_id),
            'market_hash_name': market_hash_name,
        },
    }


def make_buy_order(buy_orderid, appid=730, quantity='2', quantity_remaining='2'):
    return {
        'appid': appid,
        'hash_name': 'Item',
        'wallet_currency': 1,
        'price': '100',
        'quantity': quantity,
        'quantity_remaining': quantity_remaining,
        'buy_orderid': str(buy_orderid),
        'description': {'type': 'item', 'value': 'Item'},
    }


def make_listings_page(
    listings=None,
    buy_orders=None,
    listings_to_confirm=None,
    listings_on_hold=None,
    total_count=None,
    start=0,
    success=True,
):
    listings = [] if listings is None else listings
    return {
        'success': success,
        'pagesize': 100,
        'total_count': len(listings) if total_count is None else total_count,
        'start': start,
        'num_active_listings': len(listings),
        'assets': {},
        'listings': listings,
        'listings_on_hold': [] if listings_on_hold is None else listings_on_hold,
        'listings_to_confirm': [] if listings_to_confirm is None else listings_to_confirm,
        'buy_orders': [] if buy_orders is None else buy_orders,
    }


def make_market(*responses):
    market = SteamMarket(MagicMock())
    market.was_login_executed = True
    market._request = MagicMock(side_effect=list(responses))
    return market


def expected_params(language='english', start=0):
    return {'start': start, 'count': 100, 'norender': 1, 'l': language}


class TestSteamMarketUnit(TestCase):
    def test_create_buy_order_raises_when_failed_without_confirmation(self):
        market = SteamMarket(MagicMock())
        market.was_login_executed = True
        market._session_id = 'sid'
        market._request = MagicMock(return_value=DummyResponse(json_data={'success': 0, 'message': 'failed'}))

        with self.assertRaises(ApiException):
            market.create_buy_order('AK-47', '10.00', 2, GameOptions.CS, Currency.USD)

    def test_buy_item_raises_on_non_success_wallet_info(self):
        market = SteamMarket(MagicMock())
        market.was_login_executed = True
        market._session_id = 'sid'
        market._request = MagicMock(return_value=DummyResponse(json_data={'wallet_info': {'success': 0}}))

        with self.assertRaises(ApiException):
            market.buy_item('AK-47', '1', 100, 10, GameOptions.CS, Currency.USD)

    def test_get_my_market_listings_makes_one_request_with_expected_url_and_params(self):
        market = make_market(DummyResponse(json_data=make_listings_page([make_listing('1', '10')])))

        result = market.get_my_market_listings()

        market._request.assert_called_once_with(
            'GET', 'https://steamcommunity.com/market/mylistings', params=expected_params()
        )
        self.assertEqual(set(result), {'buy_orders', 'listings', 'listings_to_confirm'})

    def test_get_my_market_listings_returns_buy_orders_without_description(self):
        orders = [
            make_buy_order('1', appid=730),
            make_buy_order('2', appid=570),
            make_buy_order('3', appid=440),
        ]
        market = make_market(DummyResponse(json_data=make_listings_page(buy_orders=orders)))

        result = market.get_my_market_listings()

        self.assertEqual([order['buy_orderid'] for order in result['buy_orders']], ['1', '2', '3'])
        self.assertEqual([order['appid'] for order in result['buy_orders']], [730, 570, 440])
        self.assertEqual(
            result['buy_orders'],
            [{key: value for key, value in order.items() if key != 'description'} for order in orders],
        )
        for order in result['buy_orders']:
            self.assertNotIn('description', order)

    def test_get_my_market_listings_keeps_partly_filled_buy_order_quantities(self):
        order = make_buy_order('1', quantity='10', quantity_remaining='4')
        market = make_market(DummyResponse(json_data=make_listings_page(buy_orders=[order])))

        result = market.get_my_market_listings()

        self.assertEqual(result['buy_orders'][0]['quantity'], '10')
        self.assertEqual(result['buy_orders'][0]['quantity_remaining'], '4')

    def test_get_my_market_listings_returns_empty_lists_without_orders_or_listings(self):
        market = make_market(DummyResponse(json_data=make_listings_page()))

        result = market.get_my_market_listings()

        self.assertEqual(result, {'buy_orders': [], 'listings': [], 'listings_to_confirm': []})
        self.assertEqual(market._request.call_count, 1)

    def test_get_my_market_listings_returns_listings_to_confirm_without_another_request(self):
        awaiting = make_listing('9', '90')
        market = make_market(DummyResponse(json_data=make_listings_page(listings_to_confirm=[awaiting])))

        result = market.get_my_market_listings()

        self.assertEqual(result['listings_to_confirm'], [awaiting])
        self.assertEqual(result['listings'], [])
        self.assertEqual(market._request.call_count, 1)

    def test_get_my_market_listings_does_not_return_listings_on_hold(self):
        on_hold = make_listing('9', '90')
        market = make_market(DummyResponse(json_data=make_listings_page(listings_on_hold=[on_hold])))

        result = market.get_my_market_listings()

        self.assertNotIn('listings_on_hold', result)

    def test_get_my_market_listings_pages_through_all_listings(self):
        all_listings = [make_listing(str(i), str(1000 + i)) for i in range(250)]
        pages = [
            make_listings_page(all_listings[:100], total_count=250, buy_orders=[make_buy_order('first')]),
            make_listings_page(all_listings[100:200], total_count=250, start=100, buy_orders=[make_buy_order('second')]),
            make_listings_page(all_listings[200:], total_count=250, start=200, buy_orders=[make_buy_order('third')]),
        ]
        market = make_market(*[DummyResponse(json_data=page) for page in pages])

        result = market.get_my_market_listings()

        self.assertEqual([call.kwargs['params']['start'] for call in market._request.call_args_list], [0, 100, 200])
        self.assertEqual(result['listings'], all_listings)
        self.assertEqual([order['buy_orderid'] for order in result['buy_orders']], ['first'])

    def test_get_my_market_listings_makes_one_request_for_exactly_one_page(self):
        listings = [make_listing(str(i), str(1000 + i)) for i in range(100)]
        market = make_market(DummyResponse(json_data=make_listings_page(listings, total_count=100)))

        result = market.get_my_market_listings()

        self.assertEqual(result['listings'], listings)
        self.assertEqual(market._request.call_count, 1)

    def test_get_my_market_listings_raises_when_later_page_fails(self):
        first_page = make_listings_page([make_listing(str(i), str(1000 + i)) for i in range(100)], total_count=250)
        market = make_market(DummyResponse(json_data=first_page), DummyResponse(status_code=500))

        with self.assertRaises(ApiException) as context:
            market.get_my_market_listings()

        self.assertIn('500', str(context.exception))

    def test_get_my_market_listings_raises_when_later_page_has_no_listings(self):
        first_page = make_listings_page([make_listing(str(i), str(1000 + i)) for i in range(100)], total_count=250)
        empty_page = make_listings_page([], total_count=250, start=100)
        market = make_market(DummyResponse(json_data=first_page), DummyResponse(json_data=empty_page))

        with self.assertRaises(ApiException):
            market.get_my_market_listings()

    def test_get_my_market_listings_raises_for_invalid_first_reply(self):
        def page_without(key):
            page = make_listings_page()
            del page[key]
            return DummyResponse(json_data=page)

        cases = [
            ('status 500', DummyResponse(status_code=500, json_data=make_listings_page())),
            ('not json', DummyResponse(json_data=json.JSONDecodeError('bad', 'doc', 0))),
            ('null', DummyResponse(json_data=None)),
            ('list', DummyResponse(json_data=[])),
            ('success false', DummyResponse(json_data=make_listings_page(success=False))),
            ('success missing', page_without('success')),
            ('buy_orders missing', page_without('buy_orders')),
            ('buy_orders null', DummyResponse(json_data={**make_listings_page(), 'buy_orders': None})),
            ('listings missing', page_without('listings')),
            ('listings_to_confirm missing', page_without('listings_to_confirm')),
            ('total_count text', DummyResponse(json_data={**make_listings_page(), 'total_count': '100'})),
        ]

        for name, response in cases:
            with self.subTest(name):
                market = make_market(response)
                with self.assertRaises(ApiException):
                    market.get_my_market_listings()

    def test_get_my_market_listings_errors_do_not_include_the_reply(self):
        marker = 'made-up-marker-4c1f'
        page_with_marker = make_listings_page(
            buy_orders=[{**make_buy_order('1'), 'hash_name': marker}],
            listings=[{**make_listing('1', '10'), 'market_hash_name': marker}],
        )
        del page_with_marker['listings_to_confirm']
        cases = [
            ('marker in buy order and listing', DummyResponse(json_data=page_with_marker)),
            ('list reply', DummyResponse(json_data=[{'content': marker}])),
            ('total_count text', DummyResponse(json_data={**make_listings_page(), 'total_count': marker})),
            (
                'success false with long message',
                DummyResponse(json_data={**make_listings_page(success=False), 'message': 'x' * 5000}),
            ),
        ]

        for name, response in cases:
            with self.subTest(name):
                market = make_market(response)
                with self.assertRaises(ApiException) as context:
                    market.get_my_market_listings()

                message = str(context.exception)
                self.assertNotIn(marker, message)
                self.assertLess(len(message), 500)

    def test_get_my_market_listings_passes_language(self):
        market = make_market(DummyResponse(json_data=make_listings_page()))

        market.get_my_market_listings('german')

        market._request.assert_called_once_with(
            'GET', 'https://steamcommunity.com/market/mylistings', params=expected_params('german')
        )

    def test_create_buy_order_raises_with_steam_message_and_single_request(self):
        market = SteamMarket(MagicMock())
        market.was_login_executed = True
        market._session_id = 'sid'
        market._request = MagicMock(
            return_value=DummyResponse(json_data={'success': 25, 'message': 'There was a problem with your order'})
        )

        with self.assertRaises(ApiException) as context:
            market.create_buy_order('AK-47', '10.00', 2, GameOptions.CS, Currency.USD)

        self.assertIn('There was a problem with your order', str(context.exception))
        self.assertEqual(market._request.call_count, 1)
