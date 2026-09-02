"""Token estimation, and the seam for replacing it with measurement.

Why an estimator at all
-----------------------
Two different questions get called "how many tokens is this":

1. *How many will this cost?* — answered after the fact by the API's ``usage``
   field, which is authoritative and is what the quota ledger bills from
   (Part A §16.4).
2. *Will this fit?* — answered before the call, by this module, because the
   context manager has to decide what to include while assembling the request.

Only the second needs an estimate, and it only needs to be good enough to keep
the assembled prompt under a budget that already has headroom designed into it.

Why not tiktoken
----------------
tiktoken ships OpenAI's encodings. The model here is Qwen3.8-27B behind LiteLLM,
which uses a different tokenizer, so tiktoken would be a precise answer to the
wrong question — and it would look authoritative while being wrong. Loading the
real Qwen tokenizer means a `transformers` dependency and a model download on a
laptop that is meant to work offline behind a corporate proxy (Part B §4.2).

So: a documented heuristic that is deliberately conservative, plus
``Calibration``, which folds observed ``usage`` back into the ratio. After a
handful of real turns the estimate is measured rather than guessed, and until
then it errs towards overestimating — which costs a little headroom rather than
an over-length request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["estimate_tokens", "Calibration", "CHARS_PER_TOKEN_PROSE", "CHARS_PER_TOKEN_CODE"]

# Characters per token, by content shape.
#
# Prose sits near 4.0 for English. Code sits lower because punctuation,
# indentation and identifiers split more aggressively — 3.2 is a common
# empirical figure for source and is the safer of the two to be wrong about,
# since underestimating is what produces an over-length request.
CHARS_PER_TOKEN_PROSE = 4.0
CHARS_PER_TOKEN_CODE = 3.2

# A block is treated as code when this much of it is punctuation, brackets or
# indentation. Tuned to classify Go source and JSON as code, and violation
# messages and prose as prose.
_CODE_SIGNAL_RATIO = 0.16

_CODE_SIGNALS = re.compile(r"[{}()\[\];=<>|&/\\@#$%^*+~`_\t]")
_WHITESPACE_RUN = re.compile(r"[ \t]{2,}")


def _looks_like_code(text: str) -> bool:
    """Classify a block as code or prose.

    Cheap on purpose: this runs on every tool result, and a wrong answer costs
    at most the difference between 3.2 and 4.0 characters per token.
    """
    if not text:
        return False
    signals = len(_CODE_SIGNALS.findall(text))
    indented = len(_WHITESPACE_RUN.findall(text))
    return (signals + indented) / len(text) >= _CODE_SIGNAL_RATIO


def estimate_tokens(text: str, *, ratio: float | None = None) -> int:
    """Estimate the token count of a string.

    Never returns 0 for non-empty input: a message that costs nothing is a
    message a budget will happily admit an unbounded number of.
    """
    if not text:
        return 0
    divisor = ratio if ratio is not None else (
        CHARS_PER_TOKEN_CODE if _looks_like_code(text) else CHARS_PER_TOKEN_PROSE
    )
    return max(1, int(len(text) / divisor))


@dataclass
class Calibration:
    """Folds observed usage back into the estimate.

    The agent sends ``stream_options: {"include_usage": true}`` on every call
    (Part A §4.7), so every turn returns a real ``prompt_tokens`` for a prompt
    this module just estimated. That is a free measurement of exactly the
    quantity being guessed, and ignoring it would be the same mistake the
    frontend agent made in reserving a flat 4,096 tokens and never reconciling.

    Correction is bounded and gradual: a single anomalous turn should nudge the
    ratio, not redefine it, and the bounds stop a malformed usage payload from
    driving the estimate somewhere absurd.
    """

    ratio: float = CHARS_PER_TOKEN_PROSE
    samples: int = 0
    #: Correction applied per observation. Low enough that one outlier does not
    #: move the ratio far, high enough to converge within a session.
    smoothing: float = 0.2
    #: Hard bounds. No real tokenizer sits outside this range for text, and a
    #: ratio outside it means the input was wrong, not the model.
    min_ratio: float = 2.0
    max_ratio: float = 6.0

    def observe(self, *, estimated_chars: int, actual_tokens: int) -> None:
        """Record one real measurement.

        ``estimated_chars`` is the character count of the prompt that produced
        ``actual_tokens``, so the observed ratio falls straight out.
        """
        if estimated_chars <= 0 or actual_tokens <= 0:
            return
        observed = estimated_chars / actual_tokens
        observed = max(self.min_ratio, min(self.max_ratio, observed))
        # No history list. There was one, nothing ever read it, and it grew by
        # one float per turn for the life of the process — a slow leak whose only
        # purpose was to be a slow leak (BUG SH-7). `samples` is the count, and
        # the EMA is the state; a distribution nobody plots is not worth holding.
        self.samples += 1
        self.ratio += (observed - self.ratio) * self.smoothing
        self.ratio = max(self.min_ratio, min(self.max_ratio, self.ratio))

    def estimate(self, text: str) -> int:
        """Estimate using the calibrated ratio.

        Falls back to the shape heuristic until there is something to calibrate
        against — an uncalibrated "measurement" is just a guess wearing a
        different name.
        """
        if self.samples == 0:
            return estimate_tokens(text)
        return estimate_tokens(text, ratio=self.ratio)

    @property
    def calibrated(self) -> bool:
        """Whether the ratio reflects observation rather than the default."""
        return self.samples > 0
