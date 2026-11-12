from collections.abc import Mapping
from typing import Any
from uuid import UUID

from agent_board.night.sync import Ack, BoardRefused, BoardState, BoardUnavailable
from agent_board.night_authority import AuthorityRefused, GrantUnavailable, NightAuthorityClient, ServerGrant

SERVER_STOPPING = "stopping"


class SignedBoard:
    def __init__(self, box: str, client: NightAuthorityClient) -> None:
        self._box = box
        self._client = client

    def fetch_grant(self, run_id: str, fence: str) -> ServerGrant:
        return self._client.fetch_grant(run_id, fence)

    def state(self) -> BoardState:
        data = self._request("night.state", "self", {})
        if set(data) != {"box", "journal_id", "counter", "active"} or data["box"] != self._box:
            raise BoardRefused("invalid_state")
        journal_id, counter = data["journal_id"], data["counter"]
        if (journal_id is not None and not _uuid(journal_id)) or not _counter(counter):
            raise BoardRefused("invalid_state")
        if journal_id is None and counter != 0:
            raise BoardRefused("invalid_state")
        active = data["active"]
        if not isinstance(active, list):
            raise BoardRefused("invalid_state")
        ids: set[str] = set()
        stopping: set[str] = set()
        for record in active:
            if not isinstance(record, dict) or not _uuid(record.get("id")) or record["id"] in ids:
                raise BoardRefused("invalid_state")
            if not isinstance(record.get("state"), str) or not record["state"]:
                raise BoardRefused("invalid_state")
            ids.add(record["id"])
            if record["state"] == SERVER_STOPPING:
                stopping.add(record["id"])
        return BoardState(self._box, journal_id, counter, frozenset(ids), frozenset(stopping))

    def bootstrap(self, payload: Mapping[str, Any]) -> Ack:
        data = self._request("night.bootstrap", "self", payload)
        if data.get("box") != self._box:
            raise BoardRefused("invalid_bootstrap")
        return _ack(data)

    def binding(self, run_id: str, payload: Mapping[str, Any]) -> Ack:
        return _ack(self._request("night.binding", run_id, payload))

    def evidence(self, run_id: str, payload: Mapping[str, Any]) -> Ack:
        return _ack(self._request("night.evidence", run_id, payload))

    def _request(self, action: str, target: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self._client.request(action, target, dict(payload))
        except AuthorityRefused:
            raise BoardRefused("authority_refused") from None
        except GrantUnavailable:
            raise BoardUnavailable("authority_unavailable") from None


def _uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _counter(value: Any) -> bool:
    return type(value) is int and value >= 0


def _ack(data: dict[str, Any]) -> Ack:
    if not _uuid(data.get("journal_id")) or not _counter(data.get("counter")):
        raise BoardRefused("invalid_ack")
    return Ack(data["journal_id"], data["counter"])
