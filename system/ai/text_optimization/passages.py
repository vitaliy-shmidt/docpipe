"""Phase B building blocks: split normalized text into structural blocks,
score them against task keyword groups, and select a verbatim subset that
fits a character budget.

A block is an offset range into the normalized text, so every selected
passage is an exact substring of it - nothing is rewritten. Selection is
greedy and bounded: one pass to score (blocks x patterns), one ordered pass
to pick, a couple of re-renders at most to honour marker overhead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from system.ai.text_optimization.normalize import is_table_like

# Blocks are paragraphs; a paragraph longer than _MAX_BLOCK_CHARS (OCR text
# often has no blank lines at all) is split at line boundaries around
# _SPLIT_TARGET_CHARS, and a single over-long line at sentence boundaries.
# Kept comfortably below the smallest per-call budget (see planner.py
# MIN_CALL_BUDGET_CHARS) so any block can always be selected on its own.
_MAX_BLOCK_CHARS = 1400
_SPLIT_TARGET_CHARS = 900
# A single short line directly followed by more text is treated as a
# heading and kept together with the block it introduces.
_HEADING_MAX_CHARS = 80
_SENTENCE_END_RE = re.compile(r"(?<=[.;:!?])\s+")
_HEADING_START_RE = re.compile(
    r"^(?:§\s*\d|art(?:ikel|icle)?\.?\s*\d|\d{1,2}(?:\.\d{1,2})*\.?\s+\S|[IVX]{1,5}\.\s)", re.I
)


# A neighbor block up to this size (heading, lead-in or follow-up sentence)
# is taken together with its hit block right away; larger neighbors are
# separate paragraphs and only added as context once all significant hits
# are in (if they matter on their own, they are hits themselves).
_SMALL_NEIGHBOR_CHARS = 500
# Score weights: one strong pattern always outranks any number of weak ones
# a realistic block can match, and "significant" == at least one strong.
STRONG_WEIGHT = 10
WEAK_WEIGHT = 1


@dataclass(frozen=True)
class KeywordGroup:
    """A task-specific group of response fields plus the patterns that mark
    text likely to contain them. `strong` patterns make a block significant
    (it must reach the model, or chunking kicks in); `weak` ones only rank.
    `carries_head` marks the group whose chunk always includes the document
    start/end (identity/master data)."""

    name: str
    fields: tuple[str, ...]
    strong: tuple[re.Pattern, ...]
    weak: tuple[re.Pattern, ...] = ()
    carries_head: bool = False


@dataclass(frozen=True)
class Selection:
    text: str
    passage_count: int
    hit_blocks: int
    included_hits: int

    @property
    def complete(self) -> bool:
        return self.included_hits == self.hit_blocks


def segment_blocks(text: str) -> list[tuple[int, int]]:
    blocks: list[tuple[int, int]] = []
    position = 0
    for paragraph in text.split("\n\n"):
        start, end = position, position + len(paragraph)
        position = end + 2
        if paragraph.strip():
            blocks.extend(_split_long(text, start, end))
    return _attach_headings(text, blocks)


def _split_long(text: str, start: int, end: int) -> list[tuple[int, int]]:
    if end - start <= _MAX_BLOCK_CHARS:
        return [(start, end)]
    pieces: list[tuple[int, int]] = []
    piece_start = start
    line_start = start
    while line_start < end:
        newline = text.find("\n", line_start, end)
        line_end = end if newline == -1 else newline
        if line_end - line_start > _MAX_BLOCK_CHARS:
            if line_start > piece_start:
                pieces.append((piece_start, line_start - 1))
            pieces.extend(_split_line(text, line_start, line_end))
            piece_start = line_end + 1
        elif line_end - piece_start > _SPLIT_TARGET_CHARS and line_start > piece_start:
            # Prefer not to cut between two table rows - allow the piece to
            # grow up to the hard maximum while the table continues.
            line = text[line_start:line_end]
            newline_before = text.rfind("\n", piece_start, line_start - 1)
            previous = text[piece_start if newline_before == -1 else newline_before + 1 : line_start - 1]
            in_table = is_table_like(line) and is_table_like(previous)
            if not in_table or line_end - piece_start > _MAX_BLOCK_CHARS:
                pieces.append((piece_start, line_start - 1))
                piece_start = line_start
        line_start = line_end + 1
    if piece_start < end:
        pieces.append((piece_start, end))
    return pieces


def _split_line(text: str, start: int, end: int) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    piece_start = start
    last_cut: tuple[int, int] | None = None
    for match in _SENTENCE_END_RE.finditer(text, start, end):
        if (
            match.start() - piece_start > _SPLIT_TARGET_CHARS
            and last_cut is not None
            and last_cut[0] > piece_start
        ):
            pieces.append((piece_start, last_cut[0]))
            piece_start = last_cut[1]
        last_cut = (match.start(), match.end())
    pieces.append((piece_start, end))

    # A piece still over the maximum (a line without sentence ends - wall-
    # of-text OCR output) is cut at whitespace as a last resort.
    result: list[tuple[int, int]] = []
    for piece_start, piece_end in pieces:
        while piece_end - piece_start > _MAX_BLOCK_CHARS:
            cut = text.rfind(" ", piece_start + 1, piece_start + _MAX_BLOCK_CHARS)
            next_start = cut + 1
            if cut == -1:
                cut = next_start = piece_start + _MAX_BLOCK_CHARS
            result.append((piece_start, cut))
            piece_start = next_start
        if piece_end > piece_start:
            result.append((piece_start, piece_end))
    return result


def _attach_headings(text: str, blocks: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    heading: tuple[int, int] | None = None
    for start, end in blocks:
        if heading is not None:
            if end - heading[0] <= _MAX_BLOCK_CHARS:
                result.append((heading[0], end))
                heading = None
                continue
            result.append(heading)
            heading = None
        chunk = text[start:end]
        if "\n" not in chunk and len(chunk) <= _HEADING_MAX_CHARS and _HEADING_START_RE.match(chunk):
            heading = (start, end)
            continue
        result.append((start, end))
    if heading is not None:
        result.append(heading)
    return result


def score_blocks(
    text: str, blocks: list[tuple[int, int]], groups: tuple[KeywordGroup, ...]
) -> list[dict[str, int]]:
    """Per block: group name -> weighted number of distinct matching patterns
    (distinct, not total occurrences, so one long boilerplate paragraph
    repeating a word cannot outrank a short, dense clause)."""
    scores = []
    for start, end in blocks:
        chunk = text[start:end]
        scores.append(
            {
                group.name: STRONG_WEIGHT * sum(1 for p in group.strong if p.search(chunk))
                + WEAK_WEIGHT * sum(1 for p in group.weak if p.search(chunk))
                for group in groups
            }
        )
    return scores


def select(
    text: str,
    blocks: list[tuple[int, int]],
    scores: list[dict[str, int]],
    group_names: tuple[str, ...],
    budget: int,
    *,
    head_chars: int,
    tail_chars: int,
) -> Selection:
    """Greedy, budget-bounded selection of verbatim blocks, in priority order:

    1. document head (master data: parties, number, subject);
    2. significant hits (at least one strong pattern), best first,
       interleaved across groups so no group starves the others - each with
       its small neighbors (heading, lead-in/follow-up sentence);
    3. document tail (signatures, annexes, late special terms);
    4. weak-only hits, same way;
    5. larger neighbors of significant hits, as extra context;
    6. only if there was no hit at all: evenly spaced blocks, a conservative
       sample instead of trusting keywords 100%.

    `hit_blocks`/`included_hits` count significant hits only - that is what
    decides whether the selection is complete.
    """
    if not blocks:
        return Selection(text="", passage_count=0, hit_blocks=0, included_hits=0)

    chosen: set[int] = set()
    order: list[int] = []
    used = 0

    def size(index: int) -> int:
        start, end = blocks[index]
        return end - start + 2

    def try_add(indices: list[int]) -> bool:
        nonlocal used
        new = [i for i in indices if i not in chosen]
        cost = sum(size(i) for i in new)
        if used + cost > budget:
            return False
        for i in new:
            chosen.add(i)
            order.append(i)
        used += cost
        return True

    def neighbors(core: int, *, small: bool) -> list[int]:
        return [
            i
            for i in (core - 1, core + 1)
            if 0 <= i < len(blocks) and (size(i) <= _SMALL_NEIGHBOR_CHARS) == small
        ]

    def add_hits(cores: list[int]) -> None:
        for core in cores:
            if not try_add([core] + neighbors(core, small=True)):
                try_add([core])

    head_total = 0
    head_count = 0
    for index in range(len(blocks)):
        if head_total >= head_chars or not try_add([index]):
            break
        head_total += size(index)
        head_count += 1

    hits = _interleaved_hits(scores, group_names)
    significant = [i for i in hits if max(scores[i].get(name, 0) for name in group_names) >= STRONG_WEIGHT]
    significant_set = set(significant)
    add_hits(significant)

    tail_total = 0
    for index in range(len(blocks) - 1, -1, -1):
        if tail_total >= tail_chars:
            break
        if index not in chosen and not try_add([index]):
            break
        tail_total += size(index)

    add_hits([i for i in hits if i not in significant_set])
    for core in significant:
        if core in chosen:
            for neighbor in neighbors(core, small=False):
                try_add([neighbor])

    if not hits:
        _add_even_sample(len(blocks), try_add)

    rendered = render(text, blocks, chosen)
    # Excerpt markers are added after selection - if they push the result
    # over budget, give back the most recently added (lowest-priority)
    # blocks, never the document head.
    while len(rendered) > budget and len(order) > head_count:
        chosen.discard(order.pop())
        rendered = render(text, blocks, chosen)

    return Selection(
        text=rendered,
        passage_count=_run_count(chosen),
        hit_blocks=len(significant),
        included_hits=sum(1 for core in significant if core in chosen),
    )


def _interleaved_hits(scores: list[dict[str, int]], group_names: tuple[str, ...]) -> list[int]:
    ranked = []
    for name in group_names:
        hits = [i for i, score in enumerate(scores) if score.get(name, 0) > 0]
        hits.sort(key=lambda i: (-scores[i][name], i))
        ranked.append(hits)
    result: list[int] = []
    seen: set[int] = set()
    for rank in range(max((len(hits) for hits in ranked), default=0)):
        for hits in ranked:
            if rank < len(hits) and hits[rank] not in seen:
                seen.add(hits[rank])
                result.append(hits[rank])
    return result


def _add_even_sample(block_count: int, try_add) -> None:
    # 1/2, then 1/4 and 3/4, then eighths, ... until the budget is full.
    denominator = 2
    while denominator <= block_count * 2:
        added_any = False
        for numerator in range(1, denominator, 2):
            if try_add([block_count * numerator // denominator]):
                added_any = True
        if not added_any:
            return
        denominator *= 2


def _run_count(chosen: set[int]) -> int:
    return sum(1 for i in chosen if i - 1 not in chosen)


def render(text: str, blocks: list[tuple[int, int]], chosen: set[int]) -> str:
    """Joins the chosen blocks in document order. Contiguous blocks are
    re-sliced from the original text as one excerpt (keeping their exact
    separators). Several excerpts are separated by neutral, clearly
    technical markers - never labels that could pass for document text."""
    if not chosen:
        return ""
    indices = sorted(chosen)
    if len(indices) == len(blocks):
        return text[blocks[0][0] : blocks[-1][1]]
    runs: list[tuple[int, int]] = []
    run_start = previous = indices[0]
    for index in indices[1:]:
        if index != previous + 1:
            runs.append((run_start, previous))
            run_start = index
        previous = index
    runs.append((run_start, previous))

    total = len(runs)
    parts = []
    for number, (first, last) in enumerate(runs, start=1):
        label = f"[Excerpt {number}/{total}"
        if first == 0:
            label += " - start of document"
        elif last == len(blocks) - 1:
            label += " - end of document"
        parts.append(label + "]\n" + text[blocks[first][0] : blocks[last][1]])
    return "\n\n".join(parts)
