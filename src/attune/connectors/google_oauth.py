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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from ..retry import retry_call
from .base import (
    MAX_THREAD_BODY_CHARS,
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

# Build prompt 33, task 2: kill the N+1 in list_threads.
#
# Google batch endpoints cap sub-requests per HTTP batch (historically 50,
# occasionally lower depending on the API) -- chunk defensively even though
# every caller in this codebase today passes max_results<=25.
BATCH_CHUNK_SIZE = 50
# Bounded fallback concurrency (build prompt 33, task 1's own instruction:
# "list_threads' own per-thread hydration" needs a bounded worker pool) when
# BatchHttpRequest is unavailable -- Google will rate-limit unbounded fan-out
# (the build prompt's own constraint), so this is a constant, not unbounded.
HYDRATION_POOL_SIZE = 8

# The exact Gmail headers _thread_from_metadata/_thread_from_full actually
# read (From, Subject, Reply-To -- see _header/_reply_target below):
# restricting metadataHeaders to just these means the metadata format
# doesn't transfer every header on every message.
_METADATA_HEADERS = ["From", "Subject", "Reply-To"]
# fields= partial-response masks (build prompt 33, task 2): request only the
# JSON keys _thread_from_metadata/_event_from_google/_self_response_status
# actually read. threads.get(format="metadata") still needs the full
# messages(payload/headers) shape for the headers above, plus snippet/
# internalDate/labelIds -- masking headers themselves happens via
# metadataHeaders, not fields.
_THREAD_LIST_FIELDS = "threads/id"
_THREAD_METADATA_FIELDS = (
    "id,messages(id,snippet,internalDate,labelIds,payload/headers)"
)
# get_thread (format="full") needs the full payload to decode the body --
# no header restriction here (format=full ignores metadataHeaders anyway),
# but still trims the top-level response to what's actually read.
_THREAD_FULL_FIELDS = "id,messages(id,snippet,internalDate,labelIds,payload)"
_EVENT_LIST_FIELDS = "items(id,summary,start,end,attendees,organizer)"
_EVENT_GET_FIELDS = "id,summary,start,end,attendees,organizer"


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
        ids = self._list_thread_id_page(query, max_results=max_results)
        if not ids:
            return []
        details = self._hydrate_threads(ids)
        return [
            _thread_from_metadata(details[tid], owner_email=self._owner_email)
            for tid in ids
            if tid in details
        ]

    def list_thread_ids(
        self, query: str = "is:unread", *, max_results: int = 20
    ) -> list[str]:
        """Cheap ID-only listing (build prompt 33, task 5's incremental-brief
        need — see ``connectors.base.WorkspaceConnector.list_thread_ids``):
        one ``threads.list`` call, fields-masked to just the id, and NO
        per-thread hydration at all."""
        return self._list_thread_id_page(query, max_results=max_results)

    def _list_thread_id_page(self, query: str, *, max_results: int) -> list[str]:
        gmail = self._gmail()
        res = retry_call(
            lambda: gmail.users()
            .threads()
            .list(
                userId=_USER, q=query, maxResults=max_results,
                fields=_THREAD_LIST_FIELDS,
            )
            .execute()
        )
        return [item["id"] for item in res.get("threads", [])]

    def _hydrate_threads(self, ids: list[str]) -> dict[str, Any]:
        """Fetch full metadata for every id in ``ids``, keyed by id.
        Prefers ``BatchHttpRequest`` (one or a few HTTP round trips instead
        of one per thread); degrades to a bounded thread pool when batching
        is unavailable (a service object without ``new_batch_http_request``,
        or the batch attempt itself raising) — never a crash, and never an
        unbounded serial loop. A single thread's fetch failing (batch
        per-item error, or a fallback future raising) is skipped rather
        than failing the whole page, matching the pre-existing per-thread
        loop's tolerance (a bad thread simply never appears in the result)."""
        gmail = self._gmail()
        new_batch = getattr(gmail, "new_batch_http_request", None)
        if new_batch is not None:
            try:
                return self._hydrate_threads_batch(gmail, ids, new_batch)
            except Exception:  # noqa: BLE001 - fall back rather than fail the page
                pass
        return self._hydrate_threads_fallback(gmail, ids)

    def _hydrate_threads_batch(
        self, gmail: Any, ids: list[str], new_batch_factory: Any
    ) -> dict[str, Any]:
        results: dict[str, Any] = {}

        def _callback(request_id: str, response: Any, exception: Any) -> None:
            if exception is not None:
                return
            results[request_id] = response

        for chunk in _chunked(ids, BATCH_CHUNK_SIZE):
            batch = new_batch_factory(callback=_callback)
            for tid in chunk:
                batch.add(
                    gmail.users().threads().get(
                        userId=_USER, id=tid, format="metadata",
                        metadataHeaders=_METADATA_HEADERS,
                        fields=_THREAD_METADATA_FIELDS,
                    ),
                    request_id=tid,
                )
            retry_call(lambda b=batch: b.execute())
        return results

    def _hydrate_threads_fallback(self, gmail: Any, ids: list[str]) -> dict[str, Any]:
        def _fetch_one(tid: str) -> "tuple[str, Any]":
            detail = retry_call(
                lambda: gmail.users()
                .threads()
                .get(
                    userId=_USER, id=tid, format="metadata",
                    metadataHeaders=_METADATA_HEADERS,
                    fields=_THREAD_METADATA_FIELDS,
                )
                .execute()
            )
            return tid, detail

        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=min(HYDRATION_POOL_SIZE, len(ids))) as pool:
            futures = [pool.submit(_fetch_one, tid) for tid in ids]
            for future in futures:
                try:
                    tid, detail = future.result()
                except Exception:  # noqa: BLE001 - one bad thread must not sink the page
                    continue
                results[tid] = detail
        return results

    def get_thread(self, thread_id: str) -> EmailThread:
        gmail = self._gmail()
        detail = retry_call(
            lambda: gmail.users()
            .threads()
            .get(
                userId=_USER, id=thread_id, format="full",
                fields=_THREAD_FULL_FIELDS,
            )
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

    def delete_draft(self, draft_id: str) -> None:
        """Delete a draft via ``drafts.delete`` (build prompt 31) — the
        undo path for DRAFT_REPLY/FOLLOW_UP. No double gate: the same
        ``gmail.compose`` scope ``create_draft`` already requires covers
        deleting a draft the connector itself created."""
        self._gmail().users().drafts().delete(userId=_USER, id=draft_id).execute()

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

    def supports_sending(self) -> bool:
        """Mirrors ``send_enabled`` directly (Phase 4 stage 2, G15) — unlike
        ``supports_labeling``/``supports_calendar_writes``, which report
        unconditional backend capability, this IS the enabled posture: the
        connector was constructed with ``send_enabled=True`` (which, by the
        class's own discipline, is only ever set alongside a real
        gmail.send scope and an explicit autonomy grant)."""
        return self._send_enabled

    def add_label(self, *, thread_id: str, label: str) -> None:
        label_id = self._resolve_label_id(label)
        self._gmail().users().threads().modify(
            userId=_USER,
            id=thread_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    def supports_add_label(self) -> bool:
        """Structural capability: the direct-OAuth backend CAN add a label
        with the same ``gmail.compose``-adjacent access ``create_draft``
        already requires — see :meth:`add_label`'s own "low-risk,
        organizational" posture (no deployment opt-in flag, unlike
        :meth:`label_thread`/:meth:`remove_label`/:meth:`mark_read`, which
        all need the removal-capable ``gmail.modify`` scope)."""
        return True

    def remove_label(self, thread_id: str, *, label: str) -> None:
        """Remove ``label`` from a Gmail thread via ``threads.modify``
        (build prompt 30, task 6.1) — same double-gate discipline as
        :meth:`label_thread`: refuses unless ``labels_enabled`` was
        explicitly set alongside the gmail.modify scope."""
        if not self._labels_enabled:
            raise LabelNotPermitted(
                "DirectOAuthConnector labeling disabled: requires the "
                "gmail.modify scope AND ATTUNE_MAIL_LABELS_ENABLED=1. "
                "Draft-only is the default; nothing is archived silently."
            )
        label_id = self._resolve_label_id_cached(label)
        self._gmail().users().threads().modify(
            userId=_USER, id=thread_id, body={"removeLabelIds": [label_id]},
        ).execute()

    def mark_read(self, thread_id: str) -> None:
        """Remove Gmail's UNREAD label via ``threads.modify`` (build prompt
        30, task 6.1) — mechanically :meth:`remove_label` with
        ``label="UNREAD"``, but UNREAD is a Gmail SYSTEM label (no lookup/
        creation round trip the way a user label needs — see
        :meth:`_resolve_label_id_cached`), so this calls ``modify``
        directly with the literal id rather than resolving one. Same
        double-gate discipline as :meth:`label_thread`/:meth:`remove_label`."""
        if not self._labels_enabled:
            raise LabelNotPermitted(
                "DirectOAuthConnector labeling disabled: requires the "
                "gmail.modify scope AND ATTUNE_MAIL_LABELS_ENABLED=1. "
                "Draft-only is the default; nothing is archived silently."
            )
        self._gmail().users().threads().modify(
            userId=_USER, id=thread_id, body={"removeLabelIds": ["UNREAD"]},
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
        calendar = self._calendar()
        res = retry_call(
            lambda: calendar.events()
            .list(
                calendarId="primary",
                timeMin=_to_rfc3339(time_min),
                timeMax=_to_rfc3339(time_max),
                singleEvents=True,
                orderBy="startTime",
                fields=_EVENT_LIST_FIELDS,
            )
            .execute()
        )
        return [
            _event_from_google(e, self._internal_domains)
            for e in res.get("items", [])
        ]

    def supports_freebusy(self) -> bool:
        """Structural capability: the direct-OAuth backend CAN query
        free/busy via ``calendar.freebusy.query`` (build prompt 30, task
        6.3) — read-only, no additional scope beyond what
        ``calendar.readonly`` (already requested for every deployment)
        covers."""
        return True

    def free_busy(
        self, emails: list[str], *, time_min: datetime, time_max: datetime
    ) -> "dict[str, list[tuple[datetime, datetime]]]":
        if not emails:
            return {}
        res = (
            self._calendar()
            .freebusy()
            .query(body={
                "timeMin": _to_rfc3339(time_min),
                "timeMax": _to_rfc3339(time_max),
                "items": [{"id": email} for email in emails],
            })
            .execute()
        )
        result: "dict[str, list[tuple[datetime, datetime]]]" = {}
        for email, data in res.get("calendars", {}).items():
            # An attendee with no reported visibility (Google returns an
            # "errors" entry rather than a "busy" list) is simply absent
            # here — never raises, see this method's own docstring.
            if "errors" in (data or {}):
                continue
            result[email] = [
                (
                    datetime.fromisoformat(block["start"]),
                    datetime.fromisoformat(block["end"]),
                )
                for block in (data or {}).get("busy", [])
            ]
        return result

    def get_event(self, event_id: str) -> CalendarEvent:
        calendar = self._calendar()
        detail = retry_call(
            lambda: calendar.events()
            .get(calendarId="primary", eventId=event_id, fields=_EVENT_GET_FIELDS)
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

    def delete_event(self, event_id: str) -> None:
        """Delete a calendar event via ``events.delete`` (build prompt 31)
        — the undo path for CREATE_HOLD. No double gate: the same calendar
        scope ``create_hold`` already uses covers deleting an event this
        connector itself created."""
        self._calendar().events().delete(
            calendarId="primary", eventId=event_id
        ).execute()

    def supports_calendar_writes(self) -> bool:
        """Structural capability, independent of whether it's turned on —
        the direct-OAuth backend CAN decline/reschedule (unlike MCP
        contract v1, which has neither tool). ``decline_invite``/
        ``reschedule_event`` still refuse unless ``calendar_writes_enabled``
        was set (the second, deployment-level gate)."""
        return True

    def decline_invite(
        self, event_id: str, *, current: CalendarEvent | None = None
    ) -> None:
        """Patch the PRINCIPAL's own attendee responseStatus to "declined"
        (Phase 3 stage 2, Deliverable A) — never touches any other
        attendee's entry. See :meth:`_respond_to_invite` for the shared
        fetch/patch mechanics every RSVP response (this, :meth:`accept_
        invite`, :meth:`tentative_invite`) uses.

        ``current`` (build prompt 33, task 3): when supplied with a
        populated ``raw_attendees`` (i.e. it came from THIS connector's own
        :meth:`get_event`), skips the internal ``events.get`` and reuses
        ``current.raw_attendees`` to build the patch body — see
        :meth:`WorkspaceConnector.decline_invite`'s docstring for why this is
        safe (the caller's own late, same-call freshness fetch, not cached
        state).

        Refuses unless ``calendar_writes_enabled`` was explicitly set
        alongside the calendar write scope (the same double-gate discipline
        as ``label_thread``/``labels_enabled``)."""
        self._respond_to_invite(event_id, "declined", current=current)

    def accept_invite(self, event_id: str) -> None:
        """Patch the PRINCIPAL's own attendee responseStatus to "accepted"
        (build prompt 30, task 6.2) — the positive counterpart to
        :meth:`decline_invite`, same mechanics via
        :meth:`_respond_to_invite`. Same double-gate discipline."""
        self._respond_to_invite(event_id, "accepted")

    def tentative_invite(self, event_id: str) -> None:
        """Patch the PRINCIPAL's own attendee responseStatus to "tentative"
        (build prompt 30, task 6.2) — same mechanics via
        :meth:`_respond_to_invite`. Same double-gate discipline."""
        self._respond_to_invite(event_id, "tentative")

    def reset_invite_response(self, event_id: str) -> None:
        """Reset the principal's own responseStatus to "needsAction" (build
        prompt 31) — the undo path for DECLINE_INVITE, same mechanics via
        :meth:`_respond_to_invite`. Same double-gate discipline."""
        self._respond_to_invite(event_id, "needsAction")

    def _respond_to_invite(
        self,
        event_id: str,
        response_status: str,
        *,
        current: CalendarEvent | None = None,
    ) -> None:
        """Shared RSVP mechanics for :meth:`decline_invite`/
        :meth:`accept_invite`/:meth:`tentative_invite` (build prompt 30,
        task 6.2) via ``events.patch`` — never touches any other
        attendee's entry. Calendar's PATCH replaces the whole
        ``attendees`` array rather than merging into it, so this needs the
        FULL raw attendees array, flips only the principal's own entry, and
        sends the full array back.

        ``current`` (build prompt 33, task 3): when given with a non-empty
        ``raw_attendees``, that array is reused instead of performing a
        second ``events.get`` -- ``accept_invite``/``tentative_invite``/
        ``reset_invite_response`` never pass it, so they keep today's exact
        behavior (an internal fresh fetch)."""
        if not self._calendar_writes_enabled:
            raise CalendarWriteNotPermitted(
                "DirectOAuthConnector calendar writes disabled: requires "
                "the calendar.events scope AND "
                "ATTUNE_CALENDAR_WRITES_ENABLED=1. Read-only is the "
                "default; nothing is declined or rescheduled silently."
            )
        if current is not None and current.raw_attendees:
            raw_attendees = current.raw_attendees
        else:
            calendar = self._calendar()
            event = retry_call(
                lambda: calendar.events()
                .get(calendarId="primary", eventId=event_id, fields="attendees")
                .execute()
            )
            raw_attendees = event.get("attendees", [])
        updated_attendees = []
        found_self = False
        for attendee in raw_attendees:
            entry = dict(attendee)
            if self._is_self_attendee(entry):
                entry["responseStatus"] = response_status
                found_self = True
            updated_attendees.append(entry)
        if not found_self:
            raise CalendarWriteNotPermitted(
                f"cannot respond to {event_id}: the principal is not "
                "listed as an attendee on this event"
            )
        calendar = self._calendar()
        retry_call(
            lambda: calendar.events()
            .patch(
                calendarId="primary",
                eventId=event_id,
                body={"attendees": updated_attendees},
            )
            .execute()
        )

    def reschedule_event(
        self,
        event_id: str,
        *,
        new_start: datetime,
        new_end: datetime,
        current: CalendarEvent | None = None,
    ) -> None:
        """Move ``event_id`` to a new start/end via ``events.patch`` (Phase
        3 stage 2, Deliverable A). Refuses (``CalendarWriteNotPermitted``)
        unless the principal is this event's ORGANIZER.

        ``current`` (build prompt 33, task 3): when supplied, reads
        ``current.organizer_is_self`` instead of performing a second
        ``events.get`` -- this is STILL "verified from a fresh fetch": the
        organizer check now runs against whichever fetch is fresher (the
        caller's own late, same-call ``get_event``, per
        ``WorkspaceConnector.reschedule_event``'s docstring), never from a
        cached checkpoint or proposal-time snapshot. Omitting ``current``
        preserves exactly today's behavior -- an internal fresh fetch,
        performed right here."""
        if not self._calendar_writes_enabled:
            raise CalendarWriteNotPermitted(
                "DirectOAuthConnector calendar writes disabled: requires "
                "the calendar.events scope AND "
                "ATTUNE_CALENDAR_WRITES_ENABLED=1. Read-only is the "
                "default; nothing is declined or rescheduled silently."
            )
        if current is not None:
            organizer_is_self = current.organizer_is_self
        else:
            calendar = self._calendar()
            event = retry_call(
                lambda: calendar.events()
                .get(calendarId="primary", eventId=event_id, fields="organizer")
                .execute()
            )
            organizer = event.get("organizer", {}) or {}
            organizer_is_self = self._is_self_attendee(organizer)
        if not organizer_is_self:
            raise CalendarWriteNotPermitted(
                f"cannot reschedule {event_id}: the principal is not this "
                "event's organizer"
            )
        calendar = self._calendar()
        retry_call(
            lambda: calendar.events()
            .patch(
                calendarId="primary",
                eventId=event_id,
                body={
                    "start": {"dateTime": new_start.isoformat()},
                    "end": {"dateTime": new_end.isoformat()},
                },
            )
            .execute()
        )

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


def _chunked(items: list[str], size: int) -> "list[list[str]]":
    return [items[i : i + size] for i in range(0, len(items), size)]


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
    body = (_decode_body(last.get("payload", {})) or last.get("snippet", ""))[
        :MAX_THREAD_BODY_CHARS
    ]
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
    raw_attendees = data.get("attendees", [])
    attendees = [a["email"] for a in raw_attendees if "email" in a]
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
        response_status=_self_response_status(raw_attendees),
        # Build prompt 33, task 3: verbatim raw entries so decline_invite's
        # `current` can skip a second events.get purely to rebuild the full
        # attendees array a PATCH must resend in full — see base.py's
        # CalendarEvent.raw_attendees docstring.
        raw_attendees=[dict(a) for a in raw_attendees],
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
