from contextlib import ExitStack

from agent_board import identity
from agent_board.client import Client
from agent_board.config import Config, ConfigError
from agent_board.runner import loop
from agent_board.runner.config import RunnerConfig
from agent_board.runner.happier import Happier, Session

WAKE_TEST_GREETING = (
    "This session was opened by `agent-board wake-test` so this working directory can be trusted once "
    "from the phone. Approve the trust question if one appears; nothing else is needed."
)


def run(board: Config, *, once: bool = False) -> None:
    cfg = RunnerConfig.load()
    with ExitStack() as stack:
        clients = {me: stack.enter_context(Client(board, me)) for me in map(identity.resolve, cfg.kinds)}
        happier = Happier(session_home=cfg.session_home)
        if once:
            loop.run_once(cfg, clients, happier)
        else:
            loop.run_forever(cfg, clients, happier)


def wake_test(kind: str) -> Session:
    cfg = RunnerConfig.load()
    settings = cfg.kinds.get(kind)
    if settings is None:
        raise ConfigError(f"{kind} is not one of the runner's kinds")
    return loop.open_session(
        settings, identity.resolve(kind), Happier(session_home=cfg.session_home), WAKE_TEST_GREETING
    )
