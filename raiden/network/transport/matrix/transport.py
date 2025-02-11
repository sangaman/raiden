import json
import time
from collections import defaultdict
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import gevent
import structlog
from eth_utils import is_binary_address, to_checksum_address, to_normalized_address
from gevent.event import Event
from gevent.lock import Semaphore
from gevent.queue import JoinableQueue
from matrix_client.errors import MatrixRequestError

from raiden.constants import DISCOVERY_DEFAULT_ROOM, EMPTY_SIGNATURE
from raiden.exceptions import TransportError
from raiden.message_handler import MessageHandler
from raiden.messages.abstract import (
    Message,
    RetrieableMessage,
    SignedMessage,
    SignedRetrieableMessage,
)
from raiden.messages.healthcheck import Ping, Pong
from raiden.messages.matrix import ToDevice
from raiden.messages.synchronization import Delivered, Processed
from raiden.network.transport.matrix.client import GMatrixClient, Room, User
from raiden.network.transport.matrix.utils import (
    JOIN_RETRIES,
    AddressReachability,
    UserAddressManager,
    UserPresence,
    join_global_room,
    login_or_register,
    make_client,
    make_room_alias,
    validate_and_parse_message,
    validate_userid_signature,
)
from raiden.network.transport.utils import timeout_exponential_backoff
from raiden.storage.serialization.serializer import MessageSerializer
from raiden.transfer import views
from raiden.transfer.identifiers import CANONICAL_IDENTIFIER_GLOBAL_QUEUE, QueueIdentifier
from raiden.transfer.state import NetworkState, QueueIdsToQueues
from raiden.transfer.state_change import (
    ActionChangeNodeNetworkState,
    ActionUpdateTransportAuthData,
)
from raiden.utils.runnable import Runnable
from raiden.utils.typing import (
    MYPY_ANNOTATION,
    Address,
    AddressHex,
    Any,
    Callable,
    ChainID,
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    NewType,
    Optional,
    Tuple,
    Union,
    cast,
)

if TYPE_CHECKING:
    from raiden.raiden_service import RaidenService

log = structlog.get_logger(__name__)

_RoomID = NewType("_RoomID", str)
# Combined with 10 retries (``..utils.JOIN_RETRIES``) this will give a total wait time of ~15s
ROOM_JOIN_RETRY_INTERVAL = 0.1
ROOM_JOIN_RETRY_INTERVAL_MULTIPLIER = 1.55


