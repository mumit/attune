"""``aidedecamp doctor`` — read-only validation with actionable fix hints
(roadmap prompt 08).

One PASS/FAIL/SKIP line per check; exit code 1 if anything FAILs. Every
check is an injected zero-arg callable returning ``(status, detail)``, so
tests fake the whole battery — the default battery does the real (read-only)
work with lazy imports and turns every exception into a FAIL with a hint
rather than a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

CheckFn = Callable[[], tuple[str, str]]

# Checks that must pass before `aidedecamp run` will start (see run_cmd.py).
FATAL_CHECKS = ("env", "data-dir", "fuelix", "google-credentials")


@dataclass
class Check:
    name: str
    fn: CheckFn


def run_doctor(
    checks: list[Check] | None = None,
    *,
    out: Callable[[str], None] = print,
    fatal_only: bool = False,
) -> int:
    """Run the battery, print one line per check, return the exit code."""
    checks = checks if checks is not None else build_checks()
    if fatal_only:
        checks = [c for c in checks if c.name in FATAL_CHECKS]

    failed = 0
    for check in checks:
        try:
            status, detail = check.fn()
        except Exception as exc:  # noqa: BLE001 — a crashing check is a FAIL, not a traceback
            status, detail = FAIL, f"check crashed: {type(exc).__name__}: {exc}"
        if status == FAIL:
            failed += 1
        out(f"{status:4}  {check.name:22} {detail}")

    out("")
    out("All checks passed." if failed == 0 else f"{failed} check(s) FAILED.")
    return 0 if failed == 0 else 1


# --- the default battery -----------------------------------------------------


def build_checks() -> list[Check]:  # pragma: no cover - thin assembly; each
    # check is exercised through run_doctor with injected fakes, and the real
    # ones need live services by definition.
    import os

    from ..config import Settings

    try:
        settings = Settings.from_env()
    except Exception as exc:  # noqa: BLE001
        # Everything downstream needs settings; report the one failure.
        msg = f"{type(exc).__name__}: {exc} — fix the ADC_* variable it names"
        return [Check("env", lambda: (FAIL, msg))]

    def check_env() -> tuple[str, str]:
        return PASS, f"deployment={settings.deployment.value}"

    def check_data_dir() -> tuple[str, str]:
        target = settings.data_dir or "."
        probe = os.path.join(target, ".adc-doctor-probe")
        try:
            os.makedirs(target, exist_ok=True)
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as exc:
            return FAIL, f"{target} not writable ({exc}) — set ADC_DATA_DIR"
        return PASS, target

    def check_fuelix() -> tuple[str, str]:
        if not os.environ.get("FUELIX_TOKEN"):
            return FAIL, "FUELIX_TOKEN not set — add it to .env"
        from bearer_openai import TokenRejectedError

        from ..fuelix import Task, make_client, model_for

        try:
            make_client().chat_completions_create(
                model=model_for(Task.CLASSIFY),
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
        except TokenRejectedError as exc:
            return FAIL, str(exc)
        except Exception as exc:  # noqa: BLE001
            return FAIL, f"gateway unreachable: {type(exc).__name__}"
        return PASS, "token accepted"

    def check_google_credentials() -> tuple[str, str]:
        from ..credentials import load_google_credentials

        load_google_credentials(settings)
        return PASS, settings.google_credentials_file or "ADC"

    def check_gmail_read() -> tuple[str, str]:
        from googleapiclient.discovery import build

        from ..credentials import load_google_credentials

        service = build(
            "gmail", "v1", credentials=load_google_credentials(settings)
        )
        profile = service.users().getProfile(userId="me").execute()
        return PASS, profile.get("emailAddress", "ok")

    def check_calendar_read() -> tuple[str, str]:
        from googleapiclient.discovery import build

        from ..credentials import load_google_credentials

        service = build(
            "calendar", "v3", credentials=load_google_credentials(settings)
        )
        service.calendars().get(calendarId=settings.calendar_id).execute()
        return PASS, settings.calendar_id

    def check_qdrant() -> tuple[str, str]:
        # Mem0 runs in-process. Its actual external dependency is Qdrant, not
        # the obsolete standalone Mem0 REST endpoint represented by mem0_url.
        host = os.environ.get("ADC_QDRANT_HOST", "localhost")
        port = int(os.environ.get("ADC_QDRANT_PORT", "6333"))
        url = f"http://{host}:{port}/readyz"
        import urllib.request

        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception as exc:  # noqa: BLE001
            return FAIL, (
                f"{host}:{port} unreachable ({type(exc).__name__}) — "
                "start packages/aidedecamp/deploy/compose.yml"
            )
        return PASS, f"{host}:{port}"

    def check_slack() -> tuple[str, str]:
        if not settings.slack_bot_token:
            return SKIP, "SLACK_BOT_TOKEN not set"
        from slack_sdk import WebClient

        resp = WebClient(token=settings.slack_bot_token).auth_test()
        return PASS, resp.get("team", "authenticated")

    def check_pubsub() -> tuple[str, str]:
        subscriptions = [
            s
            for s in (
                settings.gmail_pubsub_subscription,
                settings.chat_pubsub_subscription,
                settings.chat_interaction_pubsub_subscription,
                settings.calendar_pubsub_subscription,
            )
            if s
        ]
        if not subscriptions:
            return SKIP, "no Pub/Sub subscriptions configured (poll mode?)"
        from google.cloud import pubsub_v1

        subscriber = pubsub_v1.SubscriberClient()
        for sub in subscriptions:
            subscriber.get_subscription(request={"subscription": sub})
        return PASS, f"{len(subscriptions)} subscription(s) exist"

    return [
        Check("env", check_env),
        Check("data-dir", check_data_dir),
        Check("fuelix", check_fuelix),
        Check("google-credentials", check_google_credentials),
        Check("gmail-read", check_gmail_read),
        Check("calendar-read", check_calendar_read),
        Check("qdrant", check_qdrant),
        Check("slack", check_slack),
        Check("pubsub", check_pubsub),
    ]
