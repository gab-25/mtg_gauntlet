"""Deterministic checks over decision traces (#73): no model, no database."""

from itertools import count

from playtest.checks import check_match, trace_stats
from playtest.models import DecisionTrace

ids = count(1)


def trace(kind="priority", options=(), choice=0, step="MAIN2", active=True, stack=(), hand=(), lands_played=0,
          seat=0, turn=3, prompt="", outcome="done"):
    state = {
        "active_player": "you" if active else "Opp",
        "you": {"hand": list(hand), "lands_played_this_turn": lands_played},
        "stack": list(stack),
    }
    return DecisionTrace(
        id=next(ids), seat_position=seat, turn=turn, step=step, kind=kind, prompt=prompt, state=state,
        options=[{"index": i, "label": spec[0], "facts": list(spec[1]), **({"outcome": spec[2]} if len(spec) > 2 else {})}
                 for i, spec in enumerate(options)],
        choice=choice, outcome=outcome,
    )


def _outcome(you_life, opp_life, lost=(), killed=(), hand=(5, 5)):
    """A bridge outcome: your life, the opponent's life, your creatures lost, theirs killed."""
    return {
        "you": {"name": "you", "life": list(you_life), "hand": list(hand), "graveyard": [0, 0], "lost": list(lost), "gained": [], "changed": []},
        "opponents": [{"name": "Opp", "life": list(opp_life), "hand": [5, 5], "graveyard": [0, 0], "lost": list(killed), "gained": [], "changed": []}],
        "summary": "x",
    }


PRIORITY = [("Pass priority", []), ("Play Mountain #5", []), ("Cast Shock #6 for {R}: deals 2 damage", [])]


def checks_of(*traces, fmt="modern"):
    flags = check_match(list(traces), fmt)
    return [[flag["check"] for flag in flags.get(t.id, [])] for t in traces]


def test_a_play_that_did_not_happen_is_flagged():
    assert checks_of(trace(options=PRIORITY, choice=2, outcome="no_effect")) == [["no_effect"]]


def test_passing_in_the_second_main_phase_with_spells_and_a_land():
    assert checks_of(trace(options=PRIORITY)) == [["pass_with_plays", "land_not_played"]]


def test_passing_is_fine_in_the_first_main_phase_with_a_land_played_later():
    first = trace(options=PRIORITY, step="MAIN1")
    land = trace(options=PRIORITY, step="MAIN2", choice=1)
    assert checks_of(first, land) == [[], []]


def test_passing_on_the_opponents_turn_or_with_a_stack_is_fine():
    assert checks_of(trace(options=PRIORITY, active=False), trace(options=PRIORITY, stack=[{"text": "x"}])) == [[], []]


def test_holding_instant_speed_spells_in_the_second_main_phase_is_fine():
    hand = [
        {"name": "Remand", "types": "Instant"},
        {"name": "Snapcaster Mage", "types": "Creature - Human Wizard", "text": "Flash\nWhen Snapcaster Mage enters, ..."},
        {"name": "Bear", "types": "Creature - Bear", "text": "Whenever a creature with flash enters, ..."},
    ]
    held = [("Pass priority", []), ("Cast Remand #7 for {1}{U}: Counter target spell.", []),
            ("Cast Snapcaster Mage #8 for {1}{U}: Snapcaster Mage - Creature 2 / 1", [])]
    sorcery = held + [("Cast Bear #9 for {1}{G}: Bear - Creature 2 / 2", [])]
    holding, passing = trace(options=held, hand=hand, turn=3), trace(options=sorcery, hand=hand, turn=5)
    flags = check_match([holding, passing], "modern")
    assert holding.id not in flags
    assert flags[passing.id] == [{"check": "pass_with_plays", "reason": "passed the second main phase with 1 spell castable"}]


def test_land_not_played_is_flagged_once_per_turn_on_the_last_pass():
    first, last = trace(options=PRIORITY, step="MAIN1"), trace(options=PRIORITY[:2], step="MAIN2")
    assert checks_of(first, last) == [[], ["land_not_played"]]


def test_a_land_already_played_this_turn_is_not_flagged():
    assert checks_of(trace(options=PRIORITY[:2], lands_played=1)) == [[]]


def test_ignoring_a_lethal_attack():
    options = [("Done (no more attackers)", []), ("Attack with Bear #3 (3/3) -> Opp", ["deals 3 damage if unblocked", "lethal if unblocked"])]
    assert checks_of(trace(kind="attack", options=options, choice=0)) == [["ignored_lethal"]]
    assert checks_of(trace(kind="attack", options=options, choice=1)) == [[]]


def test_leaving_lethal_damage_unblocked():
    options = [("Done (no more blockers)", []), ("Block Bear #3 (3/3) with Wall #4 (0/4)", ["your blocker survives", "the attacker survives"])]
    lethal = trace(kind="block", options=options, prompt="Declare blockers (unblocked attackers deal 5, your life 4)")
    safe = trace(kind="block", options=options, prompt="Declare blockers (unblocked attackers deal 3, your life 4)")
    assert checks_of(lethal, safe) == [["ignored_lethal"], []]


def test_attacking_into_a_blocker_that_kills_it():
    options = [("Done (no more attackers)", []), ("Attack with Goblin #4 (1/1) -> Opp", ["deals 1 damage if unblocked", "dies if blocked by: Wall #5 (0/4)"])]
    attack = trace(kind="attack", options=options, choice=1)
    flags = check_match([attack], "modern")
    assert flags[attack.id] == [{"check": "bad_attack", "reason": "attacks into blockers that kill it: Wall #5 (0/4)"}]


