import gevent
import pytest
import structlog
from eth_utils import to_checksum_address

from raiden.api.python import RaidenAPI
from raiden.messages.transfers import Unlock
from raiden.tests.utils.detect_failure import raise_on_failure
from raiden.tests.utils.events import search_for_item
from raiden.tests.utils.factories import make_secret_with_hash
from raiden.tests.utils.network import CHAIN
from raiden.tests.utils.protocol import WaitForMessage
from raiden.transfer.events import EventPaymentReceivedSuccess
from raiden.utils import wait_until
from raiden.utils.echo_node import EchoNode
from raiden.utils.typing import MYPY_ANNOTATION, List, Optional
from raiden.waiting import TransferWaitResult, wait_for_received_transfer_result

log = structlog.get_logger(__name__)


@raise_on_failure
@pytest.mark.parametrize("number_of_nodes", [3])
@pytest.mark.parametrize("number_of_tokens", [1])
@pytest.mark.parametrize("channels_per_node", [CHAIN])
@pytest.mark.parametrize("reveal_timeout", [15])
@pytest.mark.parametrize("settle_timeout", [120])
def test_echo_node_response(token_addresses, raiden_chain, retry_timeout):
    app0, app1, echo_app = raiden_chain
    token_address = token_addresses[0]
    registry_address = echo_app.raiden.default_registry.address

    echo_api = RaidenAPI(echo_app.raiden)
    echo_node = EchoNode(echo_api, token_address)

    message_handler = WaitForMessage()
    echo_app.raiden.message_handler = message_handler

    echo_node.ready.wait(timeout=30)
    assert echo_node.ready.is_set()

    transfer_timeout = 10

    wait_for = list()
    for num, app in enumerate([app0, app1]):
        amount = 1 + num
        identifier = 10 ** (num + 1)
        secret, secrethash = make_secret_with_hash()

        payment_status = RaidenAPI(app.raiden).transfer_async(
            registry_address=registry_address,
            token_address=token_address,
            amount=amount,
            target=echo_app.raiden.address,
            identifier=identifier,
            secret=secret,
            secrethash=secrethash,
        )

        wait = message_handler.wait_for_message(Unlock, {"secret": secret})
        wait_for.append((wait, app.raiden.address, amount, identifier))

        msg = (
            f"Transfer {identifier} from "
            f"{to_checksum_address(app.raiden.address)} to "
            f"{to_checksum_address(echo_app.raiden.address)} timed out after "
            f"{transfer_timeout}"
        )
        with gevent.Timeout(transfer_timeout, exception=RuntimeError(msg)):
            payment_status.payment_done.wait()

        echo_identifier = identifier + amount
        msg = (
            f"Response transfer {echo_identifier} from echo node "
            f"{to_checksum_address(echo_app.raiden.address)} to "
            f"{to_checksum_address(app.raiden.address)} timed out after "
            f"{transfer_timeout}"
        )

        with gevent.Timeout(transfer_timeout, exception=RuntimeError(msg)):
            result = wait_for_received_transfer_result(
                raiden=app.raiden,
                payment_identifier=echo_identifier,
                amount=amount,
                retry_timeout=retry_timeout,
                secrethash=secrethash,
            )
            assert result == TransferWaitResult.UNLOCKED

    for wait, sender, amount, ident in wait_for:
        wait.wait()
        assert search_for_item(
            echo_app.raiden.wal.storage.get_events(),
            EventPaymentReceivedSuccess,
            {
                "amount": amount,
                "identifier": ident,
                "initiator": sender,
                "token_network_registry_address": registry_address,
            },
        )

    echo_node.stop()


def transfer_and_await(app, token_address, target, amount, identifier, timeout):
    payment_status = RaidenAPI(app.raiden).transfer_async(
        registry_address=app.raiden.default_registry.address,
        token_address=token_address,
        amount=amount,
        target=target,
        identifier=identifier,
    )

    msg = (
        f"Transfer {identifier} from "
        f"{to_checksum_address(app.raiden.address)} to "
        f"{to_checksum_address(target)} timed out after "
        f"{timeout}"
    )
    with gevent.Timeout(timeout, exception=RuntimeError(msg)):
        payment_status.payment_done.wait()


