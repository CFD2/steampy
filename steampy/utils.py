import os
import copy
import json
import math
import struct
import time
from functools import wraps
from typing import List
from decimal import Decimal
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from requests.exceptions import RequestException
from requests.structures import CaseInsensitiveDict

from steampy.models import GameOptions
from steampy.models import DEFAULT_USER_AGENT
from steampy.exceptions import ApiException, ProxyConnectionError, LoginRequired


def login_required(func):
    @wraps(func)
    def func_wrapper(self, *args, **kwargs):
        if not self.was_login_executed:
            raise LoginRequired('Use login method first')
        else:
            return func(self, *args, **kwargs)

    return func_wrapper


def text_between(text: str, begin: str, end: str) -> str:
    start = text.index(begin) + len(begin)
    end = text.index(end, start)
    return text[start:end]


def texts_between(text: str, begin: str, end: str):
    stop = 0
    while True:
        try:
            start = text.index(begin, stop) + len(begin)
            stop = text.index(end, start)
            yield text[start:stop]
        except ValueError:
            return


def account_id_to_steam_id(account_id: str) -> str:
    first_bytes = int(account_id).to_bytes(4, byteorder='big')
    last_bytes = 0x1100001.to_bytes(4, byteorder='big')
    return str(struct.unpack('>Q', last_bytes + first_bytes)[0])


def steam_id_to_account_id(steam_id: str) -> str:
    return str(struct.unpack('>L', int(steam_id).to_bytes(8, byteorder='big')[4:])[0])


def calculate_gross_price(price_net: Decimal, publisher_fee: Decimal, steam_fee: Decimal = Decimal('0.05')) -> Decimal:
    """Calculate the price including the publisher's fee and the Steam fee.

    Arguments:
        price_net (Decimal): The amount that the seller receives after a market transaction.
        publisher_fee (Decimal): The Publisher Fee is a game specific fee that is determined and collected by the game
            publisher. Most publishers have a `10%` fee - `Decimal('0.10')` with a minimum fee of `$0.01`.
        steam_fee (Decimal): The Steam Transaction Fee is collected by Steam and is used to protect against nominal
            fraud incidents and cover the cost of development of this and future Steam economy features. The fee is
            currently `5%` (with a minimum fee of `$0.01`). This fee may be increased or decreased by Steam in the
            future.
    Returns:
        Decimal: Gross price (including fees) - the amount that the buyer pays during a market transaction
    """
    price_net *= 100
    steam_fee_amount = int(math.floor(max(price_net * steam_fee, 1)))
    publisher_fee_amount = int(math.floor(max(price_net * publisher_fee, 1)))
    price_gross = price_net + steam_fee_amount + publisher_fee_amount
    return Decimal(price_gross) / 100


def calculate_net_price(price_gross: Decimal, publisher_fee: Decimal, steam_fee: Decimal = Decimal('0.05')) -> Decimal:
    """Calculate the price without the publisher's fee and the Steam fee.

    Arguments:
        price_gross (Decimal): The amount that the buyer pays during a market transaction.
        publisher_fee (Decimal): The Publisher Fee is a game specific fee that is determined and collected by the game
            publisher. Most publishers have a `10%` fee - `Decimal('0.10')` with a minimum fee of `$0.01`.
        steam_fee (Decimal): The Steam Transaction Fee is collected by Steam and is used to protect against nominal
            fraud incidents and cover the cost of development of this and future Steam economy features. The fee is
            currently `5%` (with a minimum fee of `$0.01`). This fee may be increased or decreased by Steam in the
            future.
    Returns:
        Decimal: Net price (without fees) - the amount that the seller receives after a market transaction.
    """
    price_gross *= 100
    estimated_net_price = Decimal(int(price_gross / (steam_fee + publisher_fee + 1)))
    estimated_gross_price = calculate_gross_price(estimated_net_price / 100, publisher_fee, steam_fee) * 100

    # Since calculate_gross_price has a math.floor, we could be off a cent or two. Let's check:
    iterations = 0  # Shouldn't be needed, but included to be sure nothing unforeseen causes us to get stuck
    ever_undershot = False
    while estimated_gross_price != price_gross and iterations < 10:
        if estimated_gross_price > price_gross:
            if ever_undershot:
                break
            estimated_net_price -= 1
        else:
            ever_undershot = True
            estimated_net_price += 1

        estimated_gross_price = calculate_gross_price(estimated_net_price / 100, publisher_fee, steam_fee) * 100
        iterations += 1
    return estimated_net_price / 100


