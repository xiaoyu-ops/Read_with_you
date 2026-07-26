"""Protect formula and citation fragments during translation.

The placeholders are occurrence-specific and are never persisted.  They only
exist while the current block is sent to the model, then the model response is
validated and restored before ``translation.json`` is updated.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass


_PLACEHOLDER_START = "⟦PET_IMMUTABLE_"
_PLACEHOLDER_END = "⟧"

_FRAGMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("literal", re.compile(r"⟦PET_IMMUTABLE_[^⟧\n]+⟧")),
    (
        "math",
        re.compile(
            r"\\begin\{(equation\*?|align\*?|aligned|gather\*?|multline\*?)\}"
            r"[\s\S]*?\\end\{\1\}"
        ),
    ),
    ("math", re.compile(r"\$\$[\s\S]+?\$\$")),
    ("math", re.compile(r"\\\[[\s\S]+?\\\]")),
    ("math", re.compile(r"\\\([\s\S]+?\\\)")),
    (
        "citation",
        re.compile(
            r"\\(?:cite|citep|citet|citealp|citeauthor|ref|eqref|autoref)\*?"
            r"(?:\[[^\]\n]*\])*\{[^{}\n]+\}"
        ),
    ),
    (
        "citation",
        re.compile(r"\[(?:\d+(?:\s*[-–—,;]\s*\d+)*)\]"),
    ),
)


class ImmutablePlaceholderError(ValueError):
    """The model changed the immutable placeholder sequence."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ImmutableFragment:
    kind: str
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class ProtectedTranslationText:
    text: str
    fragments: tuple[ImmutableFragment, ...]
    placeholder_prefix: str

    @property
    def placeholders(self) -> tuple[str, ...]:
        return tuple(
            f"{self.placeholder_prefix}{index:04d}{_PLACEHOLDER_END}"
            for index in range(len(self.fragments))
        )


@dataclass(frozen=True)
class ImmutableTranslationAudit:
    """Strict immutable-fragment audit for new or previously saved translations."""

    safe: bool
    reason: str | None
    source_fragments: tuple[ImmutableFragment, ...]
    translation_fragments: tuple[ImmutableFragment, ...]


def extract_immutable_fragments(text: str) -> tuple[ImmutableFragment, ...]:
    """Return non-overlapping formula/citation fragments in source order."""
    candidates: list[ImmutableFragment] = []
    for kind, pattern in _FRAGMENT_PATTERNS:
        for match in pattern.finditer(text):
            candidates.append(
                ImmutableFragment(
                    kind=kind,
                    value=match.group(0),
                    start=match.start(),
                    end=match.end(),
                )
            )
    for start, end, value in _iter_inline_dollar_math(text):
        candidates.append(
            ImmutableFragment(
                kind="math",
                value=value,
                start=start,
                end=end,
            )
        )
    candidates.sort(key=lambda item: (item.start, -(item.end - item.start)))

    fragments: list[ImmutableFragment] = []
    occupied_until = -1
    for candidate in candidates:
        if candidate.start < occupied_until:
            continue
        fragments.append(candidate)
        occupied_until = candidate.end
    return tuple(fragments)


def audit_immutable_translation(
    source_text: str,
    translated_text: str,
) -> ImmutableTranslationAudit:
    """Audit a persisted translation without mutating or re-translating it.

    This is intentionally strict: every formula/citation from the source must
    occur exactly once, byte-for-byte and in source order, and the translation
    may not introduce another immutable fragment.
    """
    source_fragments = extract_immutable_fragments(source_text)
    translation_fragments = extract_immutable_fragments(translated_text)
    reason = _fragment_sequence_reason(source_fragments, translation_fragments)
    return ImmutableTranslationAudit(
        safe=reason is None,
        reason=reason,
        source_fragments=source_fragments,
        translation_fragments=translation_fragments,
    )


def protect_immutable_fragments(text: str) -> ProtectedTranslationText:
    """Replace immutable fragments with deterministic collision-safe tokens."""
    fragments = extract_immutable_fragments(text)
    salt = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10].upper()
    prefix = f"{_PLACEHOLDER_START}{salt}_"
    while prefix in text:
        salt = hashlib.sha256((salt + text).encode("utf-8")).hexdigest()[:10].upper()
        prefix = f"{_PLACEHOLDER_START}{salt}_"
    if not fragments:
        return ProtectedTranslationText(text=text, fragments=(), placeholder_prefix=prefix)

    parts: list[str] = []
    cursor = 0
    for index, fragment in enumerate(fragments):
        parts.append(text[cursor : fragment.start])
        parts.append(f"{prefix}{index:04d}{_PLACEHOLDER_END}")
        cursor = fragment.end
    parts.append(text[cursor:])
    return ProtectedTranslationText(
        text="".join(parts),
        fragments=fragments,
        placeholder_prefix=prefix,
    )


