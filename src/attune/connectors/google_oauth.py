"""Direct-OAuth implementation of :class:`WorkspaceConnector` (design 4.3, 4.7).

The default when an MCP credential boundary is unnecessary or unavailable:
talk to Google APIs through google-api-python-client with a principal-scoped
OAuth credential.

Scope discipline (design + Google guidance): start read-only
(``gmail.readonly``, ``calendar.readonly``), add ``gmail.compose`` for
drafting, and add ``gmail.send`` ONLY when an autonomy grant explicitly calls
for autonomous sending. Never request send scope "to avoid re-auth later."

The send gate is structural, not disciplinary: ``send_reply`` refuses unless
``send_enabled=True``, which must be set alongside a real ``gmail.send`` scope
and an explicit autonomy grant. The default is draft-only.

``gmail_service`` and ``calendar_service`` are injected so tests can supply
fakes and avoid any live Google credentials or network calls.
"""

from __future__ import annotations

import base64
import email.mime.text
from datetime import datetime, timezone
from typing import Any

from .base import (
    CalendarEvent,
    CalendarWriteNotPermitted,
    DraftRef,
    EmailThread,
    LabelNotPermitted,
    Provenance,
    SendNotPermitted,
    WorkspaceConnector,
    has_external_attendees,
)

# Minimal, escalating scope sets. Compose the set from the capabilities actually
# granted; do not request send unless autonomous sending is authorized.
SCOPES_READONLY = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
)
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
# Labeling/archiving (Phase 3 stage 1, G9) needs to both add a label and
# remove INBOX -- gmail.modify is Google's scope for that (gmail.compose has
# no label-removal capability). Escalating set: request it only alongside
# ATTUNE_MAIL_LABELS_ENABLED, same discipline as SCOPE_SEND.
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
SCOPES_LABEL = SCOPES_READONLY + (SCOPE_MODIFY,)

# Calendar writes (Phase 3 stage 2: decline invites, reschedule the
# principal's own events) need events.patch, which calendar.events already
# covers. Unlike SCOPE_MODIFY, this scope is typically ALREADY present in a
# standard install — ``credentials.py``'s ``SCOPES_DEFAULT`` requests it by
# default for the Phase 2 hold-creation write path (``create_hold``, which
# has no equivalent double gate). ``calendar_writes_enabled`` below is
# still a real second gate: having the scope doesn't mean the deployment
# has opted into declining invites or moving the principal's meetings.
SCOPE_CALENDAR_WRITE = "https://www.googleapis.com/auth/calendar.events"
SCOPES_CALENDAR_WRITE = SCOPES_READONLY + (SCOPE_CALENDAR_WRITE,)

_USER = "me"


