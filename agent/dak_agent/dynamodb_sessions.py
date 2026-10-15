"""ADK session service that keeps sessions in DynamoDB, so a Lambda deployment
can continue a conversation (history, state, pending approvals and questions)
across turns. PBI #721 / Task #720.

Single table with a string `pk` / `sk` and a TTL attribute `expires_at`:

  S#<app>#<user>#<session>  META          session state + last_update_time
  S#<app>#<user>#<session>  E#<ts>#<id>   one event (`Event` JSON)
  U#<app>#<user>            STATE         `user:` state
  U#<app>#<user>            S#<session>   index entry, so a user's sessions can
                                          be listed without a table scan
  A#<app>                   STATE         `app:` state

Pending approvals and questions are not stored separately: they are derived from
the event list (`docs/design/approval-queue.md`), so persisting events is enough.
Artifacts are not kept across turns. Every item expires 7 days after its last
write; DynamoDB deletes lazily, so reads also treat an expired item as missing.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.events.event import Event
from google.adk.sessions import _session_util
from google.adk.sessions.base_session_service import (
    BaseSessionService,
    GetSessionConfig,
    ListSessionsResponse,
)
from google.adk.sessions.session import Session
from google.adk.sessions.state import State

TTL_SECONDS = 7 * 24 * 3600
# DynamoDB's item limit is 400 KB; leave room for the key and attributes.
MAX_EVENT_BYTES = 400 * 1024 - 1024

_EVENT_PREFIX = "E#"
_EVENT_END = "E#~"  # above any timestamp / uuid, below "META"


def _session_pk(app_name: str, user_id: str, session_id: str) -> str:
    return f"S#{app_name}#{user_id}#{session_id}"


def _user_pk(app_name: str, user_id: str) -> str:
    return f"U#{app_name}#{user_id}"


def _app_pk(app_name: str) -> str:
    return f"A#{app_name}"


def _event_sk(event: Event) -> str:
    return f"{_EVENT_PREFIX}{event.timestamp:020.6f}#{event.id}"


class DynamoDbSessionService(BaseSessionService):
    """`table` is a boto3 `Table` (or anything with `get_item`, `put_item`,
    `delete_item` and `query` of the same shape)."""

    def __init__(self, table: Any, *, ttl_seconds: int = TTL_SECONDS):
        self._table = table
        self._ttl = ttl_seconds

    # -- low-level helpers -------------------------------------------------

    def _expires_at(self, now: float) -> int:
        return int(now) + self._ttl

    def _get(self, pk: str, sk: str) -> Optional[dict[str, Any]]:
        item = self._table.get_item(Key={"pk": pk, "sk": sk}).get("Item")
        if item is None or int(item.get("expires_at", 0)) < time.time():
            return None
        return item

    def _put(self, pk: str, sk: str, now: float, **attrs: Any) -> None:
        self._table.put_item(
            Item={"pk": pk, "sk": sk, "expires_at": self._expires_at(now), **attrs}
        )

    def _query(self, pk: str, lo: str, hi: str, **kwargs: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        start_key = None
        while True:
            call = dict(
                KeyConditionExpression="pk = :pk AND sk BETWEEN :lo AND :hi",
                ExpressionAttributeValues={":pk": pk, ":lo": lo, ":hi": hi},
                **kwargs,
            )
            if start_key:
                call["ExclusiveStartKey"] = start_key
            page = self._table.query(**call)
            items.extend(page.get("Items", []))
            start_key = page.get("LastEvaluatedKey")
            limit = kwargs.get("Limit")
            if not start_key or (limit is not None and len(items) >= limit):
                return items

    def _scoped_state(self, pk: str) -> dict[str, Any]:
        item = self._get(pk, "STATE")
        return json.loads(item["state"]) if item else {}

    def _put_scoped_state(self, pk: str, delta: dict[str, Any], now: float) -> None:
        state = self._scoped_state(pk)
        state.update(delta)
        self._put(pk, "STATE", now, state=json.dumps(state))

    def _merged(self, session: Session) -> Session:
        for key, value in self._scoped_state(_app_pk(session.app_name)).items():
            session.state[State.APP_PREFIX + key] = value
        user_state = self._scoped_state(_user_pk(session.app_name, session.user_id))
        for key, value in user_state.items():
            session.state[State.USER_PREFIX + key] = value
        return session

    def _write_meta(self, session: Session, now: float) -> None:
        # Only session-scoped keys belong in META; app:/user: live in their own items.
        state = _session_util.extract_json_safe_state_delta(session.state)["session"]
        self._put(
            _session_pk(session.app_name, session.user_id, session.id),
            "META",
            now,
            state=json.dumps(state),
            last_update_time=str(session.last_update_time),
        )
        self._put(
            _user_pk(session.app_name, session.user_id),
            f"S#{session.id}",
            now,
            session_id=session.id,
        )

    # -- BaseSessionService ------------------------------------------------

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        session_id = session_id.strip() if session_id else str(uuid.uuid4())
        if self._get(_session_pk(app_name, user_id, session_id), "META"):
            raise AlreadyExistsError(f"Session with id {session_id} already exists.")
        now = time.time()
        deltas = _session_util.extract_json_safe_state_delta(state or {})
        if deltas["app"]:
            self._put_scoped_state(_app_pk(app_name), deltas["app"], now)
        if deltas["user"]:
            self._put_scoped_state(_user_pk(app_name, user_id), deltas["user"], now)
        session = Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=deltas["session"],
            last_update_time=now,
        )
        self._write_meta(session, now)
        return self._merged(session)

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        session_id = session_id.strip() if session_id else session_id
        pk = _session_pk(app_name, user_id, session_id)
        meta = self._get(pk, "META")
        if meta is None:
            return None
        lo = _EVENT_PREFIX
        if config and config.after_timestamp:
            lo = f"{_EVENT_PREFIX}{config.after_timestamp:020.6f}"
        kwargs: dict[str, Any] = {}
        n = config.num_recent_events if config else None
        if n == 0:
            items: list[dict[str, Any]] = []
        elif n is not None:
            kwargs = {"ScanIndexForward": False, "Limit": n}
            items = list(reversed(self._query(pk, lo, _EVENT_END, **kwargs)[:n]))
        else:
            items = self._query(pk, lo, _EVENT_END)
        now = time.time()
        events = [
            Event.model_validate_json(i["event"])
            for i in items
            if int(i.get("expires_at", 0)) >= now
        ]
        session = Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=json.loads(meta["state"]),
            events=events,
            last_update_time=float(meta["last_update_time"]),
        )
        return self._merged(session)

    async def list_sessions(
        self, *, app_name: str, user_id: Optional[str] = None
    ) -> ListSessionsResponse:
        if user_id is None:
            raise ValueError("user_id is required: DynamoDbSessionService does not scan the table")
        sessions = []
        for entry in self._query(_user_pk(app_name, user_id), "S#", "S#~"):
            meta = self._get(_session_pk(app_name, user_id, entry["session_id"]), "META")
            if meta is None:
                continue
            sessions.append(
                self._merged(
                    Session(
                        app_name=app_name,
                        user_id=user_id,
                        id=entry["session_id"],
                        state=json.loads(meta["state"]),
                        last_update_time=float(meta["last_update_time"]),
                    )
                )
            )
        sessions.sort(key=lambda s: (s.last_update_time, s.user_id, s.id))
        return ListSessionsResponse(sessions=sessions)

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        session_id = session_id.strip() if session_id else session_id
        pk = _session_pk(app_name, user_id, session_id)
        for item in self._query(pk, "E#", "META"):  # events and META
            self._table.delete_item(Key={"pk": pk, "sk": item["sk"]})
        self._table.delete_item(Key={"pk": _user_pk(app_name, user_id), "sk": f"S#{session_id}"})

    async def get_user_state(self, *, app_name: str, user_id: str) -> dict[str, Any]:
        return self._scoped_state(_user_pk(app_name, user_id))

    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial:
            return event
        pk = _session_pk(session.app_name, session.user_id, session.id)
        if self._get(pk, "META") is None:
            raise SessionNotFoundError(f"Session {session.id} not found.")
        # Applies the state delta to `session` in memory and trims `temp:` keys
        # from the event, so they are never written.
        await super().append_event(session, event)
        body = event.model_dump_json()
        if len(body.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("event_too_large")
        now = time.time()
        session.last_update_time = event.timestamp
        self._put(pk, _event_sk(event), now, event=body)
        if event.actions and event.actions.state_delta:
            deltas = _session_util.extract_json_safe_state_delta(event.actions.state_delta)
            if deltas["app"]:
                self._put_scoped_state(_app_pk(session.app_name), deltas["app"], now)
            if deltas["user"]:
                self._put_scoped_state(
                    _user_pk(session.app_name, session.user_id), deltas["user"], now
                )
        self._write_meta(session, now)
        return event
