import pytest
from eth_utils import to_canonical_address, to_checksum_address

from raiden.constants import (
    EMPTY_ADDRESS,
    GENESIS_BLOCK_NUMBER,
    SECONDS_PER_DAY,
    UINT256_MAX,
    Environment,
)
from raiden.network.proxies.proxy_manager import ProxyManager, ProxyManagerMetadata
from raiden.network.proxies.secret_registry import SecretRegistry
from raiden.network.proxies.token import Token
from raiden.settings import MONITORING_REWARD
from raiden.tests.utils.smartcontracts import (
    deploy_contract_web3,
    deploy_token,
    deploy_tokens_and_fund_accounts,
)
from raiden.utils import privatekey_to_address, typing
from raiden.utils.typing import Optional
from raiden_contracts.constants import (
    CONTRACT_CUSTOM_TOKEN,
    CONTRACT_ONE_TO_N,
    CONTRACT_SECRET_REGISTRY,
    CONTRACT_SERVICE_REGISTRY,
    CONTRACT_TOKEN_NETWORK_REGISTRY,
    CONTRACT_USER_DEPOSIT,
)

RED_EYES_PER_CHANNEL_PARTICIPANT_LIMIT = int(0.075 * 10 ** 18)
RED_EYES_PER_TOKEN_NETWORK_LIMIT = int(250 * 10 ** 18)


@pytest.fixture
def token_contract_name() -> str:
    return CONTRACT_CUSTOM_TOKEN


@pytest.fixture(name="token_addresses")
def deploy_all_tokens_register_and_return_their_addresses(
    token_amount,
    number_of_tokens,
    private_keys,
    proxy_manager,
    token_network_registry_address,
    register_tokens,
    contract_manager,
    token_contract_name,
) -> typing.List[typing.TokenAddress]:
    """ Fixture that yields `number_of_tokens` ERC20 token addresses, where the
    `token_amount` (per token) is distributed among the addresses behind `deploy_client` and
    potentially pre-registered with the Raiden Registry.
    The following arguments can control the behavior:

    Args:
        token_amount (int): the overall number of units minted per token
        number_of_tokens (int): the number of token instances
        register_tokens (bool): controls if tokens will be registered with raiden Registry
    """

    participants = [privatekey_to_address(key) for key in private_keys]
    token_addresses = deploy_tokens_and_fund_accounts(
        token_amount=token_amount,
        number_of_tokens=number_of_tokens,
        proxy_manager=proxy_manager,
        participants=participants,
        contract_manager=contract_manager,
        token_contract_name=token_contract_name,
    )

    if register_tokens:
        for token in token_addresses:
            registry = proxy_manager.token_network_registry(token_network_registry_address)
            registry.add_token(
                token_address=token,
                channel_participant_deposit_limit=RED_EYES_PER_CHANNEL_PARTICIPANT_LIMIT,
                token_network_deposit_limit=RED_EYES_PER_TOKEN_NETWORK_LIMIT,
                block_identifier=proxy_manager.client.blockhash_from_blocknumber("latest"),
            )

    return token_addresses


@pytest.fixture(name="secret_registry_address")
def deploy_secret_registry_and_return_address(deploy_client, contract_manager) -> typing.Address:
    address = deploy_contract_web3(
        contract_name=CONTRACT_SECRET_REGISTRY,
        deploy_client=deploy_client,
        contract_manager=contract_manager,
    )
    return address


@pytest.fixture(name="service_registry_address")
def maybe_deploy_service_registry_and_return_address(
    deploy_client, contract_manager, token_proxy, environment_type
) -> Optional[typing.Address]:
    if environment_type == Environment.PRODUCTION:
        return None
    # Not sure what to put in the registration fee token for testing, so using
    # the same token we use for testing for now
    constructor_arguments = (
        token_proxy.address,
        EMPTY_ADDRESS,
        int(500e18),
        6,
        5,
        180 * SECONDS_PER_DAY,
        1000,
        200 * SECONDS_PER_DAY,
    )
    address = deploy_contract_web3(
        contract_name=CONTRACT_SERVICE_REGISTRY,
        deploy_client=deploy_client,
        contract_manager=contract_manager,
        constructor_arguments=constructor_arguments,
    )
    return address


