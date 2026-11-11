"""Server-held conversations.

The client never sends the conversation back: it sends a session ID and its new question.
Each session keeps the full model conversation (including tool_use and tool_result blocks),
every tool call so far with its turn (for grounding checks and Sources) and the user's questions.

In-memory and single-process (the Procfile runs one worker). Sessions expire after a period
without activity, and the total number is capped; when full, the least recently used idle
session is dropped.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Session:
    id: str
    last_used: float
    messages: list[dict] = field(default_factory=list)       # model conversation, as sent to the API
    # Every tool call in the session: {tool, input, is_error, output, turn} (turn is 1-based).
    tool_calls: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)       # the user's questions, in order
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def tool_outputs(self) -> list[dict]:
        """Every tool output in the session (grounding checks draw on all of them)."""
        return [c["output"] for c in self.tool_calls]

    @property
    def total_question_chars(self) -> int:
        return sum(len(q) for q in self.questions)


class SessionsFull(RuntimeError):
    """Every session slot is held by a conversation that is answering right now."""


class SessionStore:
    def __init__(self, ttl_seconds: float = 3600, max_sessions: int = 200,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl_seconds
        self.max_sessions = max_sessions
        self.clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._sessions)

    def _expire(self) -> None:
        now = self.clock()
        for sid in [s.id for s in self._sessions.values() if now - s.last_used > self.ttl]:
            if not self._sessions[sid].lock.locked():
                del self._sessions[sid]

    def get(self, session_id: Any) -> Session | None:
        """The live session with this ID, or None if it's unknown or expired."""
        if not isinstance(session_id, str):
            return None
        with self._lock:
            self._expire()
            s = self._sessions.get(session_id)
            if s:
                s.last_used = self.clock()
            return s

    def create(self) -> Session:
        with self._lock:
            self._expire()
            while len(self._sessions) >= self.max_sessions:
                idle = [s for s in self._sessions.values() if not s.lock.locked()]
                if not idle:
                    raise SessionsFull("all conversation slots are busy")
                del self._sessions[min(idle, key=lambda s: s.last_used).id]
            s = Session(id=secrets.token_urlsafe(24), last_used=self.clock())
            self._sessions[s.id] = s
            return s

    def discard(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def touch(self, session: Session) -> None:
        with self._lock:
            session.last_used = self.clock()
