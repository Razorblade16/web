'''
    Copyright (C) 2017 Gitcoin Core

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program. If not, see <http://www.gnu.org/licenses/>.

'''

import datetime
import logging
import warnings
import requests
import json
import web3

from django.core.management.base import BaseCommand
from django.conf import settings
from django.db.models import Avg, Max, Min

from kudos.utils import KudosContract
from kudos.models import KudosTransfer, Token

warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("web3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class Command(BaseCommand):

    help = 'syncs database with kudos on the blockchain'

    def add_arguments(self, parser):
        parser.add_argument('network', type=str, choices=['localhost', 'rinkeby', 'mainnet'],
                            help='ethereum network to use')
        parser.add_argument('syncmethod', type=str, choices=['filter', 'id', 'opensea'],
                            help='sync method to use')
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('-s', '--start', type=str,
                           help='kudos_id to or kudos block to start syncing at.  Lowest kudos_id is 1.\
                           Options for block are: block number (int), "earliest", or "latest"')
        group.add_argument('-r', '--rewind', type=int,
                           help='Sync the lastest <rewind> Kudos Ids or block transactions.')
        group.add_argument('--catchup', action='store_true',
                           help='Attempt to sync up the newest kudos to the database')

    def opensea_sync(self, kudos_contract, start_id):
        if kudos_contract.network == 'rinkeby':
            url = 'https://rinkeby-api.opensea.io/api/v1/events'
        elif kudos_contract.network == 'mainnet':
            url = 'https://api.opensea.io/api/v1/events'
        else:
            raise RuntimeError('The Open Sea API is only supported for contracts on rinkeby and mainnet.')

        end_id = kudos_contract._contract.functions.totalSupply().call()
        token_ids = range(start_id, end_id + 1)

        headers = {'X-API-KEY': settings.OPENSEA_API_KEY}

        # Event API
        for token_id in token_ids:
            payload = dict(
                asset_contract_address=kudos_contract.address,
                token_id=token_id,
                )
            r = requests.get(url, params=payload, headers=headers)
            r.raise_for_status()
            asset_token_id = r.json()['asset_events'][0]['asset']['token_id']
            transaction_hash = r.json()['asset_events'][0]['transaction']['transaction_hash']
            logger.info(f'token_id: {asset_token_id}, txid: {transaction_hash}')
            kudos_contract.sync_db(kudos_id=int(asset_token_id), txid=transaction_hash)

    def filter_sync(self, kudos_contract, fromBlock):
        event_filter = kudos_contract._contract.events.Transfer.createFilter(fromBlock=fromBlock)
        for event in event_filter.get_all_entries():
            msg = dict(blockNumber=event.blockNumber,
                       _tokenId=event.args._tokenId,
                       transactionHash=event.transactionHash.hex()
                       )
            logger.info(f'Transfer event:  {msg}')
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f'Raw Transfer event: {event}')
            kudos_contract.sync_db(kudos_id=event.args._tokenId, txid=event.transactionHash.hex())

    def id_sync(self, kudos_contract, start_id):
        # iterate through all the kudos
        # kudos_contract.reconcile_db(start_id=start_id)
        end_id = kudos_contract._contract.functions.totalSupply().call()
        kudos_enum = start_id
        more_kudos = True

        while more_kudos:
            kudos_contract.sync_db_without_txid(kudos_id=kudos_enum)
            kudos_enum += 1

            if kudos_enum > end_id:
                more_kudos = False

    def handle(self, *args, **options):
        # config
        network = options['network']
        syncmethod = options['syncmethod']

        start = options['start']
        rewind = options['rewind']
        catchup = options['catchup']

        kudos_contract = KudosContract(network, sockets=True)

        # Handle the filter sync
        if syncmethod == 'filter':
            if start:
                if start.isdigit():
                    raise RuntimeError('This option is unstable if not on web3py 4.7.2.  May crash testrpc.')
                if start in ['earliest', 'latest']:
                    fromBlock = start
                else:
                    raise ValueError('--fromBlock must be "earliest", or "latest"')
            elif rewind:
                if web3.__version__ != '4.7.2':
                    raise RuntimeError('This option is unstable if not on web3py 4.7.2.  May crash testrpc.')
                fromBlock = kudos_contract._w3.eth.getBlock('latest')['number'] - rewind
            elif catchup:
                raise ValueError('--catchup option is not valid for filter syncing')

            logger.info(fromBlock)
            self.filter_sync(kudos_contract, fromBlock)
            return

        # Handle the other sync methods
        if start:
            start_id = start
        elif rewind:
            start_id = kudos_contract._contract.functions.totalSupply().call() - rewind
        elif catchup:
            start_id = Token.objects.aggregate(Max('id'))['id__max']

        if syncmethod == 'id':
            self.id_sync(kudos_contract, int(start_id))
        elif syncmethod == 'opensea':
            self.opensea_sync(kudos_contract, int(start_id))
        # return