@pytest.fixture(name="user_deposit_address")
def deploy_user_deposit_and_return_address(
    proxy_manager, deploy_client, contract_manager, token_proxy, private_keys, environment_type
) -> typing.Optional[typing.Address]:
    """ Deploy UserDeposit and fund accounts with some balances """
    if environment_type != Environment.DEVELOPMENT:
        return None

    constructor_arguments = [token_proxy.address, UINT256_MAX]
    user_deposit_address = deploy_contract_web3(
        contract_name=CONTRACT_USER_DEPOSIT,
        deploy_client=deploy_client,
        contract_manager=contract_manager,
        constructor_arguments=constructor_arguments,
    )

    user_deposit = proxy_manager.user_deposit(user_deposit_address)

    participants = [privatekey_to_address(key) for key in private_keys]
    for transfer_to in participants:
        user_deposit.deposit(
            beneficiary=transfer_to,
            total_deposit=MONITORING_REWARD,
            given_block_identifier="latest",
        )

    return user_deposit_address


@pytest.fixture(name="one_to_n_address")
def deploy_one_to_n_and_return_address(
    user_deposit_address,
    deploy_client,
    contract_manager,
    environment_type,
    chain_id,
    service_registry_address,
) -> typing.Optional[typing.Address]:
    """ Deploy OneToN contract and return the address """
    if environment_type != Environment.DEVELOPMENT:
        return None

    constructor_arguments = [user_deposit_address, chain_id, service_registry_address]
    one_to_n_address = deploy_contract_web3(
        contract_name=CONTRACT_ONE_TO_N,
        deploy_client=deploy_client,
        contract_manager=contract_manager,
        constructor_arguments=constructor_arguments,
    )

    return one_to_n_address


@pytest.fixture
def secret_registry_proxy(deploy_client, secret_registry_address, contract_manager):
    """This uses the available SecretRegistry JSONRPCClient proxy to
    instantiate a Raiden proxy.

    The JSONRPCClient proxy just exposes the functions from the smart contract
    as methods in a generate python object, the Raiden proxy uses it to
    provider alternative interfaces *and* most importantly to do additional
    error checking (reason for transaction failure, gas usage, etc.).
    """
    return SecretRegistry(
        jsonrpc_client=deploy_client,
        secret_registry_address=to_canonical_address(secret_registry_address),
        contract_manager=contract_manager,
    )


@pytest.fixture(name="token_network_registry_address")
def deploy_token_network_registry_and_return_address(
    deploy_client,
    secret_registry_address,
    chain_id,
    settle_timeout_min,
    settle_timeout_max,
    contract_manager,
) -> typing.Address:
    constructor_arguments = [
        to_checksum_address(secret_registry_address),
        chain_id,
        settle_timeout_min,
        settle_timeout_max,
        UINT256_MAX,
    ]

    address = deploy_contract_web3(
        contract_name=CONTRACT_TOKEN_NETWORK_REGISTRY,
        deploy_client=deploy_client,
        contract_manager=contract_manager,
        constructor_arguments=constructor_arguments,
    )
    return address


@pytest.fixture(name="token_network_proxy")
def register_token_and_return_the_network_proxy(
    contract_manager, deploy_client, token_proxy, token_network_registry_address
):
    registry_address = to_canonical_address(token_network_registry_address)

    blockchain_service = ProxyManager(
        rpc_client=deploy_client,
        contract_manager=contract_manager,
        metadata=ProxyManagerMetadata(
            token_network_registry_deployed_at=GENESIS_BLOCK_NUMBER,
            filters_start_at=GENESIS_BLOCK_NUMBER,
        ),
    )

    token_network_registry_proxy = blockchain_service.token_network_registry(registry_address)
    token_network_address = token_network_registry_proxy.add_token(
        token_address=token_proxy.address,
        channel_participant_deposit_limit=RED_EYES_PER_CHANNEL_PARTICIPANT_LIMIT,
        token_network_deposit_limit=RED_EYES_PER_TOKEN_NETWORK_LIMIT,
        block_identifier=deploy_client.get_confirmed_blockhash(),
    )

    blockchain_service = ProxyManager(
        rpc_client=deploy_client,
        contract_manager=contract_manager,
        metadata=ProxyManagerMetadata(
            token_network_registry_deployed_at=GENESIS_BLOCK_NUMBER,
            filters_start_at=GENESIS_BLOCK_NUMBER,
        ),
    )
    return blockchain_service.token_network(token_network_address)


@pytest.fixture(name="token_proxy")
def deploy_token_and_return_proxy(deploy_client, contract_manager, token_contract_name):
    token_contract = deploy_token(
        deploy_client=deploy_client,
        contract_manager=contract_manager,
        initial_amount=1000 * 10 ** 18,
        decimals=0,
        token_name="TKN",
        token_symbol="TKN",
        token_contract_name=token_contract_name,
    )

    return Token(
        jsonrpc_client=deploy_client,
        token_address=to_canonical_address(token_contract.contract.address),
        contract_manager=contract_manager,
    )