@raise_on_failure
@pytest.mark.parametrize("number_of_nodes", [8])
@pytest.mark.parametrize("number_of_tokens", [1])
@pytest.mark.parametrize("channels_per_node", [CHAIN])
@pytest.mark.parametrize("reveal_timeout", [15])
@pytest.mark.parametrize("settle_timeout", [120])
@pytest.mark.skip("https://github.com/raiden-network/raiden/issues/3750")
def test_echo_node_lottery(token_addresses, raiden_chain, network_wait):
    app0, app1, app2, app3, echo_app, app4, app5, app6 = raiden_chain
    address_to_app = {app.raiden.address: app for app in raiden_chain}
    token_address = token_addresses[0]
    echo_api = RaidenAPI(echo_app.raiden)

    echo_node = EchoNode(echo_api, token_address)
    echo_node.ready.wait(timeout=30)
    assert echo_node.ready.is_set()

    transfer_timeout = 10

    # Let 6 participants enter the pool
    amount = 7
    for num, app in enumerate([app0, app1, app2, app3, app4, app5]):
        identifier = 100 * num
        transfer_and_await(
            app=app,
            token_address=token_address,
            target=echo_app.raiden.address,
            amount=amount,
            identifier=identifier,
            timeout=transfer_timeout,
        )

    # test duplicated identifier + amount is ignored
    transfer_and_await(
        app=app5,
        token_address=token_address,
        target=echo_app.raiden.address,
        amount=amount,
        identifier=500,  # app5 used this identifier before
        timeout=transfer_timeout,
    )

    # test pool size querying
    pool_query_identifier = 77  # unused identifier different from previous one
    transfer_and_await(
        app=app5,
        token_address=token_address,
        target=echo_app.raiden.address,
        amount=amount,
        identifier=pool_query_identifier,
        timeout=transfer_timeout,
    )

    # fill the pool
    transfer_and_await(
        app=app6,
        token_address=token_address,
        target=echo_app.raiden.address,
        amount=amount,
        identifier=600,
        timeout=transfer_timeout,
    )

    def get_echoed_transfer(sent_transfer) -> Optional[EventPaymentReceivedSuccess]:
        """For a given transfer sent to echo node, get the corresponding echoed transfer"""
        app = address_to_app[sent_transfer.initiator]
        events = RaidenAPI(app.raiden).get_raiden_events_payment_history(
            token_address=token_address
        )
        for event in events:
            if not type(event) == EventPaymentReceivedSuccess:
                continue
            assert isinstance(event, EventPaymentReceivedSuccess), MYPY_ANNOTATION
            if not (
                event.initiator == echo_app.raiden.address
                and event.identifier == sent_transfer.identifier + event.amount
            ):
                continue

            return event

        return None

    def received_events_when_len(size: int) -> Optional[List[EventPaymentReceivedSuccess]]:
        """Return transfers received from echo_node when there's size transfers"""
        received_events = []
        # Check that payout was generated and pool_size_query answered
        for handled_transfer in echo_node.seen_transfers:
            event = get_echoed_transfer(handled_transfer)
            if not event:
                continue

            received_events.append(event)

        log.debug(
            "Checking number of received events",
            received_events=received_events,
            expected_size=size,
            actual_size=len(received_events),
            seen_transfers=echo_node.seen_transfers,
            received_transfers=received_events,
        )
        if len(received_events) == size:
            return received_events

        return None

    # wait for the expected echoed transfers to be handled
    received = wait_until(func=lambda: received_events_when_len(2), wait_for=network_wait)
    assert received

    received.sort(key=lambda transfer: transfer.amount)

    pool_query = received[0]
    assert pool_query.amount == 6
    assert pool_query.identifier == pool_query_identifier + 6

    winning_transfer = received[1]
    assert winning_transfer.initiator == echo_app.raiden.address
    assert winning_transfer.amount == 49
    assert (winning_transfer.identifier - 49) % 10 == 0

    echo_node.stop()