class DirectOAuthConnector(WorkspaceConnector):
    """Direct Google API access. Send is gated behind an explicit flag that a
    caller sets ONLY alongside the gmail.send scope and an autonomy grant."""

    def __init__(
        self,
        *,
        credentials: Any = None,
        gmail_service: Any = None,
        calendar_service: Any = None,
        send_enabled: bool = False,
        labels_enabled: bool = False,
        calendar_writes_enabled: bool = False,
        owner_email: str | None = None,
        internal_domains: frozenset[str] = frozenset(),
    ):
        self._creds = credentials
        self._send_enabled = send_enabled
        # Same double-gate posture as send_enabled: set ONLY alongside the
        # gmail.modify scope (SCOPE_MODIFY above) and, in practice, an
        # explicit ATTUNE_MAIL_LABELS_ENABLED opt-in (Phase 3 stage 1, G9).
        self._labels_enabled = labels_enabled
        # Same double-gate posture again: set ONLY alongside the
        # calendar.events scope (SCOPE_CALENDAR_WRITE above) and an explicit
        # ATTUNE_CALENDAR_WRITES_ENABLED opt-in (Phase 3 stage 2).
        self._calendar_writes_enabled = calendar_writes_enabled
        self._gmail_svc = gmail_service
        self._cal_svc = calendar_service
        # Lets the thread builders tell counterparty messages from the
        # owner's own, so reply_to targets the right person (finding #3).
        self._owner_email = owner_email
        self._internal_domains = internal_domains
        # Gmail label ids rarely change; resolving/creating one is a
        # list-then-maybe-create round trip. Bounded (one entry per distinct
        # label name actually used) and cached per instance so a run
        # proposing several archive candidates doesn't repeat the API calls.
        self._label_id_cache: dict[str, str] = {}

    # --- service accessors -------------------------------------------------

    def _gmail(self) -> Any:
        if self._gmail_svc is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise ImportError(
                    "DirectOAuthConnector requires google-api-python-client. "
                    "`pip install google-api-python-client`."
                ) from exc
            self._gmail_svc = build("gmail", "v1", credentials=self._creds)
        return self._gmail_svc

    def _calendar(self) -> Any:
        if self._cal_svc is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise ImportError(
                    "DirectOAuthConnector requires google-api-python-client. "
                    "`pip install google-api-python-client`."
                ) from exc
            self._cal_svc = build("calendar", "v3", credentials=self._creds)
        return self._cal_svc

    # --- read: mail --------------------------------------------------------

    def list_threads(
        self, query: str = "is:unread", *, max_results: int = 20
    ) -> list[EmailThread]:
        res = (
            self._gmail()
            .users()
            .threads()
            .list(userId=_USER, q=query, maxResults=max_results)
            .execute()
        )
        threads = []
        for item in res.get("threads", []):
            detail = (
                self._gmail()
                .users()
                .threads()
                .get(userId=_USER, id=item["id"], format="metadata")
                .execute()
            )
            threads.append(_thread_from_metadata(detail, owner_email=self._owner_email))
        return threads

    def get_thread(self, thread_id: str) -> EmailThread:
        detail = (
            self._gmail()
            .users()
            .threads()
            .get(userId=_USER, id=thread_id, format="full")
            .execute()
        )
        return _thread_from_full(detail, owner_email=self._owner_email)

    # --- write: mail -------------------------------------------------------

    def create_draft(
        self, *, to: str, subject: str, body: str, thread_id: str | None = None
    ) -> DraftRef:
        message: dict[str, Any] = {"raw": _build_raw(to=to, subject=subject, body=body)}
        if thread_id:
            message["threadId"] = thread_id
        result = (
            self._gmail()
            .users()
            .drafts()
            .create(userId=_USER, body={"message": message})
            .execute()
        )
        return DraftRef(
            draft_id=result.get("id", ""),
            thread_id=result.get("message", {}).get("threadId") or thread_id,
        )

    def send_reply(self, *, draft_id: str) -> None:
        # Even with the service wired, refuse unless send was explicitly enabled.
        if not self._send_enabled:
            raise SendNotPermitted(
                "DirectOAuthConnector send disabled: requires gmail.send scope "
                "AND an autonomy grant. Draft-and-human-send is the default."
            )
        self._gmail().users().drafts().send(
            userId=_USER, body={"id": draft_id}
        ).execute()

    def add_label(self, *, thread_id: str, label: str) -> None:
        label_id = self._resolve_label_id(label)
        self._gmail().users().threads().modify(
            userId=_USER,
            id=thread_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    def supports_labeling(self) -> bool:
        """Structural capability, independent of whether it's turned on —
        the direct-OAuth backend CAN label/archive (unlike MCP contract v1,
        which has no label-removal tool). ``label_thread`` itself still
        refuses unless ``labels_enabled`` was set (the second, deployment-
        level gate)."""
        return True

    def label_thread(self, thread_id: str, *, label: str, archive: bool) -> None:
        """Add ``label`` to a Gmail thread via ``threads.modify``, removing
        INBOX when ``archive`` is True (Gmail's own definition of archiving —
        there is no separate "archive" verb, just removing the INBOX label).

        Refuses unless ``labels_enabled`` was explicitly set alongside the
        gmail.modify scope (the same double-gate discipline as
        ``send_reply``/``send_enabled``)."""
        if not self._labels_enabled:
            raise LabelNotPermitted(
                "DirectOAuthConnector labeling disabled: requires the "
                "gmail.modify scope AND ATTUNE_MAIL_LABELS_ENABLED=1. "
                "Draft-only is the default; nothing is archived silently."
            )
        label_id = self._resolve_label_id_cached(label)
        body: dict[str, Any] = {"addLabelIds": [label_id]}
        if archive:
            body["removeLabelIds"] = ["INBOX"]
        self._gmail().users().threads().modify(
            userId=_USER, id=thread_id, body=body,
        ).execute()

    # --- read: calendar ----------------------------------------------------

    def list_events(
        self, *, time_min: datetime, time_max: datetime
    ) -> list[CalendarEvent]:
        res = (
            self._calendar()
            .events()
            .list(
                calendarId="primary",
                timeMin=_to_rfc3339(time_min),
                timeMax=_to_rfc3339(time_max),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return [
            _event_from_google(e, self._internal_domains)
            for e in res.get("items", [])
        ]

    def get_event(self, event_id: str) -> CalendarEvent:
        detail = (
            self._calendar()
            .events()
            .get(calendarId="primary", eventId=event_id)
            .execute()
        )
        return _event_from_google(detail, self._internal_domains)

    def create_hold(self, event: CalendarEvent) -> str:
        body = {
            "summary": event.summary,
            "start": {"dateTime": event.start.isoformat()},
            "end": {"dateTime": event.end.isoformat()},
            "status": "tentative",
            "attendees": [{"email": a} for a in event.attendees],
        }
        result = (
            self._calendar()
            .events()
            .insert(calendarId="primary", body=body)
            .execute()
        )
        return result.get("id", "")

    def supports_calendar_writes(self) -> bool:
        """Structural capability, independent of whether it's turned on —
        the direct-OAuth backend CAN decline/reschedule (unlike MCP
        contract v1, which has neither tool). ``decline_invite``/
        ``reschedule_event`` still refuse unless ``calendar_writes_enabled``
        was set (the second, deployment-level gate)."""
        return True

    def decline_invite(self, event_id: str) -> None:
        """Patch the PRINCIPAL's own attendee responseStatus to "declined"
        (Phase 3 stage 2, Deliverable A) via ``events.patch`` — never
        touches any other attendee's entry. Calendar's PATCH replaces the
        whole ``attendees`` array rather than merging into it, so this
        fetches the current array, flips only the principal's own entry,
        and sends the full array back.

        Refuses unless ``calendar_writes_enabled`` was explicitly set
        alongside the calendar write scope (the same double-gate discipline
        as ``label_thread``/``labels_enabled``)."""
        if not self._calendar_writes_enabled:
            raise CalendarWriteNotPermitted(
                "DirectOAuthConnector calendar writes disabled: requires "
                "the calendar.events scope AND "
                "ATTUNE_CALENDAR_WRITES_ENABLED=1. Read-only is the "
                "default; nothing is declined or rescheduled silently."
            )
        event = (
            self._calendar()
            .events()
            .get(calendarId="primary", eventId=event_id)
            .execute()
        )
        updated_attendees = []
        found_self = False
        for attendee in event.get("attendees", []):
            entry = dict(attendee)
            if self._is_self_attendee(entry):
                entry["responseStatus"] = "declined"
                found_self = True
            updated_attendees.append(entry)
        if not found_self:
            raise CalendarWriteNotPermitted(
                f"cannot decline {event_id}: the principal is not listed "
                "as an attendee on this event"
            )
        self._calendar().events().patch(
            calendarId="primary",
            eventId=event_id,
            body={"attendees": updated_attendees},
        ).execute()

    def reschedule_event(
        self, event_id: str, *, new_start: datetime, new_end: datetime
    ) -> None:
        """Move ``event_id`` to a new start/end via ``events.patch`` (Phase
        3 stage 2, Deliverable A). Refuses (``CalendarWriteNotPermitted``)
        unless the principal is this event's ORGANIZER, verified from a
        FRESH ``events.get`` fetch performed right here — never from
        cached workflow state, which is exactly what would let a stale
        checkpoint move someone else's meeting."""
        if not self._calendar_writes_enabled:
            raise CalendarWriteNotPermitted(
                "DirectOAuthConnector calendar writes disabled: requires "
                "the calendar.events scope AND "
                "ATTUNE_CALENDAR_WRITES_ENABLED=1. Read-only is the "
                "default; nothing is declined or rescheduled silently."
            )
        event = (
            self._calendar()
            .events()
            .get(calendarId="primary", eventId=event_id)
            .execute()
        )
        organizer = event.get("organizer", {}) or {}
        if not self._is_self_attendee(organizer):
            raise CalendarWriteNotPermitted(
                f"cannot reschedule {event_id}: the principal is not this "
                "event's organizer"
            )
        self._calendar().events().patch(
            calendarId="primary",
            eventId=event_id,
            body={
                "start": {"dateTime": new_start.isoformat()},
                "end": {"dateTime": new_end.isoformat()},
            },
        ).execute()

    def _is_self_attendee(self, entry: dict[str, Any]) -> bool:
        """Whether ``entry`` (an attendee or organizer sub-object from a
        FRESH Calendar API fetch) is the principal — Google's own ``self``
        flag first, falling back to an email match against
        ``owner_email`` when the flag is absent (some fake/test payloads,
        or older API responses)."""
        if entry.get("self"):
            return True
        if self._owner_email and entry.get("email", "").lower() == self._owner_email.lower():
            return True
        return False

    # --- internal ----------------------------------------------------------

    def _resolve_label_id(self, name: str) -> str:
        """Return the Gmail label id for ``name``, creating it if absent."""
        res = self._gmail().users().labels().list(userId=_USER).execute()
        for lbl in res.get("labels", []):
            if lbl.get("name", "").lower() == name.lower():
                return lbl["id"]
        created = (
            self._gmail()
            .users()
            .labels()
            .create(userId=_USER, body={"name": name})
            .execute()
        )
        return created["id"]

    def _resolve_label_id_cached(self, name: str) -> str:
        """Cached wrapper around :meth:`_resolve_label_id` for the
        ``label_thread`` write path — bounded (one entry per distinct label
        name seen) and per-instance, so proposing several archives in one
        run costs one labels.list/create round trip, not one per thread."""
        cached = self._label_id_cache.get(name)
        if cached is not None:
            return cached
        label_id = self._resolve_label_id(name)
        self._label_id_cache[name] = label_id
        return label_id


# ---------------------------------------------------------------------------
# Module-level helpers (pure, testable without a service)
# ---------------------------------------------------------------------------


def _header(message: dict[str, Any], name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_body(payload: dict[str, Any]) -> str:
    """Extract the first plain-text body from a Gmail message payload."""
    data = payload.get("body", {}).get("data")
    if data:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode(
                    "utf-8", errors="replace"
                )
        if mime.startswith("multipart/"):
            text = _decode_body(part)
            if text:
                return text
    return ""


def _received_at(message: dict[str, Any]) -> datetime | None:
    raw = message.get("internalDate")
    if raw:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    return None


def _reply_target(messages: list[dict[str, Any]], owner_email: str | None) -> str:
    """The newest message NOT authored by the owner, preferring Reply-To over
    From. Empty when every message is the owner's (nobody to reply to). With
    no owner known, fall back to the newest message's envelope."""
    candidates = list(reversed(messages))
    if owner_email:
        candidates = [
            m for m in candidates
            if owner_email.lower() not in _header(m, "from").lower()
        ]
    if not candidates:
        return ""
    newest = candidates[0]
    return _header(newest, "reply-to") or _header(newest, "from")


def _thread_from_metadata(
    data: dict[str, Any], *, owner_email: str | None = None
) -> EmailThread:
    """Build an EmailThread from a threads.get(format='metadata') response.

    Uses the thread snippet from the first message; body is not fetched in
    this format (use ``get_thread`` for the full body)."""
    messages = data.get("messages") or []
    first = messages[0] if messages else {}
    last = messages[-1] if messages else {}
    snippet = first.get("snippet", "")
    return EmailThread(
        thread_id=data.get("id", ""),
        subject=_header(first, "subject"),
        snippet=snippet,
        from_addr=_header(first, "from"),
        body=snippet,  # metadata only; full body available via get_thread
        provenance=Provenance.FETCHED,
        received_at=_received_at(first),
        labels=first.get("labelIds", []),
        last_from_addr=_header(last, "from"),
        last_message_at=_received_at(last),
        reply_to=_reply_target(messages, owner_email),
    )


def _thread_from_full(
    data: dict[str, Any], *, owner_email: str | None = None
) -> EmailThread:
    """Build an EmailThread from a threads.get(format='full') response."""
    messages = data.get("messages") or []
    first = messages[0] if messages else {}
    last = messages[-1] if messages else {}
    body = _decode_body(last.get("payload", {})) or last.get("snippet", "")
    return EmailThread(
        thread_id=data.get("id", ""),
        subject=_header(first, "subject"),
        snippet=first.get("snippet", ""),
        from_addr=_header(first, "from"),
        body=body,
        provenance=Provenance.FETCHED,
        received_at=_received_at(first),
        labels=first.get("labelIds", []),
        last_from_addr=_header(last, "from"),
        last_message_at=_received_at(last),
        reply_to=_reply_target(messages, owner_email),
    )


def _build_raw(*, to: str, subject: str, body: str) -> str:
    """Build a base64url-encoded RFC 2822 message suitable for the Drafts API."""
    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["to"] = to
    msg["subject"] = subject
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _event_from_google(
    data: dict[str, Any], internal_domains: frozenset[str] = frozenset()
) -> CalendarEvent:
    start = _parse_event_dt(data.get("start", {}))
    end = _parse_event_dt(data.get("end", {}))
    attendees = [a["email"] for a in data.get("attendees", []) if "email" in a]
    organizer = data.get("organizer") or {}
    return CalendarEvent(
        event_id=data.get("id", ""),
        summary=data.get("summary", ""),
        start=start,
        end=end,
        attendees=attendees,
        external_attendees=has_external_attendees(attendees, internal_domains),
        organizer=organizer.get("email", ""),
        organizer_is_self=bool(organizer.get("self")),
        response_status=_self_response_status(data.get("attendees", [])),
    )


def _self_response_status(raw_attendees: list[dict[str, Any]]) -> str:
    """The PRINCIPAL's own responseStatus from the raw attendees array —
    Google marks the calendar owner's own entry with ``"self": true``
    (Phase 3 stage 2, Deliverable B). "" when the principal isn't an
    attendee (e.g. events they organize solo) or the backend omits it."""
    for attendee in raw_attendees:
        if attendee.get("self"):
            return attendee.get("responseStatus", "")
    return ""


def _parse_event_dt(dt_obj: dict[str, Any]) -> datetime:
    if "dateTime" in dt_obj:
        return datetime.fromisoformat(dt_obj["dateTime"])
    if "date" in dt_obj:
        return datetime.fromisoformat(dt_obj["date"]).replace(tzinfo=timezone.utc)
    return datetime.min