def restore_immutable_fragments(
    translated_text: str,
    protected: ProtectedTranslationText,
) -> str:
    """Validate the exact placeholder sequence and restore source fragments."""
    if not protected.fragments:
        if _PLACEHOLDER_START in translated_text:
            raise ImmutablePlaceholderError("immutable_placeholder_unknown")
        reason = _fragment_sequence_reason((), extract_immutable_fragments(translated_text))
        if reason is not None:
            raise ImmutablePlaceholderError(reason)
        return translated_text

    placeholder_pattern = re.compile(
        re.escape(protected.placeholder_prefix) + r"(\d{4})" + re.escape(_PLACEHOLDER_END)
    )
    matches = list(placeholder_pattern.finditer(translated_text))
    expected_indexes = list(range(len(protected.fragments)))
    actual_indexes = [int(match.group(1)) for match in matches]

    if _PLACEHOLDER_START in placeholder_pattern.sub("", translated_text):
        raise ImmutablePlaceholderError("immutable_placeholder_unknown")
    if len(actual_indexes) < len(expected_indexes):
        raise ImmutablePlaceholderError("immutable_placeholder_missing")
    if len(actual_indexes) > len(expected_indexes) or len(set(actual_indexes)) < len(actual_indexes):
        raise ImmutablePlaceholderError("immutable_placeholder_duplicate")
    if actual_indexes != expected_indexes:
        if sorted(actual_indexes) == expected_indexes:
            raise ImmutablePlaceholderError("immutable_placeholder_reordered")
        raise ImmutablePlaceholderError("immutable_placeholder_unknown")

    restored: list[str] = []
    cursor = 0
    for match, fragment in zip(matches, protected.fragments, strict=True):
        restored.append(translated_text[cursor : match.start()])
        restored.append(fragment.value)
        cursor = match.end()
    restored.append(translated_text[cursor:])
    restored_text = "".join(restored)
    reason = _fragment_sequence_reason(
        protected.fragments,
        extract_immutable_fragments(restored_text),
    )
    if reason is not None:
        raise ImmutablePlaceholderError(reason)
    return restored_text


def _is_inline_dollar_math(value: str) -> bool:
    """Reject common currency ranges that merely contain two dollar signs."""
    inner = value[1:-1].strip()
    if not inner:
        return False
    if re.match(r"\d", inner) and "\\" not in inner and re.search(r"[A-Za-z]{2,}", inner):
        return False
    return True


def _iter_inline_dollar_math(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield plausible single-dollar math without consuming rejected openers."""
    positions = [
        index
        for index, character in enumerate(text)
        if character == "$"
        and (index == 0 or text[index - 1] not in ("\\", "$"))
        and (index + 1 == len(text) or text[index + 1] != "$")
    ]
    cursor = 0
    while cursor + 1 < len(positions):
        start = positions[cursor]
        end = positions[cursor + 1] + 1
        value = text[start:end]
        if "\n" not in value and _is_inline_dollar_math(value):
            yield start, end, value
            cursor += 2
        else:
            cursor += 1


def _fragment_sequence_reason(
    source_fragments: tuple[ImmutableFragment, ...],
    translation_fragments: tuple[ImmutableFragment, ...],
) -> str | None:
    source_values = tuple((fragment.kind, fragment.value) for fragment in source_fragments)
    translation_values = tuple(
        (fragment.kind, fragment.value) for fragment in translation_fragments
    )
    if source_values == translation_values:
        return None
    if len(translation_values) < len(source_values):
        return "immutable_missing"
    if len(translation_values) > len(source_values):
        source_counts = Counter(source_values)
        translation_counts = Counter(translation_values)
        if any(
            count > source_counts.get(value, 0) and source_counts.get(value, 0) > 0
            for value, count in translation_counts.items()
        ):
            return "immutable_duplicate"
        return "immutable_changed"
    if Counter(source_values) == Counter(translation_values):
        return "immutable_reordered"
    return "immutable_changed"
