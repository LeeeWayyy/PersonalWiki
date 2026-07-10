"""Minimal FSRS-4.5 scheduler (Anki-grade spaced repetition).

Implements the published FSRS-4.5 formulas with the default weights. Grades:
1=again, 2=hard, 3=good, 4=easy. State: 0=new, 1=review. Weights can later be
optimized from your own review history; the defaults are a sound starting point.
"""
from __future__ import annotations
import math
from dataclasses import dataclass

# Default FSRS-4.5 weights (17).
W = [0.4, 0.6, 2.4, 5.8, 4.93, 0.94, 0.86, 0.01, 1.49,
     0.14, 0.94, 2.18, 0.05, 0.34, 1.26, 0.29, 2.61]

DECAY = -0.5
FACTOR = 0.9 ** (1 / DECAY) - 1  # ≈ 0.2345679
REQUEST_RETENTION = 0.9
MAX_INTERVAL = 3650  # days


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _init_difficulty(g: int) -> float:
    return _clamp(W[4] - (g - 3) * W[5], 1.0, 10.0)


def _init_stability(g: int) -> float:
    return max(W[g - 1], 0.1)


def _retrievability(elapsed_days: float, stability: float) -> float:
    return (1 + FACTOR * elapsed_days / stability) ** DECAY


def _interval(stability: float, retention: float = REQUEST_RETENTION) -> int:
    ivl = stability / FACTOR * (retention ** (1 / DECAY) - 1)
    return int(_clamp(round(ivl), 1, MAX_INTERVAL))


def _next_stability_recall(d: float, s: float, r: float, g: int) -> float:
    hard = W[15] if g == 2 else 1.0
    easy = W[16] if g == 4 else 1.0
    return s * (1 + math.exp(W[8]) * (11 - d) * (s ** -W[9]) *
                (math.exp((1 - r) * W[10]) - 1) * hard * easy)


def _next_stability_forget(d: float, s: float, r: float) -> float:
    return W[11] * (d ** -W[12]) * (((s + 1) ** W[13]) - 1) * math.exp((1 - r) * W[14])


@dataclass
class Card:
    stability: float = 0.0
    difficulty: float = 0.0
    state: int = 0          # 0=new, 1=review
    reps: int = 0
    lapses: int = 0


def schedule(card: Card, grade: int, elapsed_days: float) -> tuple[Card, int]:
    """Return (updated_card, interval_days)."""
    grade = int(_clamp(grade, 1, 4))
    if card.state == 0 or card.stability <= 0:  # first review
        s = _init_stability(grade)
        d = _init_difficulty(grade)
        card = Card(stability=s, difficulty=d, state=1,
                    reps=1, lapses=0)
        return card, _interval(s)

    r = _retrievability(max(elapsed_days, 0), card.stability)
    # difficulty update + mean reversion toward D0(easy)
    d = card.difficulty - W[6] * (grade - 3)
    d = W[7] * _init_difficulty(4) + (1 - W[7]) * d
    d = _clamp(d, 1.0, 10.0)

    if grade == 1:
        s = min(_next_stability_forget(d, card.stability, r), card.stability)
        lapses = card.lapses + 1
    else:
        s = _next_stability_recall(d, card.stability, r, grade)
        lapses = card.lapses
    s = max(s, 0.1)

    return Card(stability=s, difficulty=d, state=1,
                reps=card.reps + 1, lapses=lapses), _interval(s)
