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
from collections import defaultdict
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
    # Block characters per tier (without excerpt markers / separators of
    # the rendering) - Planner V2 diagnostics.
    mandatory_chars: int = 0
    supporting_chars: int = 0
    # Planner V2.1: significant blocks found vs. blocks in the mandatory tier.
    candidate_count: int = 0
    mandatory_block_count: int = 0

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


class BlockScore(dict):
    """group name -> weighted score (a plain dict for every existing use),
    plus `strong`: group name -> indices of the strong patterns that matched
    and `value_keys`: the block's concrete business values (see _VALUE_RE) -
    what Planner V2.1's coverage bounding needs."""

    __slots__ = ("strong", "heading_strong", "value_keys")


# Concrete values that make two clauses of the same kind genuinely
# different (a possible contradiction): amounts, percentages, durations,
# dates, reference numbers. Only compared, never interpreted.
_VALUE_RE = re.compile(
    r"\d[\d.,']*\s?(?:€|eur\b|euro\b|chf\b|usd\b|\$|%)"
    r"|(?:€|eur\b|chf\b|usd\b|\$)\s?\d[\d.,']*"
    r"|\b\d+\s*(?:tag|woche|monat|jahr|day|week|month|year)"
    r"|\b\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})\b|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?=[A-Z0-9/-]*\d)[A-Z][A-Z0-9/-]{3,}\b",
    re.IGNORECASE,
)


def _values(chunk: str) -> frozenset[str]:
    return frozenset(re.sub(r"\s+", "", match.lower()) for match in _VALUE_RE.findall(chunk))


def score_blocks(
    text: str, blocks: list[tuple[int, int]], groups: tuple[KeywordGroup, ...]
) -> list[dict[str, int]]:
    """Per block: group name -> weighted number of distinct matching patterns
    (distinct, not total occurrences, so one long boilerplate paragraph
    repeating a word cannot outrank a short, dense clause)."""
    scores = []
    for start, end in blocks:
        chunk = text[start:end]
        score = BlockScore()
        score.strong = {}
        score.heading_strong = {}
        # A heading is a short first line ABOVE a body ("§ 5 Verlängerung"
        # + clause) - a single-line block (a table row) is not a heading.
        head_line, has_body, _ = chunk.partition("\n")
        first_line = head_line if has_body and len(head_line) <= _HEADING_MAX_CHARS else ""
        for group in groups:
            strong = frozenset(index for index, p in enumerate(group.strong) if p.search(chunk))
            score.strong[group.name] = strong
            score.heading_strong[group.name] = frozenset(
                index for index in strong if first_line and group.strong[index].search(first_line)
            )
            score[group.name] = STRONG_WEIGHT * len(strong) + WEAK_WEIGHT * sum(
                1 for p in group.weak if p.search(chunk)
            )
        score.value_keys = _values(chunk)
        scores.append(score)
    return scores


def _coverage(
    scores: list[dict[str, int]], group_names: tuple[str, ...], unit_values: list[frozenset[str]]
) -> set[int]:
    """Planner V2.1: the significant blocks that are mandatory - enough
    evidence to cover every field group, not every strong hit.

    Per group and strong pattern (e.g. "kündig", "verläng", an amount):
    1. the pattern's best block is mandatory. Best = the earliest block
       whose heading line names the topic ("§ 5 Verlängerung" - the clause
       itself), otherwise the highest-scoring block (earliest on ties);
    2. plus the next-best block that carries a concrete value (amount,
       duration, date, reference number) the first one doesn't - compared
       over each block's sentence unit (`unit_values`: the block plus the
       blocks it continues into/from), so a value cut off into the next
       block still counts - so
       "3 Monate" vs. "6 Monate" or two different prices both reach the
       model. Two differing values are enough for the prompt's
       contradiction rule (null + notes); further variants are supporting.
    Every other repeat of a covered pattern (boilerplate) is supporting.
    Near-linear: patterns x candidates (sorted once per pattern).

    Without pattern information (plain dict scores) every significant block
    is mandatory - the Planner V2 behavior."""
    result: set[int] = set()
    for name in group_names:
        candidates = [i for i, score in enumerate(scores) if score.get(name, 0) >= STRONG_WEIGHT]
        if not all(isinstance(scores[i], BlockScore) for i in candidates):
            result.update(candidates)
            continue
        by_pattern: dict[int, list[int]] = defaultdict(list)
        for i in candidates:
            for pattern in scores[i].strong[name]:
                by_pattern[pattern].append(i)
        for pattern, blocks_with_pattern in by_pattern.items():
            ranked = sorted(
                blocks_with_pattern,
                key=lambda i: (
                    pattern not in scores[i].heading_strong[name],
                    i if pattern in scores[i].heading_strong[name] else -scores[i][name],
                    i,
                ),
            )
            first = ranked[0]
            result.add(first)
            for i in ranked[1:]:
                if unit_values[i] - unit_values[first]:
                    result.add(i)
                    break
    return result


