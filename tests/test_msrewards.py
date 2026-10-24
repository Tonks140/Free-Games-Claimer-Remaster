"""Microsoft Rewards: the parts that need no browser, no account and no Microsoft.

The dashboard data below follows the shape other open-source Rewards tools read from the dashboard. It is NOT
a capture from a live account, so these tests prove the parsing is careful with that shape, not that Microsoft
still serves it. The page scripts are not run here either; they are only checked for the one mistake that
breaks them at runtime (a top-level declaration that fails the second time a page runs it).
"""

import random
import re

import main
import pytest

from src.stores import msrewards
from src.stores.msrewards import (
    MSRewardsClaimer,
    Task,
    account_day,
    build_queries,
    neighbour_key,
    parse_dashboard,
    pause_seconds,
    pick_option,
    search_gap,
    searches_needed,
    typed_result,
    typing_plan,
)


def item(title="Quiz", offer="o1", progress=0, maximum=10, complete=False, url="https://www.bing.com/search?q=x", **more):
    return {"name": offer, "offerId": offer, "title": title, "pointProgress": progress,
            "pointProgressMax": maximum, "complete": complete, "destinationUrl": url,
            "promotionType": "quiz", **more}


class TestPacing:

    def test_a_pause_is_never_shorter_than_asked(self):
        rng = random.Random(1)
        assert all(pause_seconds(2, 5, rng) >= 2 for _ in range(500))

    def test_pace_stretches_every_pause(self):
        a, b = random.Random(7), random.Random(7)
        assert pause_seconds(2, 5, a, pace=2.0) == pytest.approx(2 * pause_seconds(2, 5, b, pace=1.0))

    def test_some_pauses_run_long_like_someone_reading(self):
        rng = random.Random(3)
        assert any(pause_seconds(2, 5, rng) > 5 for _ in range(500))

    def test_searches_are_spaced_out_with_the_odd_break(self):
        rng = random.Random(5)
        gaps = [search_gap(rng) for _ in range(500)]
        assert min(gaps) >= 9 and any(g >= 60 for g in gaps) and sum(g < 60 for g in gaps) > 350


class TestQueries:

    def test_the_count_asked_for_comes_back_without_repeats(self):
        queries = build_queries(40, random.Random(2))
        assert len(queries) == 40 and len(set(queries)) == 40

    def test_nothing_asked_nothing_returned(self):
        assert build_queries(0) == [] and build_queries(-3) == []

    def test_no_template_slot_is_left_unfilled(self):
        assert all(not re.search(r"[{}]", q) for q in build_queries(200, random.Random(4)))

    def test_the_same_seed_gives_the_same_searches(self):
        assert build_queries(10, random.Random(9)) == build_queries(10, random.Random(9))


class TestTyping:

    @pytest.mark.parametrize("seed", range(40))
    def test_whatever_slips_the_box_ends_up_holding_the_search(self, seed):
        text = "best electric cars for beginners"
        plan = typing_plan(text, random.Random(seed), typo_chance=0.3)
        assert typed_result(plan) == text

    def test_typos_happen_and_always_come_with_a_correction(self):
        plan = typing_plan("a long enough search to slip on", random.Random(1), typo_chance=0.5)
        backs = [i for i, step in enumerate(plan) if step[0] == "back"]
        assert backs and all(plan[i - 1][0] == "type" for i in backs)

    def test_the_first_key_is_never_a_slip(self):
        assert all(typing_plan("hello", random.Random(s), typo_chance=1.0)[0][1] == "h" for s in range(30))

    def test_every_key_takes_some_time(self):
        assert all(delay > 0 for _, _, delay in typing_plan("weather london", random.Random(8)))

    def test_a_slip_lands_on_a_neighbouring_key(self):
        assert neighbour_key("g", random.Random(1)) in set("fhtbv")
        assert neighbour_key("5") == "5"


class TestReadingTheDashboard:

    def test_balance_counters_and_open_activities(self):
        data = {"dashboard": {
            "userStatus": {"availablePoints": 12345, "counters": {
                "pcSearch": [{"pointProgress": 30, "pointProgressMax": 90}],
                "mobileSearch": [{"pointProgress": 0, "pointProgressMax": 60}]}},
            "dailySetPromotions": {"10/24/2026": [item("Poll", "d1"), item("Done", "d2", complete=True)]},
            "morePromotions": [item("Extra", "m1", progress=0, maximum=5)],
        }}
        state = parse_dashboard(data, "10/24/2026")
        assert state["points"] == 12345
        assert state["pc"] == (30, 90) and state["mobile"] == (0, 60)
        assert [t.offer_id for t in state["tasks"]] == ["d1", "m1"]
        assert state["tasks"][0] == Task("d1", "Poll", "https://www.bing.com/search?q=x", "quiz", "daily set", 10)

    def test_the_data_may_arrive_without_its_wrapper(self):
        assert parse_dashboard({"userStatus": {"availablePoints": 7}})["points"] == 7

    @pytest.mark.parametrize("bad", [
        item(complete=True),
        item(progress=10, maximum=10),
        item(maximum=0),
        item(exclusiveLockedFeatureStatus="locked"),
        item(url="msn://news"),
        item(title="", url=""),
        "not a dict",
        None,
    ])
    def test_what_cannot_or_need_not_be_done_is_left_out(self, bad):
        assert parse_dashboard({"morePromotions": [bad]})["tasks"] == []

    def test_part_done_activities_report_what_is_left(self):
        state = parse_dashboard({"morePromotions": [item(progress=4, maximum=10)]})
        assert state["tasks"][0].remaining == 6

    def test_only_todays_daily_set_counts(self):
        sets = {"10/23/2026": [item("Old", "old")], "10/24/2026": [item("New", "new")]}
        assert [t.offer_id for t in parse_dashboard({"dailySetPromotions": sets}, "10/24/2026")["tasks"]] == ["new"]

    def test_without_todays_key_the_newest_day_is_used(self):
        sets = {"10/21/2026": [item("A", "a")], "10/23/2026": [item("B", "b")]}
        assert [t.offer_id for t in parse_dashboard({"dailySetPromotions": sets}, "10/24/2026")["tasks"]] == ["b"]

    def test_punch_card_steps_count_until_the_card_is_done(self):
        open_card = {"parentPromotion": {"complete": False}, "childPromotions": [item("Step", "p1")]}
        done_card = {"parentPromotion": {"complete": True}, "childPromotions": [item("Step", "p2")]}
        state = parse_dashboard({"punchCards": [open_card, done_card]})
        assert [(t.offer_id, t.group) for t in state["tasks"]] == [("p1", "punch card")]

    def test_the_same_activity_is_listed_once(self):
        state = parse_dashboard({"morePromotions": [item(offer="x"), item(offer="x")]})
        assert len(state["tasks"]) == 1

    @pytest.mark.parametrize("payload", [{}, {"dashboard": {}}, {"dashboard": None}, [], None, "x"])
    def test_an_empty_or_odd_answer_is_not_a_crash(self, payload):
        state = parse_dashboard(payload)
        assert state["tasks"] == [] and state["points"] is None and state["pc"] is None


