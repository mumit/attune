"""Fixed, bounded Google operations for the hosted secret broker."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol
from urllib.parse import quote

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_PRIMARY_URL = "https://www.googleapis.com/calendar/v3/calendars/primary"
GMAIL_THREADS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/threads"
CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
MAX_PROVIDER_RESPONSE_BYTES = 32_768
MAX_ACCESS_TOKEN_CHARS = 8_192
REQUEST_TIMEOUT = (3.05, 10)
_GMAIL_RESOURCE = re.compile(r"^[A-Za-z0-9_-]{1,180}$")


class ProviderFailure(RuntimeError):
    """A content-free provider failure safe for broker control flow."""


class HttpSession(Protocol):
    def post(self, url: str, **kwargs: Any): ...

    def get(self, url: str, **kwargs: Any): ...


@dataclass(frozen=True)
class GmailProfile:
    history_id: str
    messages_total: int
    threads_total: int

    def response(self) -> dict[str, Any]:
        # Google also returns emailAddress. The broker deliberately omits it.
        return {
            "history_id": self.history_id,
            "messages_total": self.messages_total,
            "threads_total": self.threads_total,
        }


@dataclass(frozen=True)
class CalendarPrimary:
    """Content-free proof that the canonical primary calendar was readable."""


@dataclass(frozen=True)
class GmailThreadSummary:
    thread_id: str
    subject: str
    sender: str
    date: str
    snippet: str

    def response(self) -> dict[str, str]:
        return {
            "thread_id": self.thread_id,
            "subject": self.subject,
            "sender": self.sender,
            "date": self.date,
            "snippet": self.snippet,
        }


@dataclass(frozen=True)
class CalendarEventSummary:
    event_id: str
    summary: str
    start: str
    end: str
    location: str
    status: str

    def response(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "summary": self.summary,
            "start": self.start,
            "end": self.end,
            "location": self.location,
            "status": self.status,
        }


class GoogleProvider:
    """Construct only reviewed Google requests; never accept URLs from callers."""

    def __init__(self, session: HttpSession | None = None):
        if session is None:
            import requests

            session = requests.Session()
            # Provider credentials must not be routed through ambient proxy
            # variables inherited from the process environment.
            session.trust_env = False
        self._session = session

    def gmail_profile(self, credential: Mapping[str, Any]) -> GmailProfile:
        oauth = _authorized_user_credential(credential, GMAIL_READONLY_SCOPE)
        access_token = self._access_token(oauth)
        profile = self._get_json(GMAIL_PROFILE_URL, access_token)
        history_id = profile.get("historyId")
        messages_total = profile.get("messagesTotal")
        threads_total = profile.get("threadsTotal")
        if (
            not isinstance(history_id, str)
            or not history_id.isdecimal()
            or len(history_id) > 32
            or not _bounded_count(messages_total)
            or not _bounded_count(threads_total)
        ):
            raise ProviderFailure("invalid Gmail profile response")
        return GmailProfile(history_id, messages_total, threads_total)

    def calendar_primary(self, credential: Mapping[str, Any]) -> CalendarPrimary:
        oauth = _authorized_user_credential(credential, CALENDAR_READONLY_SCOPE)
        access_token = self._access_token(oauth)
        calendar = self._get_json(CALENDAR_PRIMARY_URL, access_token)
        calendar_id = calendar.get("id")
        timezone_name = calendar.get("timeZone")
        if (
            not isinstance(calendar_id, str)
            or not 1 <= len(calendar_id) <= 1024
            or not isinstance(timezone_name, str)
            or not 1 <= len(timezone_name) <= 255
        ):
            raise ProviderFailure("invalid Calendar response")
        return CalendarPrimary()

    def gmail_threads(
        self, credential: Mapping[str, Any], *, query: str, limit: int
    ) -> tuple[GmailThreadSummary, ...]:
        if not isinstance(query, str) or not 1 <= len(query) <= 300:
            raise ValueError("Gmail query is invalid")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("Gmail result limit is invalid")
        oauth = _authorized_user_credential(credential, GMAIL_READONLY_SCOPE)
        access_token = self._access_token(oauth)
        listing = self._get_json(
            GMAIL_THREADS_URL,
            access_token,
            params={"q": query, "maxResults": limit, "includeSpamTrash": "false"},
        )
        raw_threads = listing.get("threads", [])
        if not isinstance(raw_threads, list) or len(raw_threads) > limit:
            raise ProviderFailure("invalid Gmail thread listing")
        summaries: list[GmailThreadSummary] = []
        for raw_thread in raw_threads:
            thread_id = raw_thread.get("id") if isinstance(raw_thread, dict) else None
            if not isinstance(thread_id, str) or not _GMAIL_RESOURCE.fullmatch(thread_id):
                raise ProviderFailure("invalid Gmail thread reference")
            detail = self._get_json(
                f"{GMAIL_THREADS_URL}/{quote(thread_id, safe='')}",
                access_token,
                params={
                    "format": "metadata",
                    "metadataHeaders": ["Subject", "From", "Date"],
                },
            )
            summaries.append(_gmail_thread_summary(thread_id, detail))
        return tuple(summaries)

    def calendar_events(
        self,
        credential: Mapping[str, Any],
        *,
        time_min: datetime,
        time_max: datetime,
        limit: int,
    ) -> tuple[CalendarEventSummary, ...]:
        if (
            not isinstance(time_min, datetime)
            or not isinstance(time_max, datetime)
            or time_min.tzinfo is None
            or time_max.tzinfo is None
            or time_max <= time_min
            or time_max - time_min > timedelta(days=31)
        ):
            raise ValueError("Calendar window is invalid")
        if type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("Calendar result limit is invalid")
        oauth = _authorized_user_credential(credential, CALENDAR_READONLY_SCOPE)
        access_token = self._access_token(oauth)
        listing = self._get_json(
            CALENDAR_EVENTS_URL,
            access_token,
            params={
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "maxResults": limit,
                "singleEvents": "true",
                "orderBy": "startTime",
                "showDeleted": "false",
            },
        )
        items = listing.get("items", [])
        if not isinstance(items, list) or len(items) > limit:
            raise ProviderFailure("invalid Calendar event listing")
        return tuple(_calendar_event_summary(item) for item in items)

    def _access_token(self, oauth: Mapping[str, str]) -> str:
        try:
            token_response = self._session.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": oauth["refresh_token"],
                    "client_id": oauth["client_id"],
                    "client_secret": oauth["client_secret"],
                },
                headers={"Accept": "application/json"},
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
                stream=True,
            )
        except Exception as error:
            raise ProviderFailure("token request failed") from error
        token_body = _json_response(token_response, expected_status=200)
        access_token = token_body.get("access_token")
        token_type = token_body.get("token_type")
        if (
            not isinstance(access_token, str)
            or not 1 <= len(access_token) <= MAX_ACCESS_TOKEN_CHARS
            or any(character.isspace() for character in access_token)
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
        ):
            raise ProviderFailure("invalid token response")

        return access_token

    def _get_json(
        self, url: str, access_token: str, *, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = self._session.get(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                params=params,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
                stream=True,
            )
        except Exception as error:
            raise ProviderFailure("provider read failed") from error
        return _json_response(response, expected_status=200)


def _authorized_user_credential(
    value: Mapping[str, Any], required_scope: str
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ProviderFailure("invalid credential")
    required = ("refresh_token", "client_id", "client_secret")
    parsed: dict[str, str] = {}
    for field in required:
        candidate = value.get(field)
        if not isinstance(candidate, str) or not 1 <= len(candidate) <= 8_192:
            raise ProviderFailure("invalid credential")
        parsed[field] = candidate
    token_uri = value.get("token_uri", GOOGLE_TOKEN_URL)
    if token_uri != GOOGLE_TOKEN_URL:
        raise ProviderFailure("unapproved token endpoint")
    scopes = value.get("scopes")
    if scopes is not None:
        if (
            not isinstance(scopes, list)
            or not all(isinstance(scope, str) for scope in scopes)
            or required_scope not in scopes
        ):
            raise ProviderFailure("required scope is unavailable")
    return parsed


def _json_response(response: Any, *, expected_status: int) -> dict[str, Any]:
    try:
        if response.status_code != expected_status:
            raise ProviderFailure("provider rejected request")
        raw = response.raw.read(MAX_PROVIDER_RESPONSE_BYTES + 1, decode_content=True)
        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ProviderFailure("provider response exceeds limit")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ProviderFailure("provider response must be an object")
        return parsed
    except ProviderFailure:
        raise
    except Exception as error:
        raise ProviderFailure("provider request failed") from error
    finally:
        try:
            response.close()
        except Exception:
            pass


def _bounded_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**63


def _bounded_provider_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ProviderFailure("provider text field is invalid")
    return value


def _gmail_thread_summary(thread_id: str, detail: Mapping[str, Any]) -> GmailThreadSummary:
    messages = detail.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
        raise ProviderFailure("invalid Gmail thread detail")
    latest = messages[-1]
    if not isinstance(latest, dict):
        raise ProviderFailure("invalid Gmail message detail")
    payload = latest.get("payload")
    headers = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(headers, list) or len(headers) > 200:
        raise ProviderFailure("invalid Gmail headers")
    selected = {"subject": "", "from": "", "date": ""}
    for header in headers:
        if not isinstance(header, dict):
            raise ProviderFailure("invalid Gmail header")
        name = header.get("name")
        value = header.get("value")
        if isinstance(name, str) and name.lower() in selected:
            selected[name.lower()] = _bounded_provider_text(value, 2_000)
    return GmailThreadSummary(
        thread_id=thread_id,
        subject=selected["subject"],
        sender=selected["from"],
        date=selected["date"],
        snippet=_bounded_provider_text(latest.get("snippet", ""), 1_000),
    )


def _calendar_event_summary(item: Any) -> CalendarEventSummary:
    if not isinstance(item, dict):
        raise ProviderFailure("invalid Calendar event")
    event_id = item.get("id")
    if not isinstance(event_id, str) or not _GMAIL_RESOURCE.fullmatch(event_id):
        raise ProviderFailure("invalid Calendar event reference")
    start = item.get("start")
    end = item.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        raise ProviderFailure("invalid Calendar event window")
    start_value = start.get("dateTime", start.get("date"))
    end_value = end.get("dateTime", end.get("date"))
    if not isinstance(start_value, str) or not start_value:
        raise ProviderFailure("invalid Calendar event start")
    if not isinstance(end_value, str) or not end_value:
        raise ProviderFailure("invalid Calendar event end")
    return CalendarEventSummary(
        event_id=event_id,
        summary=_bounded_provider_text(item.get("summary", ""), 2_000),
        start=_bounded_provider_text(start_value, 128),
        end=_bounded_provider_text(end_value, 128),
        location=_bounded_provider_text(item.get("location", ""), 2_000),
        status=_bounded_provider_text(item.get("status", ""), 64),
    )
