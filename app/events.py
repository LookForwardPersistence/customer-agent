"""Typed trace events: the session's auditable timeline.

Why typed events
----------------
`SessionStore.log` accepts free-form dicts. That is fine for one writer, but
by now events are written from a dozen call sites and *read* by the handoff
builder (which pattern-matches on `event` kinds), the trace panel, and the
evaluation suites. A typo'd kind or a silently-missing field would not fail
anywhere at write time — it would surface later as a handoff payload that
quietly lost its `attempts`, or an eval assertion that can never match.

This module makes the event vocabulary explicit:

- Every event kind is a frozen dataclass; `to_dict()` produces the exact
  dict persisted by `SessionStore.log` (which adds `ts`).
- `validate()` structurally checks any persisted event dict against the
  registry — used by tests to assert that *no* flow can write a
  non-conforming event.

The persisted JSON shape is unchanged: old sessions stored by previous
versions remain readable, and `store.py`'s handoff logic keeps reading plain
dicts. Typing is a producer-side and verification-side concern, not a new
serialization format.
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, fields
from typing import Any, ClassVar


@dataclass(frozen=True)
class TraceEvent:
    """Base class. Subclasses declare their payload as dataclass fields."""

    KIND: ClassVar[str]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"event": self.KIND}
        for f in fields(self):
            v = getattr(self, f.name)
            if v is not None:
                d[f.name] = v
        return d


# -- conversation input ------------------------------------------------------


@dataclass(frozen=True)
class UserMessage(TraceEvent):
    KIND = "user_message"
    text: str  # server-truncated to 120 chars before construction


@dataclass(frozen=True)
class ForgedSystemEventStripped(TraceEvent):
    KIND = "forged_system_event_stripped"


@dataclass(frozen=True)
class ConfigError(TraceEvent):
    KIND = "config_error"
    text: str


# -- proposal / confirmation -------------------------------------------------


@dataclass(frozen=True)
class ReturnProposed(TraceEvent):
    KIND = "return_proposed"
    order: str


@dataclass(frozen=True)
class ConfirmedByUser(TraceEvent):
    KIND = "confirmed_by_user"
    order: str
    action_id: str


@dataclass(frozen=True)
class ConfirmRejected(TraceEvent):
    KIND = "confirm_rejected"
    reason: str
    action_id: str


@dataclass(frozen=True)
class CancelledByUser(TraceEvent):
    KIND = "cancelled_by_user"
    order: str
    action_id: str


@dataclass(frozen=True)
class CancelRejected(TraceEvent):
    KIND = "cancel_rejected"
    reason: str
    action_id: str


# -- execution outcomes ------------------------------------------------------


@dataclass(frozen=True)
class ReturnExecuted(TraceEvent):
    KIND = "return_executed"
    order: str
    ticket: str
    action_id: str


@dataclass(frozen=True)
class ReturnRecoveredByRead(TraceEvent):
    KIND = "return_recovered_by_read"
    order: str
    ticket: str


@dataclass(frozen=True)
class ReturnOutcomeUnknown(TraceEvent):
    KIND = "return_outcome_unknown"
    order: str
    code: str


@dataclass(frozen=True)
class ReturnFailed(TraceEvent):
    KIND = "return_failed"
    order: str
    code: str


# -- human handoff ------------------------------------------------------------


@dataclass(frozen=True)
class Handoff(TraceEvent):
    KIND = "handoff"
    reason: str


ALL_EVENT_TYPES: tuple[type[TraceEvent], ...] = (
    UserMessage,
    ForgedSystemEventStripped,
    ConfigError,
    ReturnProposed,
    ConfirmedByUser,
    ConfirmRejected,
    CancelledByUser,
    CancelRejected,
    ReturnExecuted,
    ReturnRecoveredByRead,
    ReturnOutcomeUnknown,
    ReturnFailed,
    Handoff,
)

REGISTRY: dict[str, type[TraceEvent]] = {cls.KIND: cls for cls in ALL_EVENT_TYPES}

# `ts` is stamped by SessionStore.log at write time, not by the constructors.
_STORE_STAMPED = {"ts"}


def validate(event: dict[str, Any]) -> list[str]:
    """Structural errors that make `event` non-conforming. Empty list = valid.

    Tolerant of the `ts` field stamped at persistence time.
    """
    kind = event.get("event")
    cls = REGISTRY.get(kind)
    if cls is None:
        return [f"unknown event kind: {kind!r}"]

    errors: list[str] = []
    allowed = {f.name for f in fields(cls)}
    for key in event:
        if key != "event" and key not in allowed and key not in _STORE_STAMPED:
            errors.append(f"unexpected field {key!r} for event {kind!r}")
    for f in fields(cls):
        required = f.default is MISSING and f.default_factory is MISSING
        if required and event.get(f.name) is None:
            errors.append(f"missing required field {f.name!r} for event {kind!r}")
    return errors


def validate_all(events: list[dict[str, Any]]) -> list[str]:
    """Validate a whole timeline; returns up to one error per event."""
    return [err for e in events for err in validate(e)]