def select(
    text: str,
    blocks: list[tuple[int, int]],
    scores: list[dict[str, int]],
    group_names: tuple[str, ...],
    budget: int,
    *,
    head_chars: int,
    tail_chars: int,
    soft_budget: int | None = None,
    supporting_chars: int | None = None,
) -> Selection:
    """Greedy selection of verbatim blocks in two tiers (Planner V2).

    `budget` is the HARD limit (the real per-call budget) - never exceeded.
    `soft_budget` (default: `budget`) only bounds supporting context, and
    `supporting_chars` (default: no cap) additionally caps how much
    supporting context is added on top of the mandatory passages - free
    room is not filled just because it is there.

    Mandatory (within the hard budget, in this order):
    1. document head (master data: parties, number, subject);
    2. the COVERAGE set of significant hits (Planner V2.1, see _coverage):
       per group, the best block for each strong pattern plus one block
       with a differing concrete value (possible contradiction) - not every
       strong hit. Interleaved across groups, each with its minimal context
       only: the block it continues into/from mid-sentence or mid-table
       ("... beträgt" + "3 Monate ..."), a heading line right before it, the
       block its lead-in (":") introduces;
    3. per group, its best weak-only hit (some fields only ever have weak
       evidence, e.g. the payment interval) - one block per group at most.

    Supporting:
    4. document tail (signatures, annexes) - safety net, within the hard
       budget;
    5. redundant strong hits (best first), small neighbors of mandatory
       hits, other weak hits, then larger neighbors - within the soft
       budget and the supporting cap;
    6. only if there was no hit at all: evenly spaced blocks up to the soft
       budget (no cap), a conservative sample instead of trusting keywords.

    `hit_blocks`/`included_hits` count the mandatory (coverage) hits only -
    that is what decides whether the selection is complete (otherwise the
    planner chunks).
    """
    if not blocks:
        return Selection(text="", passage_count=0, hit_blocks=0, included_hits=0)

    soft = budget if soft_budget is None else min(soft_budget, budget)
    chosen: set[int] = set()
    order: list[int] = []
    mandatory: set[int] = set()
    used = 0

    def size(index: int) -> int:
        start, end = blocks[index]
        return end - start + 2

    def try_add(indices: list[int], limit: int = budget, *, required: bool = False) -> bool:
        nonlocal used
        new = [i for i in dict.fromkeys(indices) if i not in chosen]
        cost = sum(size(i) for i in new)
        if used + cost > limit:
            return False
        for i in new:
            chosen.add(i)
            order.append(i)
            if required:
                mandatory.add(i)
        used += cost
        return True

    def neighbors(core: int, *, small: bool) -> list[int]:
        return [
            i
            for i in (core - 1, core + 1)
            if 0 <= i < len(blocks) and (size(i) <= _SMALL_NEIGHBOR_CHARS) == small
        ]

    def continuation(core: int) -> list[int]:
        # Only the minimal context a hit cannot be read without, never a
        # chain: the block it continues from/into mid-sentence or mid-table,
        # a short heading line right before it, the block a lead-in
        # ("... wie folgt:") introduces.
        result = []
        if core > 0 and (_continues(text, blocks, core - 1) or _is_heading_line(text, blocks[core - 1])):
            result.append(core - 1)
        if core + 1 < len(blocks) and (_continues(text, blocks, core) or text[blocks[core][1] - 1] == ":"):
            result.append(core + 1)
        return result

    def add_hits(cores: list[int], limit: int, *, required: bool) -> None:
        for core in cores:
            linked = [core, *continuation(core)]
            if required:
                # Mandatory: the hit and its minimal context only.
                if not try_add(linked, limit, required=True):
                    try_add([core], limit, required=True)
            elif not try_add(linked + neighbors(core, small=True), limit):
                if not try_add(linked, limit):
                    try_add([core], limit)

    head_total = 0
    head_count = 0
    for index in range(len(blocks)):
        if head_total >= head_chars or not try_add([index], required=True):
            break
        head_total += size(index)
        head_count += 1

    hits = _interleaved_hits(scores, group_names)
    significant = [i for i in hits if max(scores[i].get(name, 0) for name in group_names) >= STRONG_WEIGHT]
    significant_set = set(significant)
    # Planner V2.1: only the coverage set is mandatory; redundant strong
    # hits (same pattern, no new value) become the first supporting tier.
    unit_values = [_unit_values(text, blocks, scores, i) for i in range(len(blocks))]
    covering = _coverage(scores, group_names, unit_values)
    required_hits = [i for i in significant if i in covering]
    add_hits(required_hits, budget, required=True)
    promoted = _best_weak_only_hits(scores, group_names)
    add_hits(promoted, budget, required=True)

    supporting_limit = soft
    if supporting_chars is not None:
        supporting_limit = min(supporting_limit, used + supporting_chars)

    tail_total = 0
    for index in range(len(blocks) - 1, -1, -1):
        if tail_total >= tail_chars:
            break
        if index not in chosen and not try_add([index]):
            break
        tail_total += size(index)

    add_hits([i for i in significant if i not in covering], supporting_limit, required=False)
    for core in required_hits:  # small neighbors of mandatory hits (follow-up sentence, lead-in)
        if core in chosen:
            for neighbor in neighbors(core, small=True):
                try_add([neighbor], supporting_limit)
    add_hits([i for i in hits if i not in significant_set], supporting_limit, required=False)
    for core in significant:
        if core in chosen:
            for neighbor in neighbors(core, small=False):
                try_add([neighbor], supporting_limit)

    if not hits:
        _add_even_sample(len(blocks), lambda indices: try_add(indices, soft))

    rendered = render(text, blocks, chosen)
    # Excerpt markers are added after selection - if they push the result
    # over the hard budget, give back the most recently added (lowest-
    # priority: supporting first) blocks, never the document head.
    while len(rendered) > budget and len(order) > head_count:
        chosen.discard(order.pop())
        rendered = render(text, blocks, chosen)

    mandatory_chars = sum(size(i) for i in chosen if i in mandatory)
    return Selection(
        text=rendered,
        passage_count=_run_count(chosen),
        hit_blocks=len(required_hits),
        included_hits=sum(1 for core in required_hits if core in chosen),
        mandatory_chars=mandatory_chars,
        supporting_chars=sum(size(i) for i in chosen) - mandatory_chars,
        candidate_count=len(significant),
        mandatory_block_count=sum(1 for i in chosen if i in mandatory),
    )