def merge_items_with_descriptions_from_inventory(inventory_response: dict, game: GameOptions) -> dict:
    inventory = inventory_response.get('assets', [])
    if not inventory:
        return {}
    descriptions = {get_description_key(description): description for description in inventory_response['descriptions']}
    return merge_items(inventory, descriptions, context_id=game.context_id)


def merge_items_with_descriptions_from_offers(offers_response: dict) -> dict:
    descriptions = {get_description_key(offer): offer for offer in offers_response['response'].get('descriptions', [])}
    received_offers = offers_response['response'].get('trade_offers_received', [])
    sent_offers = offers_response['response'].get('trade_offers_sent', [])
    offers_response['response']['trade_offers_received'] = list(
        map(lambda offer: merge_items_with_descriptions_from_offer(offer, descriptions), received_offers)
    )
    offers_response['response']['trade_offers_sent'] = list(
        map(lambda offer: merge_items_with_descriptions_from_offer(offer, descriptions), sent_offers)
    )
    return offers_response


def merge_items_with_descriptions_from_offer(offer: dict, descriptions: dict) -> dict:
    merged_items_to_give = merge_items(offer.get('items_to_give', []), descriptions)
    merged_items_to_receive = merge_items(offer.get('items_to_receive', []), descriptions)
    offer['items_to_give'] = merged_items_to_give
    offer['items_to_receive'] = merged_items_to_receive
    return offer


def merge_items(items: List[dict], descriptions: dict, **kwargs) -> dict:
    merged_items = {}

    for item in items:
        description_key = get_description_key(item)
        description = copy.copy(descriptions[description_key])
        item_id = item.get('id') or item['assetid']
        description['contextid'] = item.get('contextid') or kwargs['context_id']
        description['id'] = item_id
        description['amount'] = item['amount']
        merged_items[item_id] = description

    return merged_items


def get_wallet_details_from_html(html: str) -> dict:
    document = BeautifulSoup(html, 'html.parser')
    script = document.find('script', id='valve-ssr-data')
    if script is None:
        raise ApiException('Unable to find valve-ssr-data script on market page')

    try:
        ssr_data = json.loads(script.string or '')
    except json.JSONDecodeError as exc:
        raise ApiException('Unable to parse valve-ssr-data JSON from market page') from exc

    render_context = ssr_data.get('renderContext') if isinstance(ssr_data, dict) else None
    query_data_raw = render_context.get('queryData') if isinstance(render_context, dict) else None
    if not isinstance(query_data_raw, str):
        raise ApiException('Unable to find renderContext.queryData on market page')

    try:
        query_data = json.loads(query_data_raw)
    except json.JSONDecodeError as exc:
        raise ApiException('Unable to parse renderContext.queryData JSON from market page') from exc

    queries = query_data.get('queries') if isinstance(query_data, dict) else None
    if not isinstance(queries, list):
        raise ApiException('Unable to find queries in market page data')

    for query in queries:
        if not isinstance(query, dict):
            continue
        query_key = query.get('queryKey')
        if isinstance(query_key, list) and query_key and query_key[0] == 'CurrentUserWalletDetails':
            state = query.get('state')
            data = state.get('data') if isinstance(state, dict) else None
            if not isinstance(data, dict):
                raise ApiException('Wallet details data is missing on market page')
            return data

    raise ApiException('Unable to find wallet details in market page data')


def get_description_key(item: dict) -> str:
    return f'{item["classid"]}_{item["instanceid"]}'


def get_key_value_from_url(url: str, key: str, case_sensitive: bool = True) -> str:
    params = urlparse(url).query
    return parse_qs(params)[key][0] if case_sensitive else CaseInsensitiveDict(parse_qs(params))[key][0]


def load_credentials():
    dirname = os.path.dirname(os.path.abspath(__file__))
    with open(f'{dirname}/../secrets/credentials.pwd', 'r') as f:
        return [Credentials(line.split()[0], line.split()[1], line.split()[2]) for line in f]


class Credentials:
    def __init__(self, login: str, password: str, api_key: str):
        self.login = login
        self.password = password
        self.api_key = api_key


def ping_proxy(proxies: dict):
    attempts = 3
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            requests.get(
                'https://steamcommunity.com/',
                proxies=proxies,
                timeout=10,
                headers={'User-Agent': DEFAULT_USER_AGENT},
            )
            return True
        except RequestException as exc:
            last_exc = exc
            if attempt == attempts:
                break
            time.sleep(attempt)
    raise ProxyConnectionError(
        f'Proxy not working for steamcommunity.com after {attempts} attempts. proxies={proxies}. error={last_exc}'
    ) from last_exc


def create_cookie(name: str, cookie: str, domain: str) -> dict:
    return {'name': name, 'value': cookie, 'domain': domain}
