"""The owner's own hands, as typed in the 2026-10-10 interview.

Notation: notes in a group are 1/4 apart, a space is a 1/2-beat rest, `/` a
1-beat rest, `|` a break of several seconds and `(...)` a 1/6 burst. Answers
are 1234 strings: 1 left kat, 2 left don, 3 right don, 4 right kat.
"""
import unittest

from chart.autoplay import DDKK, FULL_ALT, ROLL, SEMI_ALT, SINGLE_TAP, note_halves, roll_halves

BEAT = 500.0  # 120 BPM
ROUND_1 = "dddd kkk ddkdd/k/kdkkd (kkkd)d|ddd"
ROUND_2 = "ddk dd kdk/kd kkd ddddd/d kd"


def notes_from(pattern: str) -> list[tuple[float, bool, bool]]:
    notes, time, gap, burst = [], 0.0, None, False
    for char in pattern:
        if char in "dk":
            if gap is not None:
                time += gap if notes else 0.0
            notes.append((time, char == "k", False))
            gap = BEAT / 6 if burst else BEAT / 4
        elif char == "(":
            burst = True
        elif char == ")":
            burst = False
            gap = BEAT / 4
        else:
            gap = {" ": BEAT / 2, "/": BEAT, "|": 8 * BEAT}[char]
    return notes


def play(pattern: str, style: str, binding: str = "1234", left: bool = False) -> str:
    halves = note_halves(notes_from(pattern), BEAT, style, binding, left)
    return "".join(str(h[0]) for h in halves)


def digits(answer: str) -> str:
    return answer.replace(" ", "")


def mirrored(answer: str) -> str:
    return digits(answer).translate(str.maketrans("1234", "4321"))


class OwnerAnswerTests(unittest.TestCase):
    def test_full_alt(self):
        self.assertEqual(play(ROUND_1, FULL_ALT), digits("3232 414 23132 4 13142 4142 3 323"))

    def test_single_tap(self):
        self.assertEqual(play(ROUND_1, SINGLE_TAP), digits("3232 414 32423 4 42413 4142 3 323"))
        # The owner typed 43 for the first kd, and said doubles alternate.
        self.assertEqual(play(ROUND_2, SINGLE_TAP), digits("324 32 424 42 413 32323 3 42"))

    def test_roll(self):
        self.assertEqual(play(ROUND_2, ROLL), digits("321 32 424 42 412 32323 3 42"))
        self.assertEqual(play("kkddk", ROLL), "41321")
        self.assertEqual(play("kkkdddk", ROLL), "4143234")

    def test_ddkk(self):
        self.assertEqual(play(ROUND_1, DDKK), digits("2323 141 23123 1 12142 1412 3 232"))

    def test_bindings(self):
        self.assertEqual(play("kkddkddd", FULL_ALT), "41324232")
        self.assertEqual(play("kkddkddd", FULL_ALT, "1324"), "41234323")
        self.assertEqual(play("kkddkddd", FULL_ALT, "4231"), "14321232")

    def test_left_handed_is_the_mirror(self):
        for style, answer in ((FULL_ALT, "3232 414 23132 4 13142 4142 3 323"),
                              (DDKK, "2323 141 23123 1 12142 1412 3 232")):
            with self.subTest(style=style):
                self.assertEqual(play(ROUND_1, style, left=True), mirrored(answer))

    def test_semi_alt_starts_a_long_group_dominant_and_is_stable(self):
        pattern = "dd kdd/ddkdd kkkkkk/d ddddd"
        hands = play(pattern, SEMI_ALT)
        self.assertEqual(hands, play(pattern, SEMI_ALT))
        self.assertIn(hands[5], "34")   # ddkdd
        self.assertIn(hands[10], "34")  # kkkkkk
        self.assertIn(hands[17], "34")  # ddddd

    def test_a_finisher_hits_both_halves(self):
        halves = note_halves([(0.0, False, True), (125.0, True, True)], BEAT, FULL_ALT)
        self.assertEqual(halves, [(2, 3), (1, 4)])


class RollTickTests(unittest.TestCase):
    def test_ticks_alternate_dons_from_the_dominant_hand(self):
        self.assertEqual(roll_halves([0.0, 125.0, 250.0, 375.0]),
                         [(0.0, (3,)), (125.0, (2,)), (250.0, (3,)), (375.0, (2,))])
        self.assertEqual(roll_halves([0.0, 125.0], left_handed=True), [(0.0, (2,)), (125.0, (3,))])


if __name__ == "__main__":
    unittest.main()