class _RetryQueue(Runnable):
    """ A helper Runnable to send batched messages to receiver through transport """

    class _MessageData(NamedTuple):
        """ Small helper data structure for message queue """

        queue_identifier: QueueIdentifier
        message: Message
        text: str
        # generator that tells if the message should be sent now
        expiration_generator: Iterator[bool]

    def __init__(self, transport: "MatrixTransport", receiver: Address):
        self.transport = transport
        self.receiver = receiver
        self._message_queue: List[_RetryQueue._MessageData] = list()
        self._notify_event = gevent.event.Event()
        self._lock = gevent.lock.Semaphore()
        super().__init__()
        self.greenlet.name = f"RetryQueue recipient:{to_checksum_address(self.receiver)}"

    @property
    def log(self):
        return self.transport.log

    @staticmethod
    def _expiration_generator(
        timeout_generator: Iterable[float], now: Callable[[], float] = time.time
    ) -> Iterator[bool]:
        """Stateful generator that yields True if more than timeout has passed since previous True,
        False otherwise.

        Helper method to tell when a message needs to be retried (more than timeout seconds
        passed since last time it was sent).
        timeout is iteratively fetched from timeout_generator
        First value is True to always send message at least once
        """
        for timeout in timeout_generator:
            _next = now() + timeout  # next value is now + next generated timeout
            yield True
            while now() < _next:  # yield False while next is still in the future
                yield False

    def enqueue(self, queue_identifier: QueueIdentifier, message: Message):
        """ Enqueue a message to be sent, and notify main loop """
        assert queue_identifier.recipient == self.receiver
        with self._lock:
            already_queued = any(
                queue_identifier == data.queue_identifier and message == data.message
                for data in self._message_queue
            )
            if already_queued:
                self.log.warning(
                    "Message already in queue - ignoring",
                    receiver=to_checksum_address(self.receiver),
                    queue=queue_identifier,
                    message=message,
                )
                return
            timeout_generator = timeout_exponential_backoff(
                self.transport._config["retries_before_backoff"],
                self.transport._config["retry_interval"],
                self.transport._config["retry_interval"] * 10,
            )
            expiration_generator = self._expiration_generator(timeout_generator)
            self._message_queue.append(
                _RetryQueue._MessageData(
                    queue_identifier=queue_identifier,
                    message=message,
                    text=MessageSerializer.serialize(message),
                    expiration_generator=expiration_generator,
                )
            )
        self.notify()

    def enqueue_global(self, message: Message):
        """ Helper to enqueue a message in the global queue (e.g. Delivered) """
        self.enqueue(
            queue_identifier=QueueIdentifier(
                recipient=self.receiver, canonical_identifier=CANONICAL_IDENTIFIER_GLOBAL_QUEUE
            ),
            message=message,
        )

    def notify(self):
        """ Notify main loop to check if anything needs to be sent """
        with self._lock:
            self._notify_event.set()

    def _check_and_send(self):
        """Check and send all pending/queued messages that are not waiting on retry timeout

        After composing the to-be-sent message, also message queue from messages that are not
        present in the respective SendMessageEvent queue anymore
        """
        if not self.transport.greenlet:
            self.log.warning("Can't retry", reason="Transport not yet started")
            return
        if self.transport._stop_event.ready():
            self.log.warning("Can't retry", reason="Transport stopped")
            return

        if self.transport._prioritize_global_messages:
            # During startup global messages have to be sent first
            self.transport._global_send_queue.join()

        self.log.debug("Retrying message", receiver=to_checksum_address(self.receiver))
        status = self.transport._address_mgr.get_address_reachability(self.receiver)
        if status is not AddressReachability.REACHABLE:
            # if partner is not reachable, return
            self.log.debug(
                "Partner not reachable. Skipping.",
                partner=to_checksum_address(self.receiver),
                status=status,
            )
            return
        # sort output by channel_identifier (so global/unordered queue goes first)
        # inside queue, preserve order in which messages were enqueued
        ordered_queue = sorted(
            self._message_queue, key=lambda d: d.queue_identifier.canonical_identifier
        )
        message_texts = [
            data.text
            for data in ordered_queue
            # if expired_gen generator yields False, message was sent recently, so skip it
            if next(data.expiration_generator)
        ]

        def message_is_in_queue(data: _RetryQueue._MessageData) -> bool:
            return any(
                isinstance(data.message, RetrieableMessage)
                and send_event.message_identifier == data.message.message_identifier
                for send_event in self.transport._queueids_to_queues[data.queue_identifier]
            )

        # clean after composing, so any queued messages (e.g. Delivered) are sent at least once
        for msg_data in self._message_queue[:]:
            remove = False
            if isinstance(msg_data.message, (Delivered, Ping, Pong)):
                # e.g. Delivered, send only once and then clear
                # TODO: Is this correct? Will a missed Delivered be 'fixed' by the
                #       later `Processed` message?
                remove = True
            elif msg_data.queue_identifier not in self.transport._queueids_to_queues:
                remove = True
                self.log.debug(
                    "Stopping message send retry",
                    queue=msg_data.queue_identifier,
                    message=msg_data.message,
                    reason="Raiden queue is gone",
                )
            elif not message_is_in_queue(msg_data):
                remove = True
                self.log.debug(
                    "Stopping message send retry",
                    queue=msg_data.queue_identifier,
                    message=msg_data.message,
                    reason="Message was removed from queue",
                )

            if remove:
                self._message_queue.remove(msg_data)

        if message_texts:
            self.log.debug(
                "Send", receiver=to_checksum_address(self.receiver), messages=message_texts
            )
            self.transport._send_raw(self.receiver, "\n".join(message_texts))

    def _run(self):
        msg = f"_RetryQueue started before transport._raiden_service is set"
        assert self.transport._raiden_service is not None, msg
        self.greenlet.name = (
            f"RetryQueue "
            f"node:{to_checksum_address(self.transport._raiden_service.address)} "
            f"recipient:{to_checksum_address(self.receiver)}"
        )
        # run while transport parent is running
        while not self.transport._stop_event.ready():
            # once entered the critical section, block any other enqueue or notify attempt
            with self._lock:
                self._notify_event.clear()
                if self._message_queue:
                    self._check_and_send()
            # wait up to retry_interval (or to be notified) before checking again
            self._notify_event.wait(self.transport._config["retry_interval"])

    def __str__(self):
        return self.greenlet.name

    def __repr__(self):
        return f"<{self.__class__.__name__} for {to_normalized_address(self.receiver)}>"