def test_a_lethal_attack_that_could_die_is_not_bad():
    options = [("Done (no more attackers)", []), ("Attack with Goblin #4 (1/1) -> Opp", ["lethal if unblocked", "dies if blocked by: Wall"])]
    assert checks_of(trace(kind="attack", options=options, choice=1)) == [[]]


def _hand(lands):
    return [{"name": "Mountain", "types": "Basic Land - Mountain"}] * lands + [{"name": "Shock", "types": "Instant"}] * (7 - lands)


def test_keeping_a_seven_with_too_few_or_too_many_lands():
    keep = [("Keep this hand", []), ("Mulligan", [])]
    hands = [trace(kind="mulligan", options=keep, hand=_hand(n), step="") for n in (1, 2, 5, 6)]
    assert checks_of(*hands) == [["mulligan_keep"], [], [], ["mulligan_keep"]]
    mulligan = trace(kind="mulligan", options=keep, choice=1, hand=_hand(0), step="")
    assert checks_of(mulligan) == [[]]


def test_delegated_decisions_are_not_checked():
    assert checks_of(trace(options=PRIORITY, choice=None, outcome="delegated")) == [[]]


def test_trace_stats_counts_each_seats_flags():
    a, b, c = trace(seat=0), trace(seat=0), trace(seat=1)
    a.flags = [{"check": "no_effect", "reason": ""}]
    b.flags = [{"check": "no_effect", "reason": ""}, {"check": "bad_attack", "reason": ""}]
    assert trace_stats([a, b, c]) == {0: {"flags": {"bad_attack": 1, "no_effect": 2}}, 1: {"flags": {}}}


def test_trace_stats_reports_agreement_with_forge_ai():
    a = trace(kind="attack", choice=1)
    a.forge_ai_choice = 1
    b = trace(kind="priority", choice=0)
    b.forge_ai_choice = 2
    c = trace(kind="priority", choice=None)
    c.forge_ai_choice = 2
    stats = trace_stats([a, b, c])
    assert stats[0]["agreement"] == {"rated": 2, "agree": 1, "by_kind": {"attack": [1, 1], "priority": [1, 0]}}


def test_ignored_lethal_reads_whole_attack_plans():
    options = [("No attack", []), ("Attack with Bear #3 (3/3) -> Player Opp (3 life)",
                                   ["Bear #3 (3/3): deals 3 damage if unblocked; lethal if unblocked"])]
    assert checks_of(trace(kind="attack", options=options, choice=0)) == [["ignored_lethal"]]
    blocks = [("No block", []), ("Block Bear #3 (3/3) with Wall #4 (0/4)", ["Wall #4 (0/4): your blocker survives; the attacker survives"])]
    lethal = trace(kind="block", options=blocks, prompt="Declare your blocks (unblocked attackers deal 5, your life 4)")
    assert checks_of(lethal) == [["ignored_lethal"]]


def test_bad_attack_on_a_plan_needs_every_attacker_to_die():
    both_die = [("No attack", []), ("Attack with Goblin #4 (1/1), Elf #5 (1/1) -> Player Opp (20 life)",
                                    ["Goblin #4 (1/1): dies if blocked by: Wall #9 (0/4)", "Elf #5 (1/1): dies if blocked by: Wall #9 (0/4)"])]
    assert checks_of(trace(kind="attack", options=both_die, choice=1)) == [["bad_attack"]]
    one_safe = [("No attack", []), ("Attack with Goblin #4 (1/1), Bear #3 (3/3) -> Player Opp (20 life)",
                                    ["Goblin #4 (1/1): dies if blocked by: Wall #9 (0/4)", "Bear #3 (3/3): deals 3 damage if unblocked"])]
    assert checks_of(trace(kind="attack", options=one_safe, choice=1)) == [[]]


def test_a_dominated_outcome_is_flagged():
    options = [("No attack", [], _outcome((20, 20), (20, 20))),
               ("Attack with Goblin #4 -> Opp", [], _outcome((20, 20), (20, 19), lost=["Goblin (1/1)"])),
               ("Attack with Bear #3 -> Opp", [], _outcome((20, 20), (20, 17)))]
    flagged = trace(kind="attack", options=options, choice=1)
    flags = check_match([flagged], "modern")
    assert flags[flagged.id] == [{"check": "worse_outcome", "reason": "Attack with Bear #3 -> Opp is better on every count"}]
    assert checks_of(trace(kind="attack", options=options, choice=2)) == [[]]


def test_an_outcome_better_on_one_count_is_not_dominated():
    options = [("No attack", [], _outcome((20, 20), (20, 20))),
               ("Attack with Goblin #4 -> Opp", [], _outcome((20, 20), (20, 16), lost=["Goblin (1/1)"])),
               ("Attack with Bear #3 -> Opp", [], _outcome((20, 20), (20, 17)))]
    assert checks_of(trace(kind="attack", options=options, choice=1)) == [[]]
    # Without outcomes (an unsimulated bridge) the check stays quiet.
    assert checks_of(trace(kind="attack", options=[("No attack", []), ("Attack with Bear #3 -> Opp", [])], choice=0)) == [[]]
