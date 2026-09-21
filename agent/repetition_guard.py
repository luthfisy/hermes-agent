"""Cheap content-sanity checks for the truncated-response continuation path.

A model in a degenerate repetition loop can spend its ENTIRE output budget echoing one fragment;
the ``finish_reason=length`` continuation would then stitch it into the final response with a
"continue" nudge (one incident: a 60k-char turn delivered as 31 Discord messages). This detects
repetition-dominated fragments BEFORE the nudge so the turn aborts with a clear error. Deliberately
conservative: only LONG verbatim repeats (60+ chars) covering a majority of the fragment trip it.
"""

from __future__ import annotations

import logging
import math
from collections import Counter

logger = logging.getLogger(__name__)

# Below this length the check doesn't run: short truncations trivially
# contain repeated tokens and are legitimately continued.
MIN_FRAGMENT_LENGTH = 400
# Exact-repeat window; far beyond ordinary phrasing reuse (citations, headings, similar code).
_REPEAT_WINDOW = 60
# A window repeating at least this often is a signal even for short fragments.
_MIN_REPEAT_COUNT = 5
# "Repetition-dominated" = repeated windows cover at least this fraction.
_DOMINANCE_RATIO = 0.5

# What an interrupt checkpoint says INSTEAD of a repetition-dominated partial. Replaying the
# looped bytes (as the redirect's api_content or as the interrupted assistant row) re-seeds the
# loop on the next request and the corruption survives restarts (#112764); the model only needs
# to know the reply degenerated and was cut off.
REPETITION_LOOP_INTERRUPTED = "[the reply degenerated into a repetition loop and was interrupted]"

# ``is_runaway_repetition``: a multi-line partial must be mostly copies of a few lines. Batch-style
# output (distinct INSERT rows, similar table rows) shares long prefixes and trips the window
# scan, but every line is distinct; a loop re-emits the same line(s).
_RUNAWAY_DISTINCT_LINE_RATIO = 0.5


def is_repetition_dominated(text: str) -> bool:
    """True when a single 60+ char substring recurs often enough to cover at least half
    of ``text`` — the signature of a repetition loop. Fail-open for non-string/short input.

    That shape is the signature of a model repetition loop (issue #86581), and continuing such a fragment is
    pointless — the continuation nudge would just stitch more repeated text into the final response.
    """
    if not isinstance(text, str):
        return False
    n = len(text)
    if n < MIN_FRAGMENT_LENGTH:
        return False

    # Fast path: one normalized line duplicated enough to cover half the fragment (the common echo shape).
    if _line_repetition_dominated(text, n):
        return True

    # General path: fixed-size windows sliding one char at a time, catching loops that
    # don't align to line boundaries. A window must appear ``needed`` times to cover
    # >= _DOMINANCE_RATIO (and >= _MIN_REPEAT_COUNT).
    window = _REPEAT_WINDOW
    needed = max(_MIN_REPEAT_COUNT, math.ceil(n * _DOMINANCE_RATIO / window))
    counts: dict[str, int] = {}
    for i in range(n - window + 1):
        key = text[i : i + window]
        c = counts.get(key, 0) + 1
        if c >= needed:
            return True
        counts[key] = c
    return False


def is_runaway_repetition(text: str) -> bool:
    """Stricter than :func:`is_repetition_dominated`: also require the runaway shape.

    An interrupt checkpoint DROPS the partial when this fires, so a legitimately repetitive but
    correct reply (distinct batch rows) must not qualify: repeated windows have to dominate AND,
    when the text has line structure, at most half of its non-empty lines may be distinct.
    """
    if not is_repetition_dominated(text):
        return False
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < _MIN_REPEAT_COUNT:
        return True  # no line structure to judge by: a dominated single-line loop
    return len(set(lines)) <= len(lines) * _RUNAWAY_DISTINCT_LINE_RATIO


def _line_repetition_dominated(text: str, n: int) -> bool:
    """True when a single normalized line covers half the fragment via repeats."""
    counts = Counter(norm for norm in (line.strip() for line in text.splitlines()) if norm)
    return any(c >= _MIN_REPEAT_COUNT and c * len(line) >= n * _DOMINANCE_RATIO for line, c in counts.items())


# ---- thinking-channel loop guard ----------------------------------------------------------
# The checks above watch the VISIBLE reply on truncation/interrupt paths. A thinking channel
# can degenerate on its own while the visible reply stays fine: one char (usually a quoting
# bracket) grows run over run — 「「「「「「実行」」」」」」「「「「「「「「「「やる」」... — with no exact
# long-window repeat, so ``is_repetition_dominated`` misses it (17 of 21 messages in one real
# incident corpus). The looped bytes must be cut BEFORE storage: a DeepSeek-style
# ``reasoning_content`` echo replays them into the next request and re-seeds the loop
# (#112764 family). Thresholds calibrated against a real-world corpus of ~175k
# reasoning messages: 「」『』 runs >= 12 and runs of >= 160 identical non-formatting chars
# never fired on any message without a degenerate segment.
THINKING_LOOP_TRUNCATED = "[thinking truncated: repetition loop detected]"

_BRACKET_RUN_CHARS = frozenset("「」『』")
_BRACKET_RUN_MIN = 12
_RUN_MIN = 160
_RUN_EXCLUDED = frozenset("-=*_|+#~` \t\r\n")


class ReasoningLoopGuard:
    """Incremental degeneration detector for a streamed reasoning channel.

    Feed each reasoning delta in order (stop once ``tripped`` is True). ``trip_index`` is
    the offset — in the concatenation of everything fed — where the degenerate run starts;
    callers cut accumulators there so display, storage and reasoning echo all stop replaying
    the loop. O(chars), no rescans.
    """

    __slots__ = ("tripped", "trip_index", "_seen", "_run_char", "_run_len", "_run_start")

    def __init__(self) -> None:
        self.tripped = False
        self.trip_index = -1
        self._seen = 0
        self._run_char = ""
        self._run_len = 0
        self._run_start = 0

    def feed(self, text: str) -> bool:
        if self.tripped or not isinstance(text, str) or not text:
            return self.tripped
        run_char, run_len, run_start = self._run_char, self._run_len, self._run_start
        i = self._seen
        for ch in text:
            if ch == run_char:
                run_len += 1
            else:
                run_char, run_len, run_start = ch, 1, i
            i += 1
            if ch in _BRACKET_RUN_CHARS and run_len >= _BRACKET_RUN_MIN:
                self._trip(run_start, ch, run_len)
                return True
            if run_len >= _RUN_MIN and ch not in _RUN_EXCLUDED and not ch.isspace():
                self._trip(run_start, ch, run_len)
                return True
        self._seen = i
        self._run_char, self._run_len, self._run_start = run_char, run_len, run_start
        return self.tripped

    def _trip(self, at: int, ch: str, length: int) -> None:
        self.tripped = True
        self.trip_index = at
        logger.debug("reasoning loop guard tripped: %r x%d at offset %d", ch, length, at)


def sanitize_degenerate_reasoning(text, *, marker: str = THINKING_LOOP_TRUNCATED):
    """Full-text pass for non-streaming intakes / storage boundaries.

    Returns ``text`` unchanged (same object) unless a degenerate shape is found; then the
    degenerate tail is dropped and ``marker`` appended. Fail-open for non-strings.
    """
    if not isinstance(text, str) or not text:
        return text
    guard = ReasoningLoopGuard()
    if not guard.feed(text):
        return text
    prefix = text[: max(0, guard.trip_index)].rstrip()
    return f"{prefix}\n\n{marker}" if prefix else marker
