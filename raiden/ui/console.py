import io
import logging
import sys
import time

import gevent
import IPython
from eth_utils import decode_hex, to_canonical_address, to_checksum_address
from IPython.lib.inputhook import inputhook_manager, stdin_ready

from raiden import waiting
from raiden.api.python import RaidenAPI
from raiden.constants import UINT256_MAX
from raiden.network.proxies.token_network import TokenNetwork
from raiden.settings import DEFAULT_RETRY_TIMEOUT
from raiden.utils import TokenAddress, typing
from raiden.utils.smart_contracts import deploy_contract_web3
from raiden_contracts.constants import CONTRACT_HUMAN_STANDARD_TOKEN

GUI_GEVENT = "gevent"

# ansi escape code for typesetting
HEADER = "\033[95m"
OKBLUE = "\033[94m"
OKGREEN = "\033[92m"
ENDC = "\033[0m"

# ipython needs to accept "--gui gevent" option
IPython.core.shellapp.InteractiveShellApp.gui.values += ("gevent",)


def print_usage():
    print(f"\t{OKBLUE}use `{HEADER}raiden{OKBLUE}` to interact with the raiden service.")
    print(f"\tuse `{HEADER}chain{OKBLUE}` to interact with the blockchain.")
    print(
        "\tuse `{}tools{}` for convenience with tokens, channels, funding, ...".format(
            HEADER, OKBLUE
        )
    )
    print(f"\tuse `{HEADER}denoms{OKBLUE}` for ether calculations")
    print(f"\tuse `{HEADER}lastlog(n){OKBLUE}` to see n lines of log-output. [default 10] ")
    print(f"\tuse `{HEADER}lasterr(n){OKBLUE}` to see n lines of stderr. [default 1]")
    print(f"\tuse `{HEADER}help(<topic>){OKBLUE}` for help on a specific topic.")
    print(f"\ttype `{HEADER}usage(){OKBLUE}` to see this help again.")
    print("\n" + ENDC)


def inputhook_gevent():
    while not stdin_ready():
        gevent.sleep(0.05)
    return 0


@inputhook_manager.register("gevent")
class GeventInputHook:
    def __init__(self, manager):
        self.manager = manager
        self._current_gui = GUI_GEVENT

    def enable(self, app=None):
        """ Enable event loop integration with gevent.

        Args:
            app: Ignored, it's only a placeholder to keep the call signature of all
                gui activation methods consistent, which simplifies the logic of
                supporting magics.

        Notes:
            This methods sets the PyOS_InputHook for gevent, which allows
            gevent greenlets to run in the background while interactively using
            IPython.
        """
        self.manager.set_inputhook(inputhook_gevent)
        self._current_gui = GUI_GEVENT
        return app

    def disable(self):
        """ Disable event loop integration with gevent.

        This merely sets PyOS_InputHook to NULL.
        """
        self.manager.clear_inputhook()


class Console(gevent.Greenlet):
    """ A service starting an interactive ipython session when receiving the
    SIGSTP signal (e.g. via keyboard shortcut CTRL-Z).
    """

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.console_locals = None

    def _run(self):  # pylint: disable=method-hidden
        # Remove handlers that log to stderr
        root = logging.getLogger()
        for handler in root.handlers[:]:
            if isinstance(handler, logging.StreamHandler) and handler.stream == sys.stderr:
                root.removeHandler(handler)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream=stream)
        handler.formatter = logging.Formatter("%(levelname)s:%(name)s %(message)s")
        root.addHandler(handler)
        err = io.StringIO()
        sys.stderr = err

        def lastlog(n=10, prefix=None, level=None):
            """ Print the last `n` log lines to stdout.
            Use `prefix='p2p'` to filter for a specific logger.
            Use `level=INFO` to filter for a specific level.
            Level- and prefix-filtering are applied before tailing the log.
            """
            lines = stream.getvalue().strip().split("\n") or []
            if prefix:
                lines = [line for line in lines if line.split(":")[1].startswith(prefix)]
            if level:
                lines = [line for line in lines if line.split(":")[0] == level]
            for line in lines[-n:]:
                print(line)

        def lasterr(n=1):
            """ Print the last `n` entries of stderr to stdout. """
            for line in (err.getvalue().strip().split("\n") or [])[-n:]:
                print(line)

        tools = ConsoleTools(self.app.raiden, self.app.config["settle_timeout"])

        self.console_locals = {
            "app": self.app,
            "raiden": self.app.raiden,
            "proxy_manager": self.app.raiden.proxy_manager,
            "tools": tools,
            "lasterr": lasterr,
            "lastlog": lastlog,
            "usage": print_usage,
        }

        print("\n" * 2)
        print("Entering Console" + OKGREEN)
        print("Tip:" + OKBLUE)
        print_usage()
        IPython.start_ipython(argv=["--gui", "gevent"], user_ns=self.console_locals)

        sys.exit(0)


