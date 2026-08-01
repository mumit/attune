"""The git-backed playbook store (build prompt 29, task 1/2/5/6).

An ACE-style learned policy: one Markdown file per domain, each holding
**bullets, not prose** — a stable id, the rule text, provenance (the
decision-ledger proposal ids that produced it), timestamps, and
``helped``/``harmed`` outcome counters. The whole directory is its own git
repository; every mutation is one commit, so the principal can ``git log``,
``git diff``, and ``git revert`` a single learned belief — the same
auditability property the hash-chained audit log gives effects, applied to
beliefs (see the module's own build prompt, ``docs/build-prompts/29-playbook.md``).

**Delta edits only.** :class:`GitPlaybookStore` exposes exactly three
mutating operations over an existing bullet — :meth:`refine_bullet`,
:meth:`retire_bullet`, :meth:`record_outcome` — plus :meth:`add_bullet` for a
genuinely new one. There is no "rewrite the file" method anywhere in this
class, deliberately: ACE's finding is that full rewrites cause brevity bias
(summarization silently drops the specific insight that made a rule useful)
and context collapse (iterative rewriting erodes detail). Every mutation
here touches exactly one bullet's block and leaves every other bullet's
text byte-for-byte unchanged.

**Bounded by construction** (never a tunable environment variable — this is
operational state, the same posture ``attention.py`` already applies to its
own bounds): :data:`MAX_BULLETS_PER_FILE`, :data:`MAX_CHARS_PER_BULLET`,
:data:`DOMAINS` (a fixed, closed set of files), :data:`MAX_NEW_BULLETS_PER_DAY`
(enforced by the reflector, ``reflector.py``, not here — this module has no
notion of "today's proposals", only "how many bullets already carry
``created_at`` on a given date", which :meth:`count_created_on` answers for
the reflector's ratchet check).

**One-line-per-field block format**, not YAML/JSON: each bullet is a
``### <id>`` header followed by ``- key: value`` lines. One bullet is one
contiguous block, so refining/retiring/recording an outcome for bullet A
never touches the diff of bullet B or C — the git history stays legible
(the whole reason this is git-backed markdown rather than a JSON blob).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# The fixed, closed set of playbook domains (build prompt 29, task 1). Not
# derived from ``orchestrator.autonomy.Domain`` (MAIL/CALENDAR/CHAT/SLACK):
# the playbook's own domain list is product-defined and intentionally
# includes "voice" and "scheduling", which are not autonomy domains today —
# keeping this list independent means a playbook file can exist ahead of
# any autonomy wiring for that surface.
DOMAINS: tuple[str, ...] = ("mail", "calendar", "voice", "scheduling")

MAX_BULLETS_PER_FILE = 40
MAX_CHARS_PER_BULLET = 280
MAX_NEW_BULLETS_PER_DAY = 3

# The bounded prompt-slice defaults for :meth:`GitPlaybookStore.render_slice`
# (task 5): "never dump everything" — ExpeL's documented failure mode.
MAX_SLICE_BULLETS = 12
MAX_SLICE_CHARS = 2_000

def _new_id(now: datetime) -> str:
    """``b_<yyyymmdd>_<6 hex>`` — sortable-ish, collision-safe within a file,
    never reused across domains (git-tracked, never recycled)."""
    return f"b_{now.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"


@dataclass
class Bullet:
    """One learned rule. See the module docstring for why mutation is
    delta-only — nothing here is ever silently rewritten wholesale."""

    id: str
    domain: str
    text: str
    provenance: tuple[str, ...] = ()
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_used_at: datetime | None = None
    helped: int = 0
    harmed: int = 0
    pinned: bool = False
    retired: bool = False
    retired_reason: str | None = None

    def utility(self) -> int:
        return self.helped - self.harmed


_BLOCK_RE = re.compile(r"^### (?P<id>\S+)\s*$", re.MULTILINE)


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("true", "1", "yes")


def _parse_iso(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw or raw == "-":
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _render_bullet_block(bullet: Bullet) -> str:
    """One bullet's markdown block. Field order is fixed so a diff against
    the previous commit shows only the field(s) that actually changed."""
    lines = [f"### {bullet.id}"]
    lines.append(f"- domain: {bullet.domain}")
    lines.append(f"- text: {bullet.text}")
    lines.append(f"- provenance: {', '.join(bullet.provenance)}")
    lines.append(f"- created_at: {bullet.created_at.isoformat()}")
    lines.append(
        f"- last_used_at: {bullet.last_used_at.isoformat() if bullet.last_used_at else '-'}"
    )
    lines.append(f"- helped: {bullet.helped}")
    lines.append(f"- harmed: {bullet.harmed}")
    lines.append(f"- pinned: {'true' if bullet.pinned else 'false'}")
    lines.append(f"- retired: {'true' if bullet.retired else 'false'}")
    lines.append(f"- retired_reason: {bullet.retired_reason or '-'}")
    return "\n".join(lines) + "\n"


def _parse_bullet_block(block_id: str, block_text: str, *, domain: str) -> Bullet:
    fields: dict[str, str] = {}
    for line in block_text.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        key, _, value = line[2:].partition(":")
        fields[key.strip()] = value.strip()
    provenance_raw = fields.get("provenance", "")
    provenance = tuple(p.strip() for p in provenance_raw.split(",") if p.strip())
    created_at = _parse_iso(fields.get("created_at", "")) or datetime.now(timezone.utc)
    return Bullet(
        id=block_id,
        domain=fields.get("domain", domain),
        text=fields.get("text", ""),
        provenance=provenance,
        created_at=created_at,
        last_used_at=_parse_iso(fields.get("last_used_at", "-")),
        helped=int(fields.get("helped", "0") or "0"),
        harmed=int(fields.get("harmed", "0") or "0"),
        pinned=_parse_bool(fields.get("pinned", "false")),
        retired=_parse_bool(fields.get("retired", "false")),
        retired_reason=_none_if_dash(fields.get("retired_reason", "-")),
    )


def _none_if_dash(raw: str) -> str | None:
    raw = raw.strip()
    return None if raw in ("", "-") else raw


def _parse_file(content: str, *, domain: str) -> list[Bullet]:
    bullets: list[Bullet] = []
    matches = list(_BLOCK_RE.finditer(content))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block = _parse_bullet_block(m.group("id"), content[start:end], domain=domain)
        bullets.append(block)
    return bullets


def _render_file(bullets: list[Bullet]) -> str:
    return "\n".join(_render_bullet_block(b) for b in bullets)


class GitPlaybookStore:
    """One git repository, one Markdown file per :data:`DOMAINS` entry.

    Every read degrades to empty (best-effort: "a playbook read failure
    degrades to no playbook, never an error on a path a human is waiting
    on" — the module's own acceptance line). Every write commits; a git
    failure (git not installed, or any subprocess error) is logged and
    swallowed — the in-memory bullet list the caller gets back is still
    correct, but a deployment without git loses the audit trail, which is
    reported once via a warning rather than raising into a nightly job or a
    CLI command.
    """

    def __init__(self, root_dir: str):
        self._root = root_dir
        self._lock = threading.RLock()
        self._initialized = False

    # --- repo/file plumbing -------------------------------------------------

    def _repo_exists(self) -> bool:
        return os.path.isdir(os.path.join(self._root, ".git"))

    def _ensure_repo(self) -> None:
        """Provision the git repo — called ONLY from write paths (add/
        refine/retire/pin/record_outcomes_batch/touch_last_used). Every
        read-only method (:meth:`load`, :meth:`current_commit`,
        :meth:`history`, :meth:`revert`) must check :meth:`_repo_exists`
        instead and degrade to empty/``None``/``False`` — a plain read (a
        draft's ``retrieve`` node, a CLI ``show``) must never provision a
        git repository as a side effect."""
        if self._initialized:
            return
        os.makedirs(self._root, exist_ok=True)
        if not self._repo_exists():
            self._run_git(["init", "-q"])
            # Repo-local identity (never global) so a commit never depends on
            # the host machine having git configured at all.
            self._run_git(["config", "user.email", "reflector@attune.local"])
            self._run_git(["config", "user.name", "Attune Playbook Reflector"])
        self._initialized = True

    def _run_git(self, args: list[str]) -> bool:
        try:
            subprocess.run(
                ["git", *args], cwd=self._root, capture_output=True, text=True,
                check=True,
            )
            return True
        except (OSError, subprocess.CalledProcessError) as exc:
            logger.warning("playbook git %s failed: %s", args, exc)
            return False

    def _git_output(self, args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args], cwd=self._root, capture_output=True, text=True,
                check=True,
            )
            return result.stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            logger.warning("playbook git %s failed: %s", args, exc)
            return None

    def _file_path(self, domain: str) -> str:
        return os.path.join(self._root, f"{domain}.md")

    def _rel_path(self, domain: str) -> str:
        return f"{domain}.md"

    def _commit(self, domain: str, message: str) -> None:
        rel = self._rel_path(domain)
        if not self._run_git(["add", rel]):
            return
        # An empty diff (e.g. a no-op write) makes `git commit` fail with a
        # non-zero exit; that is an expected, harmless case here, not an
        # error to surface — `_run_git` already logged it at warning level
        # if something else was actually wrong.
        self._run_git(["commit", "-q", "-m", message])

    # --- reads ---------------------------------------------------------

    def load(self, domain: str) -> list[Bullet]:
        """Every bullet in ``domain`` (active and retired) — best-effort:
        a missing file or read error is an empty playbook, never a raised
        exception (a human waiting on a draft must never see a playbook
        outage)."""
        path = self._file_path(domain)
        try:
            if not os.path.exists(path):
                return []
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            logger.warning("playbook read failed for domain=%s", domain, exc_info=True)
            return []
        return _parse_file(content, domain=domain)

    def load_active(self, domain: str) -> list[Bullet]:
        return [b for b in self.load(domain) if not b.retired]

    def find(self, bullet_id: str) -> tuple[str, Bullet] | None:
        """Search every domain file for ``bullet_id`` — the CLI's
        ``retire``/``pin`` commands take no domain argument (task 6), so
        this is the one lookup they and :meth:`record_outcome` share."""
        for domain in DOMAINS:
            for bullet in self.load(domain):
                if bullet.id == bullet_id:
                    return domain, bullet
        return None

    def all_provenance_ids(self) -> set[str]:
        """Every ledger proposal id already cited by any bullet (active or
        retired), across every domain — the reflector's dedup guard so the
        same three rejections never spawn a second bullet on a later run."""
        ids: set[str] = set()
        for domain in DOMAINS:
            for bullet in self.load(domain):
                ids.update(bullet.provenance)
        return ids

    def count_created_on(self, day: "datetime") -> int:
        """How many bullets across every domain already carry ``created_at``
        on ``day``'s calendar date — the reflector's durable per-day ratchet
        (task 4: "≤3 new bullets/day, hard" must hold even across multiple
        runs in the same day, not just within one call)."""
        target = day.date()
        count = 0
        for domain in DOMAINS:
            for bullet in self.load(domain):
                if bullet.created_at.date() == target:
                    count += 1
        return count

    def render_slice(
        self,
        domain: str,
        *,
        max_bullets: int = MAX_SLICE_BULLETS,
        max_chars: int = MAX_SLICE_CHARS,
    ) -> tuple[str, tuple[str, ...]]:
        """The bounded, cacheable prompt slice for ``domain`` (task 5):
        active bullets only, selected by utility (helped-harmed, pinned
        first) then recency, never dumped whole — ExpeL's documented
        failure mode. Returns ``(text, bullet_ids)``; the caller is
        responsible for :meth:`touch_last_used` on the ids it actually
        showed to a model."""
        active = self.load_active(domain)
        active.sort(
            key=lambda b: (
                b.pinned,
                b.utility(),
                b.last_used_at or b.created_at,
            ),
            reverse=True,
        )
        lines: list[str] = []
        ids: list[str] = []
        total = 0
        for bullet in active[:max_bullets]:
            line = f"- {bullet.text}"
            if total + len(line) + 1 > max_chars:
                break
            lines.append(line)
            ids.append(bullet.id)
            total += len(line) + 1
        return "\n".join(lines), tuple(ids)

    def show(self, domain: str | None = None) -> str:
        """Human-readable listing for ``attune playbook show`` — every
        active bullet's id, text, and helped/harmed tally; retired bullets
        are noted by count only (their text remains discoverable via
        ``attune playbook history``)."""
        domains = (domain,) if domain else DOMAINS
        lines: list[str] = []
        for d in domains:
            bullets = self.load(d)
            active = [b for b in bullets if not b.retired]
            retired = [b for b in bullets if b.retired]
            lines.append(f"# {d} ({len(active)} active, {len(retired)} retired)")
            for b in sorted(active, key=lambda x: x.utility(), reverse=True):
                marker = " [pinned]" if b.pinned else ""
                lines.append(
                    f"  [{b.id}]{marker} helped={b.helped} harmed={b.harmed} :: {b.text}"
                )
            if not active:
                lines.append("  (none)")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def history(self, bullet_id: str) -> list[str]:
        """``git log`` entries whose commit message mentions ``bullet_id`` —
        every commit this store makes names the bullet id in its message
        (see the mutation methods below), so a plain ``--grep`` finds the
        full history of one learned belief without needing to know which
        domain file it lives in."""
        found = self.find(bullet_id)
        if found is None:
            return []
        domain, _ = found
        if not self._repo_exists():
            return []
        output = self._git_output(
            [
                "log", "--grep", bullet_id, "--oneline", "--",
                self._rel_path(domain),
            ]
        )
        if not output:
            return []
        return [line for line in output.splitlines() if line.strip()]

    def revert(self, commit: str) -> bool:
        """``git revert --no-edit <commit>`` inside the playbook repo — the
        principal's escape hatch to undo exactly one learned mutation
        without touching anything else. Returns whether the revert
        succeeded (a dirty tree or an unknown commit both fail cleanly,
        logged, no partial state). A playbook with no repo yet has nothing
        to revert — returns ``False`` without provisioning one."""
        if not self._repo_exists():
            return False
        return self._run_git(["revert", "--no-edit", commit])

    # --- writes (delta edits only — see module docstring) ---------------

    def add_bullet(
        self,
        domain: str,
        text: str,
        *,
        provenance: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> Bullet | None:
        """A genuinely new bullet. Refuses (returns ``None``, logged) once
        ``domain`` already holds :data:`MAX_BULLETS_PER_FILE` bullets —
        bounded by construction, never silently evicting an existing one."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            self._ensure_repo()
            bullets = self.load(domain)
            if len(bullets) >= MAX_BULLETS_PER_FILE:
                logger.warning(
                    "playbook domain=%s at MAX_BULLETS_PER_FILE=%d — refusing new bullet",
                    domain, MAX_BULLETS_PER_FILE,
                )
                return None
            bullet = Bullet(
                id=_new_id(now),
                domain=domain,
                text=text[:MAX_CHARS_PER_BULLET],
                provenance=tuple(provenance),
                created_at=now,
            )
            bullets.append(bullet)
            self._write(domain, bullets)
            self._commit(domain, f"[{bullet.id}] add: {bullet.text[:72]}")
            return bullet

    def refine_bullet(
        self,
        bullet_id: str,
        new_text: str,
        *,
        add_provenance: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> bool:
        """Replace ONE bullet's text in place (delta edit — every other
        bullet in the file is untouched), optionally extending its
        provenance tuple with new ledger ids that justified the refinement."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            self._ensure_repo()
            found = self.find(bullet_id)
            if found is None:
                return False
            domain, _ = found
            bullets = self.load(domain)
            for b in bullets:
                if b.id == bullet_id:
                    b.text = new_text[:MAX_CHARS_PER_BULLET]
                    if add_provenance:
                        b.provenance = tuple(
                            dict.fromkeys((*b.provenance, *add_provenance))
                        )
                    break
            self._write(domain, bullets)
            self._commit(domain, f"[{bullet_id}] refine: {new_text[:72]}")
            return True

    def retire_bullet(
        self, bullet_id: str, *, reason: str, now: datetime | None = None,
    ) -> bool:
        """Mark ``bullet_id`` retired — kept in the file (and its git
        history) for audit, excluded from :meth:`load_active`/
        :meth:`render_slice` from this point on. Never deletes the block."""
        with self._lock:
            self._ensure_repo()
            found = self.find(bullet_id)
            if found is None:
                return False
            domain, _ = found
            bullets = self.load(domain)
            for b in bullets:
                if b.id == bullet_id:
                    b.retired = True
                    b.retired_reason = reason
                    break
            self._write(domain, bullets)
            self._commit(domain, f"[{bullet_id}] retire: {reason[:72]}")
            return True

    def pin_bullet(self, bullet_id: str) -> bool:
        """Exempt ``bullet_id`` from decay and harmed>helped retirement — the
        principal's explicit override, the same posture
        ``importance.py``'s pin already holds for a sender's tier."""
        return self._set_pinned(bullet_id, True)

    def unpin_bullet(self, bullet_id: str) -> bool:
        return self._set_pinned(bullet_id, False)

    def _set_pinned(self, bullet_id: str, pinned: bool) -> bool:
        with self._lock:
            self._ensure_repo()
            found = self.find(bullet_id)
            if found is None:
                return False
            domain, _ = found
            bullets = self.load(domain)
            for b in bullets:
                if b.id == bullet_id:
                    b.pinned = pinned
                    break
            self._write(domain, bullets)
            verb = "pin" if pinned else "unpin"
            self._commit(domain, f"[{bullet_id}] {verb}")
            return True

    def record_outcome(
        self, bullet_id: str, outcome: str, *, now: datetime | None = None,
    ) -> bool:
        """Increment ``helped``/``harmed`` for ``bullet_id`` — the nightly
        reflector's per-bullet accounting (task 4, step 1), the part ACE
        needs and the part interference-avoidance requires. Not committed
        as its own git commit per call (a busy bullet could otherwise
        acquire dozens of trivial commits a day); the reflector batches
        every accounting update for one run into a single commit per
        touched domain via :meth:`record_outcomes_batch`."""
        if outcome not in ("helped", "harmed"):
            raise ValueError(f"outcome must be 'helped' or 'harmed', got {outcome!r}")
        found = self.find(bullet_id)
        if found is None:
            return False
        domain, _ = found
        return self.record_outcomes_batch(domain, {bullet_id: (1 if outcome == "helped" else 0,
                                                                1 if outcome == "harmed" else 0)})

    def record_outcomes_batch(
        self, domain: str, deltas: dict[str, tuple[int, int]],
    ) -> bool:
        """Apply many (helped_delta, harmed_delta) pairs to ``domain`` in one
        commit — what the nightly reflector actually calls, so a run that
        accounts for 50 decided proposals against 8 bullets produces one
        commit per domain, not 50."""
        if not deltas:
            return True
        with self._lock:
            self._ensure_repo()
            bullets = self.load(domain)
            touched: list[str] = []
            for b in bullets:
                if b.id in deltas:
                    helped_delta, harmed_delta = deltas[b.id]
                    b.helped += helped_delta
                    b.harmed += harmed_delta
                    touched.append(b.id)
            if not touched:
                return False
            self._write(domain, bullets)
            self._commit(
                domain,
                f"accounting: {', '.join(sorted(touched))}",
            )
            return True

    def touch_last_used(
        self, bullet_ids: "tuple[str, ...] | list[str]", *, now: datetime | None = None,
    ) -> None:
        """Bump ``last_used_at`` for every id in ``bullet_ids`` — called by
        whoever actually showed a rendered slice to a model
        (``draft_approve.py``'s ``retrieve`` node). Not committed: usage
        timestamps are high-frequency and low-signal for the git history
        (every draft would otherwise produce a commit); they persist to the
        working tree so decay/selection still see them, and ride along on
        the next real mutation's commit."""
        if not bullet_ids:
            return
        now = now or datetime.now(timezone.utc)
        ids = set(bullet_ids)
        with self._lock:
            self._ensure_repo()
            for domain in DOMAINS:
                bullets = self.load(domain)
                changed = False
                for b in bullets:
                    if b.id in ids:
                        b.last_used_at = now
                        changed = True
                if changed:
                    self._write(domain, bullets)

    def current_commit(self) -> str | None:
        """The playbook repo's current HEAD short hash — stamped onto the
        decision ledger's ``playbook_commit`` field (build prompt 26) so a
        proposal's context is traceable to an exact playbook snapshot.
        ``None`` when no repo exists yet (nothing has ever been written) —
        never provisions one just to answer this."""
        if not self._repo_exists():
            return None
        output = self._git_output(["rev-parse", "--short", "HEAD"])
        return output.strip() if output else None

    def _write(self, domain: str, bullets: list[Bullet]) -> None:
        path = self._file_path(domain)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_render_file(bullets))

    # --- nightly-reflection bookkeeping (not git-tracked: a cursor is
    # operational state, not a learned belief) -----------------------------

    def _cursor_path(self) -> str:
        return os.path.join(self._root, ".reflection_cursor")

    def load_reflection_cursor(self) -> datetime | None:
        """The ``decided_at`` watermark of the last ledger row the nightly
        reflector already accounted for — read by ``runtime.py`` so a
        second run the same day never double-counts a decision already
        credited/blamed to a bullet. ``None`` before the first run."""
        try:
            with open(self._cursor_path(), encoding="utf-8") as fh:
                return _parse_iso(fh.read().strip())
        except OSError:
            return None

    def save_reflection_cursor(self, now: datetime) -> None:
        os.makedirs(self._root, exist_ok=True)
        with open(self._cursor_path(), "w", encoding="utf-8") as fh:
            fh.write(now.isoformat())