class MatrixTransport(Runnable):
    _room_prefix = "raiden"
    _room_sep = "_"
    log = log

    def __init__(self, config: dict):
        super().__init__()
        self._uuid = uuid4()
        self._config = config
        self._raiden_service: Optional["RaidenService"] = None

        if config["server"] == "auto":
            available_servers = config["available_servers"]
        elif urlparse(config["server"]).scheme in {"http", "https"}:
            available_servers = [config["server"]]
        else:
            raise TransportError('Invalid matrix server specified (valid values: "auto" or a URL)')

        def _http_retry_delay() -> Iterable[float]:
            # below constants are defined in raiden.app.App.DEFAULT_CONFIG
            return timeout_exponential_backoff(
                config["retries_before_backoff"],
                config["retry_interval"] / 5,
                config["retry_interval"],
            )

        self._client: GMatrixClient = make_client(
            available_servers,
            http_pool_maxsize=4,
            http_retry_timeout=40,
            http_retry_delay=_http_retry_delay,
        )
        self._server_url = self._client.api.base_url
        self._server_name = config.get("server_name", urlparse(self._server_url).netloc)

        self.greenlets: List[gevent.Greenlet] = list()

        self._address_to_retrier: Dict[Address, _RetryQueue] = dict()

        self._global_rooms: Dict[str, Optional[Room]] = dict()
        self._global_send_queue: JoinableQueue[Tuple[str, Message]] = JoinableQueue()

        self._started = False
        self._starting = False

        self._stop_event = Event()
        self._stop_event.set()

        self._global_send_event = Event()
        self._prioritize_global_messages = True

        self._invite_queue: List[Tuple[_RoomID, dict]] = []

        self._address_mgr: UserAddressManager = UserAddressManager(
            client=self._client,
            get_user_callable=self._get_user,
            address_reachability_changed_callback=self._address_reachability_changed,
            user_presence_changed_callback=self._user_presence_changed,
            _log_context={"transport_uuid": str(self._uuid)},
        )

        self._client.add_invite_listener(self._handle_invite)
        self._client.add_listener(self._handle_to_device_message, event_type="to_device")

        self._health_lock = Semaphore()
        self._getroom_lock = Semaphore()
        self._account_data_lock = Semaphore()

        self._message_handler: Optional[MessageHandler] = None

    def __repr__(self):
        if self._raiden_service is not None:
            node = f" node:{to_checksum_address(self._raiden_service.address)}"
        else:
            node = ""

        return f"<{self.__class__.__name__}{node} id:{self._uuid}>"

    def start(  # type: ignore
        self,
        raiden_service: "RaidenService",
        message_handler: MessageHandler,
        prev_auth_data: Optional[str],
    ):
        if not self._stop_event.ready():
            raise RuntimeError(f"{self!r} already started")
        self.log.debug("Matrix starting")
        self._stop_event.clear()
        self._starting = True
        self._raiden_service = raiden_service
        self._message_handler = message_handler

        prev_user_id: Optional[str]
        prev_access_token: Optional[str]
        if prev_auth_data and prev_auth_data.count("/") == 1:
            prev_user_id, _, prev_access_token = prev_auth_data.partition("/")
        else:
            prev_user_id = prev_access_token = None

        self._address_mgr.start()
        login_or_register(
            client=self._client,
            signer=self._raiden_service.signer,
            prev_user_id=prev_user_id,
            prev_access_token=prev_access_token,
        )
        self.log = log.bind(
            current_user=self._user_id,
            node=to_checksum_address(self._raiden_service.address),
            transport_uuid=str(self._uuid),
        )

        self.log.debug("Start: handle greenlet", handle_greenlet=self._client._handle_thread)
        if self._client._handle_thread:
            # wait on _handle_thread for initial sync
            # this is needed so the rooms are populated before we _inventory_rooms
            self._client._handle_thread.get()

        for suffix in self._config["global_rooms"]:
            room_name = make_room_alias(self.chain_id, suffix)  # e.g. raiden_ropsten_discovery
            room = join_global_room(
                self._client, room_name, self._config.get("available_servers") or ()
            )
            self._global_rooms[room_name] = room

        self._inventory_rooms()

        def on_success(greenlet):
            if greenlet in self.greenlets:
                self.greenlets.remove(greenlet)

        self._client.start_listener_thread()
        assert isinstance(self._client.sync_thread, gevent.Greenlet)
        self._client.sync_thread.link_exception(self.on_error)
        self._client.sync_thread.link_value(on_success)
        self.greenlets = [self._client.sync_thread]
        self._schedule_new_greenlet(self._address_mgr.log_status_message)

        self._client.set_presence_state(UserPresence.ONLINE.value)

        # (re)start any _RetryQueue which was initialized before start
        for retrier in self._address_to_retrier.values():
            if not retrier:
                self.log.debug("Starting retrier", retrier=retrier)
                retrier.start()

        super().start()  # start greenlet
        self._starting = False
        self._started = True

        self.log.debug("Matrix started", config=self._config)

        # Handle any delayed invites in the future
        self._schedule_new_greenlet(self._process_queued_invites, in_seconds_from_now=1)

    def _process_queued_invites(self):
        if self._invite_queue:
            self.log.debug("Processing queued invites", queued_invites=len(self._invite_queue))
            for room_id, state in self._invite_queue:
                self._handle_invite(room_id, state)
            self._invite_queue.clear()

    def _run(self):
        """ Runnable main method, perform wait on long-running subtasks """
        # dispatch auth data on first scheduling after start
        state_change = ActionUpdateTransportAuthData(f"{self._user_id}/{self._client.api.token}")
        self.greenlet.name = (
            f"MatrixTransport._run node:{to_checksum_address(self._raiden_service.address)}"
        )
        self._raiden_service.handle_and_track_state_changes([state_change])
        try:
            # waits on _stop_event.ready()
            self._global_send_worker()
            # children crashes should throw an exception here
        except gevent.GreenletExit:  # killed without exception
            self._stop_event.set()
            gevent.killall(self.greenlets)  # kill children
            raise  # re-raise to keep killed status
        except Exception:
            self.stop()  # ensure cleanup and wait on subtasks
            raise

    def stop(self):
        """ Try to gracefully stop the greenlet synchronously

        Stop isn't expected to re-raise greenlet _run exception
        (use self.greenlet.get() for that),
        but it should raise any stop-time exception """
        if self._stop_event.ready():
            return
        self.log.debug("Matrix stopping")
        self._stop_event.set()
        self._global_send_event.set()

        for retrier in self._address_to_retrier.values():
            if retrier:
                retrier.notify()

        # Wait for retriers to exit, then discard them
        gevent.wait({r.greenlet for r in self._address_to_retrier.values()})
        self._address_to_retrier = {}

        self._address_mgr.stop()
        self._client.stop()  # stop sync_thread, wait on client's greenlets

        # wait on own greenlets, no need to get on them, exceptions should be raised in _run()
        gevent.wait(self.greenlets)

        self._client.set_presence_state(UserPresence.OFFLINE.value)

        # Ensure keep-alive http connections are closed
        self._client.api.session.close()

        self.log.debug("Matrix stopped", config=self._config)
        try:
            del self.log
        except AttributeError:
            # During shutdown the log attribute may have already been collected
            pass
        # parent may want to call get() after stop(), to ensure _run errors are re-raised
        # we don't call it here to avoid deadlock when self crashes and calls stop() on finally

    def whitelist(self, address: Address):
        """Whitelist peer address to receive communications from

        This may be called before transport is started, to ensure events generated during
        start are handled properly.
        """
        self.log.debug("Whitelist", address=to_checksum_address(address))
        self._address_mgr.add_address(address)

    def start_health_check(self, node_address):
        """Start healthcheck (status monitoring) for a peer

        It also whitelists the address to answer invites and listen for messages
        """
        self.whitelist(node_address)
        with self._health_lock:
            node_address_hex = to_normalized_address(node_address)
            self.log.debug("Healthcheck", peer_address=to_checksum_address(node_address))

            candidates = [
                self._get_user(user)
                for user in self._client.search_user_directory(node_address_hex)
            ]
            user_ids = {
                user.user_id
                for user in candidates
                if validate_userid_signature(user) == node_address
            }
            self._address_mgr.add_userids_for_address(node_address, user_ids)

            # Ensure network state is updated in case we already know about the user presences
            # representing the target node
            self._address_mgr.refresh_address_presence(node_address)

    def send_async(self, queue_identifier: QueueIdentifier, message: Message):
        """Queue the message for sending to recipient in the queue_identifier

        It may be called before transport is started, to initialize message queues
        The actual sending is started only when the transport is started
        """
        # even if transport is not started, can run to enqueue messages to send when it starts
        receiver_address = queue_identifier.recipient

        if not is_binary_address(receiver_address):
            raise ValueError("Invalid address {}".format(to_checksum_address(receiver_address)))

        # These are not protocol messages, but transport specific messages
        if isinstance(message, (Delivered, Ping, Pong)):
            raise ValueError(f"Do not use send_async for {message.__class__.__name__} messages")

        self.log.debug(
            "Send async",
            receiver_address=to_checksum_address(receiver_address),
            message=message,
            queue_identifier=queue_identifier,
        )

        self._send_with_retry(queue_identifier, message)

    def send_global(self, room: str, message: Message) -> None:
        """Sends a message to one of the global rooms

        These rooms aren't being listened on and therefore no reply could be heard, so these
        messages are sent in a send-and-forget async way.
        The actual room name is composed from the suffix given as parameter and chain name or id
        e.g.: raiden_ropsten_discovery
        Params:
            room: name suffix as passed in config['global_rooms'] list
            message: Message instance to be serialized and sent
        """
        self._global_send_queue.put((room, message))
        self._global_send_event.set()

    def _global_send_worker(self):
        def _send_global(room_name, serialized_message):
            if not any(suffix in room_name for suffix in self._config["global_rooms"]):
                raise RuntimeError(
                    f'Send global called on non-global room "{room_name}". '
                    f'Known global rooms: {self._config["global_rooms"]}.'
                )
            room_name = make_room_alias(self.chain_id, room_name)
            if room_name not in self._global_rooms:
                room = join_global_room(
                    self._client, room_name, self._config.get("available_servers") or ()
                )
                self._global_rooms[room_name] = room

            assert self._global_rooms.get(room_name), f"Unknown global room: {room_name!r}"

            room = self._global_rooms[room_name]
            self.log.debug(
                "Send global",
                room_name=room_name,
                room=room,
                data=serialized_message.replace("\n", "\\n"),
            )
            room.send_text(serialized_message)

        while not self._stop_event.ready():
            self._global_send_event.clear()
            messages: Dict[str, List[Message]] = defaultdict(list)
            while self._global_send_queue.qsize() > 0:
                room_name, message = self._global_send_queue.get()
                messages[room_name].append(message)
            for room_name, messages_for_room in messages.items():
                message_text = "\n".join(
                    MessageSerializer.serialize(message) for message in messages_for_room
                )
                _send_global(room_name, message_text)
                for _ in messages_for_room:
                    # Every message needs to be marked as done.
                    # Unfortunately there's no way to do that in one call :(
                    # https://github.com/gevent/gevent/issues/1436
                    self._global_send_queue.task_done()

            # Stop prioritizing global messages after initial queue has been emptied
            self._prioritize_global_messages = False
            self._global_send_event.wait(self._config["retry_interval"])

    @property
    def _queueids_to_queues(self) -> QueueIdsToQueues:
        assert self._raiden_service is not None, "_raiden_service not set"

        chain_state = views.state_from_raiden(self._raiden_service)
        return views.get_all_messagequeues(chain_state)

    @property
    def _user_id(self) -> Optional[str]:
        return getattr(self, "_client", None) and getattr(self._client, "user_id", None)

    @property
    def chain_id(self) -> ChainID:
        assert self._raiden_service is not None
        return self._raiden_service.rpc_client.chain_id

    @property
    def _private_rooms(self) -> bool:
        return bool(self._config.get("private_rooms"))

    def _inventory_rooms(self):
        self.log.debug("Inventory rooms", rooms=self._client.rooms)
        for room in self._client.rooms.values():
            room_aliases = set(room.aliases)
            if room.canonical_alias:
                room_aliases.add(room.canonical_alias)
            room_alias_is_global = any(
                global_alias in room_alias
                for global_alias in self._config["global_rooms"]
                for room_alias in room_aliases
            )
            if room_alias_is_global:
                continue
            # we add listener for all valid rooms, _handle_message should ignore them
            # if msg sender isn't whitelisted yet
            if not room.listeners:
                room.add_listener(self._handle_message, "m.room.message")
            self.log.debug(
                "Found room", room=room, aliases=room.aliases, members=room.get_joined_members()
            )

    def _handle_invite(self, room_id: _RoomID, state: dict):
        """ Join rooms invited by whitelisted partners """
        if self._stop_event.ready():
            return

        if self._starting:
            self.log.debug("Queueing invite", room_id=room_id)
            self._invite_queue.append((room_id, state))
            return

        self.log.debug("Got invite", room_id=room_id)
        invite_events = [
            event
            for event in state["events"]
            if event["type"] == "m.room.member"
            and event["content"].get("membership") == "invite"
            and event["state_key"] == self._user_id
        ]
        invite_event = invite_events[0]

        if not invite_events:
            self.log.debug("Invite: no invite event found", room_id=room_id)
            return  # there should always be one and only one invite membership event for us
        sender = invite_event["sender"]
        user = self._get_user(sender)
        peer_address = validate_userid_signature(user)

        if not peer_address:
            self.log.debug(
                "Got invited to a room by invalid signed user - ignoring",
                room_id=room_id,
                user=user,
            )
            return

        if not self._address_mgr.is_address_known(peer_address):
            self.log.debug(
                "Got invited by a non-whitelisted user - ignoring", room_id=room_id, user=user
            )
            return

        sender_join_events = [
            event
            for event in state["events"]
            if event["type"] == "m.room.member"
            and event["content"].get("membership") == "join"
            and event["state_key"] == sender
        ]

        if not sender_join_events:
            self.log.debug("Invite: no sender join event", room_id=room_id)
            return  # there should always be one and only one join membership event for the sender
        sender_join_event = sender_join_events[0]
        user.displayname = sender_join_event["content"].get("displayname") or user.displayname

        join_rules_events = [
            event for event in state["events"] if event["type"] == "m.room.join_rules"
        ]

        # room privacy as seen from the event
        private_room: bool = False
        if join_rules_events:
            join_rules_event = join_rules_events[0]
            private_room = join_rules_event["content"].get("join_rule") == "invite"

        # we join room and _set_room_id_for_address despite room privacy and requirements,
        # _get_room_ids_for_address will take care of returning only matching rooms and
        # _leave_unused_rooms will clear it in the future, if and when needed
        room: Optional[Room] = None
        last_ex: Optional[Exception] = None
        retry_interval = 0.1
        for _ in range(JOIN_RETRIES):
            try:
                room = self._client.join_room(room_id)
            except MatrixRequestError as e:
                last_ex = e
                if self._stop_event.wait(retry_interval):
                    break
                retry_interval = retry_interval * 2
            else:
                break
        else:
            assert last_ex is not None
            raise last_ex  # re-raise if couldn't succeed in retries

        assert room is not None, f"joining room {room} failed"

        if not room.listeners:
            room.add_listener(self._handle_message, "m.room.message")

        # room state may not populated yet, so we populate 'invite_only' from event
        room.invite_only = private_room

        self._set_room_id_for_address(address=peer_address, room_id=room_id)

        self.log.debug(
            "Joined from invite",
            room_id=room_id,
            aliases=room.aliases,
            inviting_address=to_checksum_address(peer_address),
        )

    def _handle_message(self, room, event) -> bool:
        """ Handle text messages sent to listening rooms """
        if (
            event["type"] != "m.room.message"
            or event["content"]["msgtype"] != "m.text"
            or self._stop_event.ready()
        ):
            # Ignore non-messages and non-text messages
            return False

        sender_id = event["sender"]

        if sender_id == self._user_id:
            # Ignore our own messages
            return False

        user = self._get_user(sender_id)
        peer_address = validate_userid_signature(user)
        if not peer_address:
            self.log.debug(
                "Message from invalid user displayName signature",
                peer_user=user.user_id,
                room=room,
            )
            return False

        # don't proceed if user isn't whitelisted (yet)
        if not self._address_mgr.is_address_known(peer_address):
            # user not whitelisted
            self.log.debug(
                "Message from non-whitelisted peer - ignoring",
                sender=user,
                sender_address=to_checksum_address(peer_address),
                room=room,
            )
            return False

        # rooms we created and invited user, or were invited specifically by them
        room_ids = self._get_room_ids_for_address(peer_address)

        # TODO: Remove clause after `and` and check if things still don't hang
        if room.room_id not in room_ids and (self._private_rooms and not room.invite_only):
            # this should not happen, but is not fatal, as we may not know user yet
            if self._private_rooms and not room.invite_only:
                reason = "required private room, but received message in a public"
            else:
                reason = "unknown room for user"
            self.log.debug(
                "Ignoring invalid message",
                peer_user=user.user_id,
                peer_address=to_checksum_address(peer_address),
                room=room,
                expected_room_ids=room_ids,
                reason=reason,
            )
            return False

        # TODO: With the condition in the TODO above restored this one won't have an effect, check
        #       if it can be removed after the above is solved
        if not room_ids or room.room_id != room_ids[0]:
            if self._is_room_global(room):
                # This must not happen. Nodes must not listen on global rooms.
                raise RuntimeError(f"Received message in global room {room.aliases}.")
            self.log.debug(
                "Received message triggered new comms room for peer",
                peer_user=user.user_id,
                peer_address=to_checksum_address(peer_address),
                known_user_rooms=room_ids,
                room=room,
            )
            self._set_room_id_for_address(peer_address, room.room_id)

        is_peer_reachable = self._address_mgr.get_address_reachability(peer_address) is (
            AddressReachability.REACHABLE
        )
        if not is_peer_reachable:
            self.log.debug(
                "Forcing presence update",
                peer_address=to_checksum_address(peer_address),
                user_id=sender_id,
            )
            self._address_mgr.force_user_presence(user, UserPresence.ONLINE)
            self._address_mgr.refresh_address_presence(peer_address)

        messages = validate_and_parse_message(event["content"]["body"], peer_address)

        if not messages:
            return False

        self.log.debug(
            "Incoming messages",
            messages=messages,
            sender=to_checksum_address(peer_address),
            sender_user=user,
            room=room,
        )

        for message in messages:
            if not isinstance(message, (SignedRetrieableMessage, SignedMessage)):
                self.log.warning("Received invalid message", message=message)
            if isinstance(message, Delivered):
                self._receive_delivered(message)
            elif isinstance(message, Processed):
                self._receive_message(message)
            else:
                assert isinstance(message, SignedRetrieableMessage)
                self._receive_message(message)

        return True

    def _receive_delivered(self, delivered: Delivered):
        assert delivered.sender is not None, MYPY_ANNOTATION
        self.log.debug(
            "Delivered message received",
            sender=to_checksum_address(delivered.sender),
            message=delivered,
        )

        assert self._raiden_service is not None
        self._raiden_service.on_message(delivered)

    def _receive_message(self, message: Union[SignedRetrieableMessage, Processed]):
        assert self._raiden_service is not None
        assert message.sender is not None, MYPY_ANNOTATION
        self.log.debug(
            "Message received",
            node=to_checksum_address(self._raiden_service.address),
            message=message,
            sender=to_checksum_address(message.sender),
        )

        # TODO: Maybe replace with Matrix read receipts.
        #       Unfortunately those work on an 'up to' basis, not on individual messages
        #       which means that message order is important which isn't guaranteed between
        #       federated servers.
        #       See: https://matrix.org/docs/spec/client_server/r0.3.0.html#id57
        delivered_message = Delivered(
            delivered_message_identifier=message.message_identifier, signature=EMPTY_SIGNATURE
        )
        self._raiden_service.sign(delivered_message)

        if message.sender:  # Ignore unsigned messages
            retrier = self._get_retrier(message.sender)
            retrier.enqueue_global(delivered_message)
            self._raiden_service.on_message(message)

    def _receive_to_device(self, to_device: ToDevice):
        assert to_device.sender is not None, MYPY_ANNOTATION
        self.log.debug(
            "ToDevice message received",
            sender=to_checksum_address(to_device.sender),
            message=to_device,
        )

    def _get_retrier(self, receiver: Address) -> _RetryQueue:
        """ Construct and return a _RetryQueue for receiver """
        if receiver not in self._address_to_retrier:
            retrier = _RetryQueue(transport=self, receiver=receiver)
            self._address_to_retrier[receiver] = retrier
            # Always start the _RetryQueue, otherwise `stop` will block forever
            # waiting for the corresponding gevent.Greenlet to complete. This
            # has no negative side-effects if the transport has stopped because
            # the retrier itself checks the transport running state.
            retrier.start()
            # ``Runnable.start()`` may re-create the internal greenlet
            retrier.greenlet.link_exception(self.on_error)
        return self._address_to_retrier[receiver]

    def _send_with_retry(self, queue_identifier: QueueIdentifier, message: Message):
        retrier = self._get_retrier(queue_identifier.recipient)
        retrier.enqueue(queue_identifier=queue_identifier, message=message)

    def _send_raw(self, receiver_address: Address, data: str):
        with self._getroom_lock:
            room = self._get_room_for_address(receiver_address)
        if not room:
            self.log.error("No room for receiver", receiver=to_checksum_address(receiver_address))
            return
        self.log.debug(
            "Send raw",
            receiver=to_checksum_address(receiver_address),
            room=room,
            data=data.replace("\n", "\\n"),
        )
        room.send_text(data)

    def _get_room_for_address(self, address: Address, allow_missing_peers=False) -> Optional[Room]:
        if self._stop_event.ready():
            return None
        address_hex = to_normalized_address(address)
        msg = f"address not health checked: me: {self._user_id}, peer: {address_hex}"
        assert address and self._address_mgr.is_address_known(address), msg

        # filter_private is done in _get_room_ids_for_address
        room_ids = self._get_room_ids_for_address(address)
        if room_ids:  # if we know any room for this user, use the first one
            # This loop is used to ignore any global rooms that may have 'polluted' the
            # user's room cache due to bug #3765
            # Can be removed after the next upgrade that switches to a new TokenNetworkRegistry
            while room_ids:
                room_id = room_ids.pop(0)
                room = self._client.rooms[room_id]
                if not self._is_room_global(room):
                    self.log.debug("Existing room", room=room, members=room.get_joined_members())
                    return room
                self.log.warning("Ignoring global room for peer", room=room, peer=address_hex)

        assert self._raiden_service is not None
        address_pair = sorted(
            [to_normalized_address(address) for address in [address, self._raiden_service.address]]
        )
        room_name = make_room_alias(self.chain_id, *address_pair)

        # no room with expected name => create one and invite peer
        peer_candidates = [
            self._get_user(user) for user in self._client.search_user_directory(address_hex)
        ]

        # filter peer_candidates
        peers = [user for user in peer_candidates if validate_userid_signature(user) == address]
        if not peers and not allow_missing_peers:
            self.log.error("No valid peer found", peer_address=to_checksum_address(address))
            return None

        if self._private_rooms:
            room = self._get_private_room(invitees=peers)
        else:
            room = self._get_public_room(room_name, invitees=peers)

        peer_ids = self._address_mgr.get_userids_for_address(address)
        member_ids = {member.user_id for member in room.get_joined_members(force_resync=True)}
        room_is_empty = not bool(peer_ids & member_ids)
        if room_is_empty:
            last_ex: Optional[Exception] = None
            retry_interval = ROOM_JOIN_RETRY_INTERVAL
            self.log.debug(
                "Waiting for peer to join from invite",
                room=room,
                peer_address=to_checksum_address(address),
            )
            for _ in range(JOIN_RETRIES):
                try:
                    member_ids = {
                        member.user_id for member in room.get_joined_members(force_resync=True)
                    }
                except MatrixRequestError as e:
                    last_ex = e
                room_is_empty = not bool(peer_ids & member_ids)
                if room_is_empty or last_ex:
                    if self._stop_event.wait(retry_interval):
                        break
                    retry_interval *= ROOM_JOIN_RETRY_INTERVAL_MULTIPLIER
                else:
                    break

            if room_is_empty or last_ex:
                if last_ex:
                    raise last_ex  # re-raise if couldn't succeed in retries
                else:
                    # Inform the client, that currently no one listens:
                    self.log.error(
                        "Peer has not joined from invite yet, should join eventually",
                        room=room,
                        peer_address=to_checksum_address(address),
                    )

        self._address_mgr.add_userids_for_address(address, {user.user_id for user in peers})
        self._set_room_id_for_address(address, room.room_id)

        if not room.listeners:
            room.add_listener(self._handle_message, "m.room.message")

        self.log.debug("Channel room", peer_address=to_checksum_address(address), room=room)
        return room

    def _is_room_global(self, room):
        return any(
            suffix in room_alias
            for suffix in self._config["global_rooms"]
            for room_alias in room.aliases
        )

    def _get_private_room(self, invitees: List[User]):
        """ Create an anonymous, private room and invite peers """
        room = self._client.create_room(
            None, invitees=[user.user_id for user in invitees], is_public=False
        )
        self.log.debug("Creating private room", room=room, invitees=invitees)
        return room

    def _get_public_room(self, room_name, invitees: List[User]):
        """ Obtain a public, canonically named (if possible) room and invite peers """
        room_name_full = f"#{room_name}:{self._server_name}"
        invitees_uids = [user.user_id for user in invitees]

        for _ in range(JOIN_RETRIES):
            # try joining room
            try:
                room = self._client.join_room(room_name_full)
            except MatrixRequestError as error:
                if error.code == 404:
                    self.log.debug(
                        f"No room for peer, trying to create",
                        room_name=room_name_full,
                        error=error,
                    )
                else:
                    self.log.debug(
                        f"Error joining room",
                        room_name=room_name,
                        error=error.content,
                        error_code=error.code,
                    )
            else:
                # Invite users to existing room
                member_ids = {user.user_id for user in room.get_joined_members(force_resync=True)}
                users_to_invite = set(invitees_uids) - member_ids
                self.log.debug("Inviting users", room=room, invitee_ids=users_to_invite)
                for invitee_id in users_to_invite:
                    room.invite_user(invitee_id)
                self.log.debug("Joined public room", room=room)
                break

            # if can't, try creating it
            try:
                room = self._client.create_room(room_name, invitees=invitees_uids, is_public=True)
            except MatrixRequestError as error:
                if error.code == 409:
                    msg = (
                        "Error creating room, "
                        "seems to have been created by peer meanwhile, retrying."
                    )
                else:
                    msg = "Error creating room, retrying."

                self.log.debug(
                    msg, room_name=room_name, error=error.content, error_code=error.code
                )
            else:
                self.log.debug("Room created successfully", room=room, invitees=invitees)
                break
        else:
            # if can't join nor create, create an unnamed one
            room = self._client.create_room(None, invitees=invitees_uids, is_public=True)
            self.log.warning(
                "Could not create nor join a named room. Successfuly created an unnamed one",
                room=room,
                invitees=invitees,
            )

        return room

    def _user_presence_changed(self, user: User, _presence: UserPresence):
        # maybe inviting user used to also possibly invite user's from presence changes
        assert self._raiden_service is not None  # make mypy happy
        greenlet = self._schedule_new_greenlet(self._maybe_invite_user, user)
        greenlet.name = (
            f"invite node:{to_checksum_address(self._raiden_service.address)} user:{user}"
        )

    def _address_reachability_changed(self, address: Address, reachability: AddressReachability):
        if reachability is AddressReachability.REACHABLE:
            node_reachability = NetworkState.REACHABLE
            # _QueueRetry.notify when partner comes online
            retrier = self._address_to_retrier.get(address)
            if retrier:
                retrier.notify()
        elif reachability is AddressReachability.UNKNOWN:
            node_reachability = NetworkState.UNKNOWN
        elif reachability is AddressReachability.UNREACHABLE:
            node_reachability = NetworkState.UNREACHABLE
        else:
            raise TypeError(f'Unexpected reachability state "{reachability}".')

        assert self._raiden_service is not None  # make mypy happy
        state_change = ActionChangeNodeNetworkState(address, node_reachability)
        self._raiden_service.handle_and_track_state_changes([state_change])

    def _maybe_invite_user(self, user: User):
        peer_address = validate_userid_signature(user)
        if not peer_address:
            return

        room_ids = self._get_room_ids_for_address(peer_address)
        if not room_ids:
            return

        room = self._client.rooms[room_ids[0]]
        if not room._members:
            room.get_joined_members(force_resync=True)
        if user.user_id not in room._members:
            self.log.debug(
                "Inviting", peer_address=to_checksum_address(peer_address), user=user, room=room
            )
            try:
                room.invite_user(user.user_id)
            except (json.JSONDecodeError, MatrixRequestError):
                self.log.warning(
                    "Exception inviting user, maybe their server is not healthy",
                    peer_address=to_checksum_address(peer_address),
                    user=user,
                    room=room,
                    exc_info=True,
                )

    def _sign(self, data: bytes) -> bytes:
        """ Use eth_sign compatible hasher to sign matrix data """
        assert self._raiden_service is not None
        return self._raiden_service.signer.sign(data=data)

    def _get_user(self, user: Union[User, str]) -> User:
        """Creates an User from an user_id, if none, or fetch a cached User

        As all users are supposed to be in discovery room, its members dict is used for caching"""
        user_id: str = getattr(user, "user_id", user)
        discovery_room = self._global_rooms.get(
            make_room_alias(self.chain_id, DISCOVERY_DEFAULT_ROOM)
        )
        if discovery_room and user_id in discovery_room._members:
            duser = discovery_room._members[user_id]
            # if handed a User instance with displayname set, update the discovery room cache
            if getattr(user, "displayname", None):
                assert isinstance(user, User)
                duser.displayname = user.displayname
            user = duser
        elif not isinstance(user, User):
            user = self._client.get_user(user_id)
        return user

    def _set_room_id_for_address(self, address: Address, room_id: Optional[_RoomID] = None):
        """ Uses GMatrixClient.set_account_data to keep updated mapping of addresses->rooms

        If room_id is falsy, clean list of rooms. Else, push room_id to front of the list """

        assert not room_id or room_id in self._client.rooms, "Invalid room_id"
        address_hex: AddressHex = to_checksum_address(address)
        # filter_private=False to preserve public rooms on the list, even if we require privacy
        room_ids = self._get_room_ids_for_address(address, filter_private=False)

        with self._account_data_lock:
            # no need to deepcopy, we don't modify lists in-place
            # cast generic Dict[str, Any] to types we expect, to satisfy mypy, runtime no-op
            _address_to_room_ids = cast(
                Dict[AddressHex, List[_RoomID]],
                self._client.account_data.get("network.raiden.rooms", {}).copy(),
            )

            changed = False
            if not room_id:  # falsy room_id => clear list
                changed = address_hex in _address_to_room_ids
                _address_to_room_ids.pop(address_hex, None)
            else:
                # push to front
                room_ids = [room_id] + [r for r in room_ids if r != room_id]
                if room_ids != _address_to_room_ids.get(address_hex):
                    _address_to_room_ids[address_hex] = room_ids
                    changed = True

            if changed:
                # dict will be set at the end of _clean_unused_rooms
                self._leave_unused_rooms(_address_to_room_ids)

    def _get_room_ids_for_address(
        self, address: Address, filter_private: bool = None
    ) -> List[_RoomID]:
        """ Uses GMatrixClient.get_account_data to get updated mapping of address->rooms

        It'll filter only existing rooms.
        If filter_private=True, also filter out public rooms.
        If filter_private=None, filter according to self._private_rooms
        """
        address_hex: AddressHex = to_checksum_address(address)
        with self._account_data_lock:
            room_ids = self._client.account_data.get("network.raiden.rooms", {}).get(address_hex)
            self.log.debug("Room ids for address", for_address=address_hex, room_ids=room_ids)
            if not room_ids:  # None or empty
                room_ids = list()
            if not isinstance(room_ids, list):  # old version, single room
                room_ids = [room_ids]

            if filter_private is None:
                filter_private = self._private_rooms
            if not filter_private:
                # existing rooms
                room_ids = [room_id for room_id in room_ids if room_id in self._client.rooms]
            else:
                # existing and private rooms
                room_ids = [
                    room_id
                    for room_id in room_ids
                    if room_id in self._client.rooms and self._client.rooms[room_id].invite_only
                ]

            return room_ids

    def _leave_unused_rooms(self, _address_to_room_ids: Dict[AddressHex, List[_RoomID]]):
        """
        Checks for rooms we've joined and which partner isn't health-checked and leave.

        **MUST** be called from a context that holds the `_account_data_lock`.
        """
        _msg = "_leave_unused_rooms called without account data lock"
        assert self._account_data_lock.locked(), _msg

        # TODO: To be implemented, see https://github.com/raiden-network/raiden/issues/3262
        self._client.set_account_data(
            "network.raiden.rooms",  # back from cast in _set_room_id_for_address
            cast(Dict[str, Any], _address_to_room_ids),
        )
        return

    def send_to_device(self, address: Address, message: Message) -> None:
        """ Sends send-to-device events to a all known devices of a peer without retries. """
        user_ids = self._address_mgr.get_userids_for_address(address)

        data = {user_id: {"*": MessageSerializer.serialize(message)} for user_id in user_ids}

        return self._client.api.send_to_device("m.to_device_message", data)

    def _handle_to_device_message(self, event):
        """
        Handles to_device_message sent to us.
        - validates peer_whitelisted
        - validates userid_signature
        Todo: Currently doesnt do anything but logging when a to device message is received.
        """
        sender_id = event["sender"]

        if (
            event["type"] != "m.to_device_message"
            or self._stop_event.ready()
            or sender_id == self._user_id
        ):
            # Ignore non-messages and our own messages
            return False

        user = self._get_user(sender_id)
        peer_address = validate_userid_signature(user)
        if not peer_address:
            self.log.debug(
                "To_device_message from invalid user displayName signature", peer_user=user.user_id
            )
            return False

        # don't proceed if user isn't whitelisted (yet)
        if not self._address_mgr.is_address_known(peer_address):
            # user not start_health_check'ed
            self.log.debug(
                "ToDevice Message from non-whitelisted peer - ignoring",
                sender=user,
                sender_address=to_checksum_address(peer_address),
            )
            return False

        is_peer_reachable = self._address_mgr.get_address_reachability(peer_address) is (
            AddressReachability.REACHABLE
        )

        if not is_peer_reachable:
            self.log.debug(
                "Forcing presence update",
                peer_address=to_checksum_address(peer_address),
                user_id=sender_id,
            )
            self._address_mgr.force_user_presence(user, UserPresence.ONLINE)
            self._address_mgr.refresh_address_presence(peer_address)

        messages = validate_and_parse_message(event["content"], peer_address)

        if not messages:
            return False

        self.log.debug(
            "Incoming ToDevice Messages",
            messages=messages,
            sender=to_checksum_address(peer_address),
            sender_user=user,
        )

        for message in messages:
            if isinstance(message, ToDevice):
                self._receive_to_device(message)
            else:
                log.warning(
                    "Received Message is not of type ToDevice, invalid",
                    message=message,
                    peer_address=to_checksum_address(peer_address),
                )
                continue

        return True
