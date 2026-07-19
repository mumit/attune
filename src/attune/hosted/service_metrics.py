"""Content-free structured request/task metrics for hosted Flask services.

Phase 6 SLO-grade observability (``docs/future-state.md`` "hosted
operations"; ``docs/gap-analysis.md`` G19; hosted review gap #8: seven
job-failure alert policies existed and nothing gave latency, error-rate,
or per-service health visibility).

Call :func:`instrument_service_metrics` on a Flask app immediately after
construction, once, with the service's fixed name. It installs a
before/after_request pair that emits exactly one line per request::

    {"metric": "http_request", "service": <fixed>, "route": <matched Flask
     URL rule template>, "method": <str>, "status_class": "2xx".."5xx",
     "status": <int>, "duration_ms": <int>}

Content-free by construction (``docs/decisions.md``: "Content-free anomaly
markers drive an operational alert; tenant or provider content is not
copied into logs or metric labels"). Only fixed-vocabulary fields are ever
read:

- ``route`` is the Flask URL *rule template* (``request.url_rule.rule``,
  e.g. ``/v1/connectors/google/tests/<uuid:job_id>``) -- never
  ``request.path``, so a real UUID, token, or other identifier substituted
  into a templated route never reaches a log line or a metric label. A
  request that never matched a route (a 404 before dispatch) reports
  ``"unmatched"``.
- No query string, request or response body, ``Authorization`` or other
  header, ``User-Agent``, or client IP is ever read.
- No tenant or principal identifier is read; these hooks run below every
  application route and have no notion of either.

:func:`emit_task_execution` is the equivalent one-line-per-task signal for
the worker's dispatch seam (``worker_dispatch.py``): ``{"metric":
"task_execution", "task": <fixed registered kind>, "outcome": <fixed
vocabulary>, "duration_ms": <int>}``.

Both emitters use a bare ``print(json.dumps(...), flush=True)`` rather than
the ``logging`` module -- matching the existing structured-log precedent in
``protocol_retention.py``, ``export_cleanup.py``, and
``content_retention.py``. This matters here specifically: these hosted
Flask services run under gunicorn with no ``logging.basicConfig`` call
anywhere (unlike the local runtime's ``logging_setup.py``), so the root
logger's default level is ``WARNING`` and every existing per-request
``LOG.info`` call in these modules (e.g. ``control_plane_service.py``'s
``hosted_signup_attempted``) is already silently dropped before it reaches
a handler. A per-request operational signal that must reliably reach Cloud
Logging cannot depend on that. Cloud Run parses a bare JSON stdout/stderr
line into ``jsonPayload`` automatically regardless of logger
configuration, exactly like the retention jobs already rely on.

A failure anywhere in either emitter is caught and swallowed (with a
content-free ``logging`` breadcrumb at DEBUG) so instrumentation can never
break the request or task it is observing.
"""

from __future__ import annotations

import json
import logging
import time

LOG = logging.getLogger(__name__)

METRIC_HTTP_REQUEST = "http_request"
METRIC_TASK_EXECUTION = "task_execution"

_START_ATTR = "_attune_metrics_start"


def _emit_metric_line(payload: dict) -> None:
    try:
        print(json.dumps(payload, sort_keys=True), flush=True)
    except Exception:
        LOG.debug("service metrics emit failed", exc_info=True)


def instrument_service_metrics(app, *, service: str) -> None:
    """Install the before/after_request pair that emits the http_request line.

    Call once per app, immediately after construction, before any other
    ``before_request``/``after_request`` hook or route is registered:
    Flask runs ``before_request`` functions in registration order and
    ``after_request`` functions in *reverse* registration order, so
    registering first here means this module's timer starts before any
    other hook runs and its emission happens last -- after every other
    hook has finished mutating the response -- giving an accurate status
    and duration for the request actually sent.
    """

    from flask import g, request

    @app.before_request
    def _attune_metrics_before():
        try:
            setattr(g, _START_ATTR, time.monotonic())
        except Exception:
            LOG.debug("service metrics before-hook failed", exc_info=True)

    @app.after_request
    def _attune_metrics_after(response):
        try:
            start = getattr(g, _START_ATTR, None)
            duration_ms = (
                int((time.monotonic() - start) * 1000) if start is not None else 0
            )
            rule = request.url_rule
            status = response.status_code
            _emit_metric_line(
                {
                    "metric": METRIC_HTTP_REQUEST,
                    "service": service,
                    "route": rule.rule if rule is not None else "unmatched",
                    "method": request.method,
                    "status_class": f"{status // 100}xx",
                    "status": status,
                    "duration_ms": duration_ms,
                }
            )
        except Exception:
            LOG.debug("service metrics after-hook failed", exc_info=True)
        return response


def emit_task_execution(*, task: str, outcome: str, duration_ms: int) -> None:
    """Emit one content-free ``task_execution`` line for the worker dispatch seam."""

    _emit_metric_line(
        {
            "metric": METRIC_TASK_EXECUTION,
            "task": task,
            "outcome": outcome,
            "duration_ms": duration_ms,
        }
    )