class TestHowManySearches:

    @pytest.mark.parametrize("counter,limit,expected", [
        ((0, 90), 50, 30),
        ((30, 90), 50, 20),
        ((88, 90), 50, 1),
        ((90, 90), 50, 0),
        ((0, 90), 10, 10),
        ((0, 150), 20, 20),
        ((0, 0), 50, 0),
        ((0, 90), 0, 0),
    ])
    def test_what_the_counter_leaves_capped_by_the_limit(self, counter, limit, expected):
        assert searches_needed(counter, limit) == expected

    def test_an_unreadable_counter_still_searches_a_sensible_amount(self):
        assert searches_needed(None, 50) == msrewards.UNREADABLE_SEARCHES
        assert searches_needed(None, 12) == 12 and searches_needed(None, 0) == 0


class TestChoosingAnAnswer:

    def test_the_page_marked_right_answer_wins(self):
        options = [{"i": 0, "correct": False}, {"i": 1, "correct": True}, {"i": 2, "correct": False}]
        assert all(pick_option(options, random.Random(s))["i"] == 1 for s in range(20))

    def test_a_poll_with_no_right_answer_gets_any_of_them(self):
        options = [{"i": 0, "correct": None}, {"i": 1, "correct": None}]
        assert {pick_option(options, random.Random(s))["i"] for s in range(40)} == {0, 1}

    def test_a_wrong_marked_answer_is_avoided_when_others_are_unmarked(self):
        options = [{"i": 0, "correct": False}, {"i": 1, "correct": None}]
        assert all(pick_option(options, random.Random(s))["i"] == 1 for s in range(20))

    def test_nothing_to_click_is_nothing(self):
        assert pick_option([]) is None


class TestTheRecordOfTheDay:

    def test_the_day_is_a_plain_date(self):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", account_day())


class TestPageScripts:

    SCRIPTS = {name: getattr(msrewards, name) for name in
               ("PAGE_FACTS_JS", "FIND_TASK_JS", "PAGE_STATE_JS", "SEARCH_BOX_JS", "DASHBOARD_JS")}

    @pytest.mark.parametrize("name", sorted(SCRIPTS))
    def test_a_script_is_one_call_so_running_it_twice_cannot_redeclare_anything(self, name):
        script = self.SCRIPTS[name].strip()
        assert script.startswith("(") and script.endswith("()")
        # Nothing may be declared outside the function: the second run on a page would fail on it.
        assert not re.match(r"\s*(const|let|var)\b", script)

    def test_arguments_are_filled_in_as_json(self):
        filled = msrewards._fill(msrewards.FIND_TASK_JS, title='He said "hi"', href="https://x.test/a?b=1")
        assert "__ARGS__" not in filled and r'He said \"hi\"' in filled


class TestRegistration:

    def test_it_is_a_store_you_can_pick(self):
        assert main.ALL_CLAIMERS["msrewards"][0] == "MS Rewards"
        assert "msrewards" in main._selectable()

    def test_it_never_runs_unless_named(self):
        # Microsoft forbids automating Rewards, so nobody gets it by accident.
        assert "msrewards" not in main.DEFAULT_STORES

    @pytest.mark.parametrize("name", ["msrewards", "rewards", "ms-rewards", "Microsoft-Rewards"])
    def test_its_names_resolve(self, name):
        assert main._resolve_stores([name]) == ["msrewards"]

    def test_the_microsoft_store_names_still_mean_the_store(self):
        assert main._resolve_stores(["ms", "microsoft", "xbox"]) == ["microsoft"]

    def test_it_rides_the_microsoft_sign_in(self):
        claimer = MSRewardsClaimer()
        assert claimer.store_name == "msrewards" and claimer.profile_name == "microsoft"

    def test_a_pace_below_the_floor_cannot_make_it_inhumanly_fast(self, monkeypatch):
        monkeypatch.setattr(msrewards.cfg, "msr_pace", 1)
        assert MSRewardsClaimer().pace == 0.25
