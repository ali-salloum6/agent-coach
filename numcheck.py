"""Check that a number was actually stated in some text.

Memory writes must not invent numbers: a logged weight, a meal's calories
or a lift's reps has to appear in the message or reply it came from. This
module answers "does this value appear in this text", allowing the usual
ways of writing it (81.25, 2,430, 60.0).
"""

from __future__ import annotations

import re


def number_forms(value: float) -> list[str]:
    """Ways the value may be written: 60, 60.0, 2,430, 76.05."""
    value = float(value)
    if value.is_integer():
        return sorted({str(int(value)), f"{int(value):,}", f"{int(value)}.0"})
    return sorted({f"{value:g}", f"{value:.2f}".rstrip("0")})


def appears(value: float, text: str) -> bool:
    """True when the value is written in the text as a whole number token.

    Zero also counts when the text says "zero", "negligible", "trace" or
    "no protein" and the like.
    """
    if value is None:
        return True
    if float(value) == 0 and re.search(
        r"\b(?:zero|negligible|trace|none|no (?:protein|calories|kcal|carbs|fat))\b", text, re.I
    ):
        return True
    return any(
        re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d]|[.,]\d)", text)
        for form in number_forms(value)
    )


def missing(values: list[float | None], text: str) -> list[float]:
    """The values that do not appear in the text."""
    return [v for v in values if v is not None and not appears(v, text)]
