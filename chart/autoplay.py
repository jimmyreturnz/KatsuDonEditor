"""Which drum half an autoplay hits for every note, per playstyle.

Drum halves are numbered the way taiko players write them: 1 = left kat (rim),
2 = left don (centre), 3 = right don, 4 = right kat. The rules are the
owner's own play, taken down as 1234 strings over test patterns (2026-10-10)
and pinned by `tests/test_autoplay.py`:

* **Full alt** -- strict R/L alternation through every rest; only a break of
  a measure or more starts on the dominant hand again.
* **Single tap** -- every group (notes closer than half a beat) starts on the
  dominant hand, then alternates. TaikoToManiaConverter's kddk.
* **Semi alt** -- full alt, but a group of five or more after a rest restarts
  on the dominant hand, and about one shorter group in five does too.
* **Roll** -- single tap, plus a roll: at a colour change inside a group, when
  the run just finished has two or more notes and the new one an odd length,
  the hand that just hit hits again (`kkkdddk` = 4143234).
* **DDKK** -- one hand per colour; each colour run alternates its two halves
  from the start again.

A binding is the order the four physical keys (left kat, left don, right don,
right kat) light the drum in: 1324 puts each hand's don on the other side.
"""
from __future__ import annotations

FULL_ALT, SEMI_ALT, SINGLE_TAP, ROLL, DDKK = "full", "semi", "single", "roll", "ddkk"
STYLES = (FULL_ALT, SEMI_ALT, SINGLE_TAP, ROLL, DDKK)
BINDINGS = ("1234", "1324", "4231")

# Semi alt: groups of this many notes after a rest always start dominant,
# and this share of the shorter ones do (owner: "80% full alt, 20% single
# tap, longer than 5 notes more than likely started with dominant hand").
SEMI_RESET_LENGTH = 5
SEMI_RESET_SHARE = 0.2

# Physical key positions, left to right.
_LEFT_KAT, _LEFT_DON, _RIGHT_DON, _RIGHT_KAT = range(4)


def _semi_resets(time_ms: float) -> bool:
    # A hash of the note's own time rather than a random draw, so the preview
    # shows the same hands every frame and every time the map is opened.
    return (int(time_ms) * 2654435761) % 2**32 < SEMI_RESET_SHARE * 2**32


def _groups(notes, beat_ms: float) -> list[list]:
    groups: list[list] = []
    for note in notes:
        # Half a beat or more apart is a rest; a hair under counts, since a
        # snapped millisecond is truncated (`osu_snap_ms`).
        if groups and note[0] - groups[-1][-1][0] < beat_ms / 2 - 1:
            groups[-1].append(note)
        else:
            groups.append([note])
    return groups


def _run_lengths(colours: list[bool]) -> list[int]:
    """Length of the same-colour run each position belongs to."""
    lengths = [0] * len(colours)
    start = 0
    for index in range(1, len(colours) + 1):
        if index == len(colours) or colours[index] != colours[start]:
            lengths[start:index] = [index - start] * (index - start)
            start = index
    return lengths


def _kddk_hands(groups, style: str, beat_ms: float) -> list[int]:
    """0 = dominant hand, 1 = the other, per note."""
    hands: list[int] = []
    previous_time = None
    for group in groups:
        gap = float("inf") if previous_time is None else group[0][0] - previous_time
        after_break = gap >= 4 * beat_ms
        if style == FULL_ALT:
            restart = after_break
        elif style == SEMI_ALT:
            restart = (after_break or len(group) >= SEMI_RESET_LENGTH
                       or _semi_resets(group[0][0]))
        else:
            restart = True
        hand = 0 if restart or not hands else 1 - hands[-1]
        colours = [kat for _time, kat, _big in group]
        runs = _run_lengths(colours)
        for index in range(len(group)):
            if index:
                rolls = (style == ROLL and colours[index] != colours[index - 1]
                         and runs[index - 1] >= 2 and runs[index] % 2 == 1)
                hand = hand if rolls else 1 - hand
            hands.append(hand)
        previous_time = group[-1][0]
    return hands


def note_halves(notes, beat_ms: float, style: str, binding: str = "1234",
                left_handed: bool = False) -> list[tuple[int, ...]]:
    """Drum halves hit for each of `notes`, `(time, is_kat, is_finisher)`
    sorted by time. A finisher hits both halves of its colour."""
    groups = _groups(notes, beat_ms)
    if style == DDKK:
        # d on 2 then 3, k on 1 then 4; mirrored for the left hand.
        cycles = {False: (3, 2), True: (4, 1)} if left_handed else {False: (2, 3), True: (1, 4)}
        halves = []
        for group in groups:
            step = 0
            for index, (_time, kat, big) in enumerate(group):
                step = step + 1 if index and kat == group[index - 1][1] else 0
                pair = cycles[kat]
                halves.append(tuple(sorted(pair)) if big else (pair[step % 2],))
        return halves
    keys = [int(c) for c in binding]
    halves = []
    for (_time, kat, big), hand in zip(notes, _kddk_hands(groups, style, beat_ms)):
        right = (hand == 0) != left_handed
        if big:
            positions = (_LEFT_KAT, _RIGHT_KAT) if kat else (_LEFT_DON, _RIGHT_DON)
        elif kat:
            positions = (_RIGHT_KAT if right else _LEFT_KAT,)
        else:
            positions = (_RIGHT_DON if right else _LEFT_DON,)
        halves.append(tuple(sorted(keys[p] for p in positions)))
    return halves


def roll_halves(tick_times, left_handed: bool = False) -> list[tuple[float, tuple[int, ...]]]:
    """A drumroll's ticks (`gui.drumroll_tick_times`), dons alternating from
    the dominant hand, the way osu!'s autoplay plays one."""
    first, second = (2, 3) if left_handed else (3, 2)
    return [(t, (first if i % 2 == 0 else second,)) for i, t in enumerate(tick_times)]
