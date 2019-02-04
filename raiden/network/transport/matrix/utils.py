import re
from collections import OrderedDict
from random import Random
from typing import Sequence
from urllib.parse import urlparse

import structlog
from eth_utils import encode_hex, to_normalized_address
from matrix_client.errors import MatrixRequestError

from raiden.exceptions import TransportError
from raiden.utils.signer import Signer
from raiden.network.transport.matrix.client import GMatrixClient, Room, User

log = structlog.get_logger(__name__)

JOIN_RETRIES = 5


def join_global_room(client: GMatrixClient, name: str, servers: Sequence[str] = ()) -> Room:
    """Join or create a global public room with given name

    First, try to join room on own server (client-configured one)
    If can't, try to join on each one of servers, and if able, alias it in our server
    If still can't, create a public room with name in our server

    Params:
        client: matrix-python-sdk client instance
        name: name or alias of the room (without #-prefix or server's name suffix)
        servers: optional: sequence of known/available servers to try to find the room in
    Returns:
        matrix's Room instance linked to client
    """
    assert urlparse(client.api.base_url).netloc, 'Invalid client\'s homeserver url'
    servers = [
        urlparse(s).netloc
        for s in ([client.api.base_url] + list(servers))  # client's own server first
        if urlparse(s).netloc
    ]
    servers = list(OrderedDict.fromkeys(servers))  # dedupe, keep order

    our_server_global_room_alias_full = f'#{name}:{servers[0]}'

    # try joining a global room on any of the available servers, starting with ours
    for server in servers:
        global_room_alias_full = f'#{name}:{server}'
        try:
            global_room = client.join_room(global_room_alias_full)
        except MatrixRequestError as ex:
            if ex.code not in (403, 404, 500):
                raise
            log.debug(
                'Could not join global room',
                room_alias_full=global_room_alias_full,
                _exception=ex,
            )
        else:
            if our_server_global_room_alias_full not in global_room.aliases:
                # we managed to join a global room, but it's not aliased in our server
                global_room.add_room_alias(our_server_global_room_alias_full)
                global_room.aliases.append(our_server_global_room_alias_full)
            break
    else:
        log.debug('Could not join any global room, trying to create one')
        for _ in range(JOIN_RETRIES):
            try:
                global_room = client.create_room(name, is_public=True)
            except MatrixRequestError as ex:
                if ex.code not in (400, 409):
                    raise
                try:
                    global_room = client.join_room(
                        our_server_global_room_alias_full,
                    )
                except MatrixRequestError as ex:
                    if ex.code not in (404, 403):
                        raise
                else:
                    break
            else:
                break
        else:
            raise TransportError('Could neither join nor create a global room')

    return global_room


def login_or_register(
        client: GMatrixClient,
        signer: Signer,
        prev_user_id: str = None,
        prev_access_token: str = None,
) -> User:
    """Login to a Raiden matrix server with password and displayname proof-of-keys

    - Username is in the format: 0x<eth_address>(.<suffix>)?, where the suffix is not required,
    but a deterministic (per-account) random 8-hex string to prevent DoS by other users registering
    our address
    - Password is the signature of the server hostname, verified by the server to prevent account
    creation spam
    - Displayname currently is the signature of the whole user_id (including homeserver), to be
    verified by other peers. May include in the future other metadata such as protocol version

    Params:
        client: GMatrixClient instance configured with desired homeserver
        signer: raiden.utils.signer.Signer instance for signing password and displayname
        prev_user_id: (optional) previous persisted client.user_id. Must match signer's account
        prev_access_token: (optional) previous persistend client.access_token for prev_user_id
    Returns:
        Own matrix_client.User
    """
    server_url = client.api.base_url
    server_name = urlparse(server_url).netloc

    base_username = to_normalized_address(signer.address)
    _match_user = re.match(
        f'^@{re.escape(base_username)}.*:{re.escape(server_name)}$',
        prev_user_id or '',
    )
    if _match_user:  # same user as before
        log.debug('Trying previous user login', user_id=prev_user_id)
        client.set_access_token(user_id=prev_user_id, token=prev_access_token)

        try:
            client.api.get_devices()
        except MatrixRequestError as ex:
            log.debug(
                'Couldn\'t use previous login credentials, discarding',
                prev_user_id=prev_user_id,
                _exception=ex,
            )
        else:
            prev_sync_limit = client.set_sync_limit(0)
            client._sync()  # initial_sync
            client.set_sync_limit(prev_sync_limit)
            log.debug('Success. Valid previous credentials', user_id=prev_user_id)
            return client.get_user(client.user_id)
    elif prev_user_id:
        log.debug(
            'Different server or account, discarding',
            prev_user_id=prev_user_id,
            current_address=base_username,
            current_server=server_name,
        )

    # password is signed server address
    password = encode_hex(signer.sign(server_name.encode()))
    rand = None
    # try login and register on first 5 possible accounts
    for i in range(JOIN_RETRIES):
        username = base_username
        if i:
            if not rand:
                rand = Random()  # deterministic, random secret for username suffixes
                # initialize rand for seed (which requires a signature) only if/when needed
                rand.seed(int.from_bytes(signer.sign(b'seed')[-32:], 'big'))
            username = f'{username}.{rand.randint(0, 0xffffffff):08x}'

        try:
            client.login(username, password, sync=False)
            prev_sync_limit = client.set_sync_limit(0)
            client._sync()  # when logging, do initial_sync with limit=0
            client.set_sync_limit(prev_sync_limit)
            log.debug(
                'Login',
                homeserver=server_name,
                server_url=server_url,
                username=username,
            )
            break
        except MatrixRequestError as ex:
            if ex.code != 403:
                raise
            log.debug(
                'Could not login. Trying register',
                homeserver=server_name,
                server_url=server_url,
                username=username,
            )
            try:
                client.register_with_password(username, password)
                log.debug(
                    'Register',
                    homeserver=server_name,
                    server_url=server_url,
                    username=username,
                )
                break
            except MatrixRequestError as ex:
                if ex.code != 400:
                    raise
                log.debug('Username taken. Continuing')
                continue
    else:
        raise ValueError('Could not register or login!')

    name = encode_hex(signer.sign(client.user_id.encode()))
    user = client.get_user(client.user_id)
    user.set_display_name(name)
    return user