class ConsoleTools:
    def __init__(self, raiden_service, settle_timeout):
        self._chain = raiden_service.chain
        self._raiden = raiden_service
        self._api = RaidenAPI(raiden_service)
        self.settle_timeout = settle_timeout

    def create_token(
        self,
        registry_address,
        initial_alloc=10 ** 6,
        name="raidentester",
        symbol="RDT",
        decimals=2,
        timeout=60,
        auto_register=True,
    ):
        """ Create a proxy for a new HumanStandardToken (ERC20), that is
        initialized with Args(below).
        Per default it will be registered with 'raiden'.

        Args:
            initial_alloc (int): amount of initial tokens.
            name (str): human readable token name.
            symbol (str): token shorthand symbol.
            decimals (int): decimal places.
            timeout (int): timeout in seconds for creation.
            auto_register (boolean): if True(default), automatically register
                the token with raiden.

        Returns:
            token_address_hex: the hex encoded address of the new token/token.
        """
        with gevent.Timeout(timeout):
            token_address = deploy_contract_web3(
                CONTRACT_HUMAN_STANDARD_TOKEN,
                self._chain.client,
                contract_manager=self._raiden.contract_manager,
                constructor_arguments=(initial_alloc, name, decimals, symbol),
            )

        token_address_hex = to_checksum_address(token_address)
        if auto_register:
            self.register_token(registry_address, token_address_hex)
        print(
            "Successfully created {}the token '{}'.".format(
                "and registered " if auto_register else " ", name
            )
        )
        return token_address_hex

    def register_token(
        self,
        registry_address_hex: typing.AddressHex,
        token_address_hex: typing.AddressHex,
        retry_timeout: typing.NetworkTimeout = DEFAULT_RETRY_TIMEOUT,
    ) -> TokenNetwork:
        """ Register a token with the raiden token manager.

        Args:
            registry_address_hex: a hex encoded registry address.
            token_address_hex: a hex encoded token address.

        Returns:
            The token network proxy.
        """
        registry_address = to_canonical_address(registry_address_hex)
        token_address = TokenAddress(to_canonical_address(token_address_hex))

        registry = self._raiden.proxy_manager.token_network_registry(registry_address)

        token_network_address = registry.add_token(
            token_address=token_address,
            channel_participant_deposit_limit=UINT256_MAX,
            token_network_deposit_limit=UINT256_MAX,
        )
        waiting.wait_for_token_network(
            self._raiden, registry.address, token_address, retry_timeout
        )

        return self._raiden.proxy_manager.token_network(token_network_address)

    def open_channel_with_funding(
        self,
        registry_address_hex,
        token_address_hex,
        peer_address_hex,
        total_deposit,
        settle_timeout=None,
    ):
        """ Convenience method to open a channel.

        Args:
            registry_address_hex (str): hex encoded address of the registry for the channel.
            token_address_hex (str): hex encoded address of the token for the channel.
            peer_address_hex (str): hex encoded address of the channel peer.
            total_deposit (int): amount of total funding for the channel.
            settle_timeout (int): amount of blocks for the settle time (if None use app defaults).

        Return:
            netting_channel: the (newly opened) netting channel object.
        """
        # Check, if peer is discoverable
        registry_address = decode_hex(registry_address_hex)
        peer_address = decode_hex(peer_address_hex)
        token_address = decode_hex(token_address_hex)

        self._api.channel_open(
            registry_address, token_address, peer_address, settle_timeout=settle_timeout
        )

        return self._api.set_total_channel_deposit(
            registry_address, token_address, peer_address, total_deposit
        )

    def wait_for_contract(self, contract_address_hex, timeout=None):
        """ Wait until a contract is mined

        Args:
            contract_address_hex (string): hex encoded address of the contract
            timeout (int): time to wait for the contract to get mined

        Returns:
            True if the contract got mined, false otherwise
        """
        contract_address = decode_hex(contract_address_hex)
        start_time = time.time()
        result = self._raiden.rpc_client.web3.eth.getCode(to_checksum_address(contract_address))

        current_time = time.time()
        while not result:
            if timeout and start_time + timeout > current_time:
                return False

            result = self._raiden.rpc_client.web3.eth.getCode(
                to_checksum_address(contract_address)
            )
            gevent.sleep(0.5)

            current_time = time.time()

        return len(result) > 0