def _continues(text: str, blocks: list[tuple[int, int]], index: int) -> bool:
    """Block `index` and the next one belong together: one paragraph cut
    mid-sentence (no blank line, no sentence end at the cut), or one table
    cut between two rows (the touching lines both look like table rows -
    also across a blank line, so a header row keeps its value rows)."""
    end, next_start = blocks[index][1], blocks[index + 1][0]
    if "\n\n" not in text[end:next_start] and text[end - 1] not in ".;:!?":
        return True
    last_line = text[text.rfind("\n", blocks[index][0], end) + 1 : end]
    next_end = text.find("\n", next_start, blocks[index + 1][1])
    first_line = text[next_start : blocks[index + 1][1] if next_end == -1 else next_end]
    return is_table_like(last_line) and is_table_like(first_line)


def _unit_values(text: str, blocks: list[tuple[int, int]], scores: list, index: int) -> frozenset[str]:
    """Concrete values of a block together with the blocks it continues
    into/from (one sentence/table split by segmentation)."""

    def values(i: int) -> frozenset[str]:
        score = scores[i]
        return score.value_keys if isinstance(score, BlockScore) else frozenset()

    result = values(index)
    if index > 0 and _continues(text, blocks, index - 1):
        result |= values(index - 1)
    if index + 1 < len(blocks) and _continues(text, blocks, index):
        result |= values(index + 1)
    return result


def _is_heading_line(text: str, block: tuple[int, int]) -> bool:
    chunk = text[block[0] : block[1]]
    return "\n" not in chunk and len(chunk) <= _HEADING_MAX_CHARS and chunk[-1:] not in ".;!?"


def _best_weak_only_hits(scores: list[dict[str, int]], group_names: tuple[str, ...]) -> list[int]:
    """Per group, the block with the most weak matches among blocks without
    a strong one (earliest on ties). Some fields only ever have weak
    evidence (payment interval: "monatlich", "quartalsweise", "Rechnung";
    a bare start date), so each group's best such block is mandatory -
    at most one block per group, linear in the number of blocks."""
    result = []
    for name in group_names:
        best, best_score = None, 0
        for i, score in enumerate(scores):
            value = score.get(name, 0)
            if 0 < value < STRONG_WEIGHT and value > best_score:
                best, best_score = i, value
        if best is not None:
            result.append(best)
    return result


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
