"""Microsoft Rewards: collects the day's points through the Rewards dashboard and Bing, the way a person would.

It signs in with the Microsoft Store's account (MS_EMAIL, MS_PASSWORD, MS_OTP_KEY) and browser profile, opens
the Rewards dashboard, works through the activities that still offer points (daily set, more activities, punch
cards) and then runs the Bing searches the dashboard says are left. Everything that earns points goes through
the browser: curved mouse paths, uneven clicks, wheel scrolling, key by key typing with the odd typo, and pauses
that vary from one step to the next. The one thing read without a click is the dashboard's own status data, which
the page fetches itself anyway, so the bot knows what is left and can check what it earned.

Mobile searches are not done: they need a phone's browser and touch input, and a desktop Chrome pretending
to be a phone is exactly what Microsoft looks for.

Microsoft's rules forbid automating Rewards, so this store is opt-in: it runs only when STORES names it.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import nodriver as uc
from sqlalchemy import select

from src.core.claimer import mask_account
from src.core.config import cfg
from src.core.database import ClaimedGame, async_session, get_or_create
from src.core.url_security import url_has_allowed_host
from src.stores.microsoft import MicrosoftClaimer

REWARDS_URL = "https://rewards.bing.com/"
REWARDS_HOSTS = ("rewards.bing.com", "rewards.microsoft.com")
BING_URL = "https://www.bing.com/"

# What a desktop search is worth and how many to try when the dashboard cannot be read.
POINTS_PER_SEARCH = 3
UNREADABLE_SEARCHES = 30

# Bounds, so a page that never settles cannot keep the bot busy for hours.
MAX_ACTIVITY_ROUNDS = 14
ACTIVITY_TIMEOUT = 300
MAX_FAILED_SEARCHES = 3


# ----- Pacing, all pure -----

def pause_seconds(lo: float, hi: float, rng=random, pace: float = 1.0) -> float:
    """How long to wait: usually between lo and hi, now and then a good deal longer, like someone reading."""
    wait = rng.uniform(lo, hi)
    if rng.random() < 0.08:
        wait *= rng.uniform(1.5, 3.0)
    return wait * pace


def search_gap(rng=random, pace: float = 1.0) -> float:
    """Seconds between two searches: mostly under a minute, once in a while a proper break."""
    if rng.random() < 0.12:
        return rng.uniform(60, 150) * pace
    return rng.uniform(9, 40) * pace


# ----- Search queries, all pure -----

THINGS = (
    "solar panels", "electric cars", "sourdough starter", "heat pumps", "mechanical keyboards", "raspberry pi",
    "indoor plants", "home insulation", "road bikes", "podcast equipment", "3d printers", "espresso machines",
    "hiking boots", "vegetable gardening", "board games", "noise cancelling headphones", "standing desks",
    "air fryers", "wireless chargers", "smart thermostats", "film cameras", "chess openings", "knitting patterns",
    "bird watching", "watercolour painting", "cold water swimming", "meal prepping", "language learning apps",
    "composting", "telescopes for beginners",
)
CITIES = (
    "london", "edinburgh", "manchester", "bristol", "cardiff", "leeds", "glasgow", "oxford", "brighton", "york",
    "dublin", "paris", "lisbon", "amsterdam", "berlin", "madrid", "rome", "prague", "vienna", "copenhagen",
)
FOODS = (
    "lasagne", "banana bread", "chicken curry", "shakshuka", "pancakes", "tomato soup", "flapjacks", "risotto",
    "fish pie", "chilli con carne", "carrot cake", "ratatouille", "paella", "overnight oats", "cottage pie",
    "scones", "minestrone", "falafel", "granola", "apple crumble",
)
TEMPLATES = (
    "{thing}", "best {thing}", "{thing} for beginners", "how do {thing} work", "{thing} buying guide",
    "{thing} reviews", "{thing} vs {other}", "is {thing} worth it", "{thing} tips",
    "weather {city}", "things to do in {city}", "{city} train times", "{city} museums",
    "{food} recipe", "easy {food} recipe", "how long to cook {food}", "{food} ingredients",
)


def build_queries(count: int, rng=random) -> list[str]:
    """Distinct, ordinary searches: the sort of thing anyone looks up on a quiet morning."""
    wanted = max(0, int(count))
    found: list[str] = []
    seen: set[str] = set()
    attempts = 0
    while len(found) < wanted and attempts < wanted * 40 + 40:
        attempts += 1
        thing, other = rng.sample(THINGS, 2)
        query = rng.choice(TEMPLATES).format(
            thing=thing, other=other, city=rng.choice(CITIES), food=rng.choice(FOODS))
        if query not in seen:
            seen.add(query)
            found.append(query)
    return found


# ----- Typing, all pure -----

KEY_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")


def neighbour_key(char: str, rng=random) -> str:
    """A key next to this one on a QWERTY keyboard, which is where a slipping finger lands."""
    for index, row in enumerate(KEY_ROWS):
        spot = row.find(char)
        if spot < 0:
            continue
        near = [row[spot - 1]] if spot > 0 else []
        if spot + 1 < len(row):
            near.append(row[spot + 1])
        if index + 1 < len(KEY_ROWS):
            below = KEY_ROWS[index + 1]
            near += [below[min(spot, len(below) - 1)]]
        return rng.choice(near) if near else char
    return char


def typing_plan(text: str, rng=random, typo_chance: float = 0.04) -> list[tuple]:
    """Key presses as (action, character, delay seconds): letters, now and then a wrong one taken back."""
    plan: list[tuple] = []
    for index, char in enumerate(text):
        delay = rng.uniform(0.05, 0.22)
        if char == " " or rng.random() < 0.05:
            # A word ends, or the mind wanders for a moment.
            delay += rng.uniform(0.08, 0.35)
        if char.isalpha() and index > 0 and rng.random() < typo_chance:
            wrong = neighbour_key(char.lower(), rng)
            if wrong != char.lower():
                plan.append(("type", wrong, rng.uniform(0.06, 0.2)))
                plan.append(("back", "", rng.uniform(0.2, 0.55)))
        plan.append(("type", char, delay))
    return plan


def typed_result(plan: list[tuple]) -> str:
    """What the box holds after a plan has been typed, used to prove the typos always get corrected."""
    out: list[str] = []
    for action, char, _ in plan:
        if action == "type":
            out.append(char)
        elif out:
            out.pop()
    return "".join(out)


# ----- Reading the dashboard, all pure -----

@dataclass(frozen=True)
class Task:
    """One dashboard activity that still offers points."""

    offer_id: str
    title: str
    url: str
    kind: str
    group: str
    remaining: int


def _as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _counter(counters: dict, name: str) -> tuple | None:
    """(points so far, points possible) of a search counter, None when the dashboard does not say."""
    entries = counters.get(name) if isinstance(counters, dict) else None
    first = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else None
    if not first:
        return None
    progress, maximum = _as_int(first.get("pointProgress")), _as_int(first.get("pointProgressMax"))
    if maximum is None:
        return None
    return (progress or 0, maximum)


def _open_task(item, group: str) -> Task | None:
    """The activity as a Task, or None when it is done, locked, or something a browser cannot do."""
    if not isinstance(item, dict) or item.get("complete") is True:
        return None
    if str(item.get("exclusiveLockedFeatureStatus") or "").lower() == "locked":
        return None
    maximum = _as_int(item.get("pointProgressMax"))
    if not maximum:
        return None
    remaining = maximum - (_as_int(item.get("pointProgress")) or 0)
    url = str(item.get("destinationUrl") or "")
    # App-only activities point at a store or app scheme, not a web page.
    if remaining <= 0 or (url and not url.lower().startswith("http")):
        return None
    # `name` is an internal id that never appears on the page, so it cannot be used to find the card.
    title = str(item.get("title") or "").strip()
    if not title and not url:
        return None
    return Task(
        offer_id=str(item.get("offerId") or item.get("name") or title),
        title=title or url,
        url=url,
        kind=str(item.get("promotionType") or ""),
        group=group,
        remaining=remaining,
    )


def _pick_day(sets: dict, today: str) -> list:
    """Today's daily set, or the newest one listed when today's key is missing; older days cannot be done."""
    if not isinstance(sets, dict) or not sets:
        return []
    if today in sets:
        return sets[today] or []

    def stamp(key):
        try:
            return datetime.strptime(key, "%m/%d/%Y")
        except ValueError:
            return datetime.min

    return sets[max(sets, key=stamp)] or []


def parse_dashboard(payload: dict, today: str = "") -> dict:
    """Balance, search counters and open activities out of the dashboard's status data."""
    data = payload.get("dashboard") if isinstance(payload, dict) and isinstance(payload.get("dashboard"), dict) \
        else (payload if isinstance(payload, dict) else {})
    status = data.get("userStatus") if isinstance(data.get("userStatus"), dict) else {}
    counters = status.get("counters") or {}

    tasks: list[Task] = []
    for item in _pick_day(data.get("dailySetPromotions"), today):
        tasks.append(_open_task(item, "daily set"))
    for item in data.get("morePromotions") or []:
        tasks.append(_open_task(item, "more activities"))
    for card in data.get("punchCards") or []:
        parent = card.get("parentPromotion") if isinstance(card, dict) else None
        if isinstance(parent, dict) and parent.get("complete") is True:
            continue
        for item in (card.get("childPromotions") or []) if isinstance(card, dict) else []:
            tasks.append(_open_task(item, "punch card"))

    seen: set[str] = set()
    unique: list[Task] = []
    for task in tasks:
        if task and task.offer_id not in seen:
            seen.add(task.offer_id)
            unique.append(task)
    return {
        "points": _as_int(status.get("availablePoints")),
        "pc": _counter(counters, "pcSearch"),
        "mobile": _counter(counters, "mobileSearch"),
        "tasks": unique,
    }


def searches_needed(counter: tuple | None, limit: int, per_search: int = POINTS_PER_SEARCH) -> int:
    """How many Bing searches to run: what the counter says is left, never more than the limit."""
    if limit <= 0:
        return 0
    if counter is None:
        return min(limit, UNREADABLE_SEARCHES)
    progress, maximum = counter
    return min(limit, math.ceil(max(0, maximum - progress) / max(1, per_search)))


# ----- Activity pages, all pure -----

def pick_option(options: list[dict], rng=random) -> dict | None:
    """The answer to click: the one the page marks as right when it says, otherwise any of them."""
    if not options:
        return None
    right = [o for o in options if o.get("correct") is True]
    return rng.choice(right or [o for o in options if o.get("correct") is not False] or options)


def account_day(now: datetime | None = None) -> str:
    """The date a daily record is kept under, in the zone the scheduler runs in."""
    try:
        zone = ZoneInfo(cfg.scheduler_timezone)
    except Exception:
        zone = None
    return (now or datetime.now(zone)).strftime("%Y-%m-%d")


# ----- What runs inside the page -----
# Selectors here are guesses from how Microsoft has built these pages so far, and they will drift: every
# lookup falls back to the text of the thing it wants, and a miss is logged rather than guessed at.

JS_HELPERS = r"""
    const VIS = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && parseFloat(st.opacity) > 0.1;
};
const RECT = el => {
    const r = el.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height,
            inView: r.top >= 60 && r.bottom <= innerHeight - 20};
};
const NORM = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
"""

DASHBOARD_JS = r"""
(async () => {
    try {
        const reply = await fetch('/api/getuserinfo?type=1', {credentials: 'include'});
        if (!reply.ok) return JSON.stringify({error: 'http ' + reply.status});
        return JSON.stringify(await reply.json());
    } catch (e) {
        return JSON.stringify({error: String(e)});
    }
})()
"""

def _script(body: str) -> str:
    """A page script as one function call: its helpers are declared inside it, so running it twice is safe."""
    return "(() => {" + JS_HELPERS + body + "})()"


PAGE_FACTS_JS = _script(r"""
    const signIn = [...document.querySelectorAll('a, button')].filter(VIS).find(el =>
        /^sign in$/.test(NORM(el.innerText)) || /login\.(live|microsoftonline)\.com/.test(el.href || ''));
    const text = NORM(document.body ? document.body.innerText : '');
    return JSON.stringify({
        host: location.host,
        path: location.pathname,
        signIn: signIn ? RECT(signIn) : null,
        notJoined: /^\/welcome/.test(location.pathname) || /\bjoin now\b|\bjoin microsoft rewards\b/.test(text.slice(0, 800)),
    });
""")

FIND_TASK_JS = _script(r"""
    const args = __ARGS__;
    const title = NORM(args.title);
    const path = (args.href || '').split('?')[0].replace(/\/+$/, '').toLowerCase();
    const clickable = [...document.querySelectorAll('a[href], button, [role="link"], [role="button"]')].filter(VIS);
    let hit = null;
    if (path) {
        hit = clickable.find(el => el.tagName === 'A'
            && (el.href || '').split('?')[0].replace(/\/+$/, '').toLowerCase() === path);
    }
    if (!hit && title) {
        const matches = clickable.filter(el => NORM(el.innerText || el.getAttribute('aria-label')).includes(title));
        matches.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
        hit = matches[0] || null;
    }
    return JSON.stringify(hit ? RECT(hit) : null);
""")

PAGE_STATE_JS = _script(r"""
    const text = NORM(document.body ? document.body.innerText : '');
    const sel = '[iscorrectoption], .rqOption, [id^="rqAnswerOption"], .btOptionCard, [id^="btoption"], .btOption, [data-option]';
    let found = [...document.querySelectorAll(sel)].filter(el =>
        VIS(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true');
    found = found.filter(el => !found.some(other => other !== el && other.contains(el)));
    const options = found.map((el, i) => {
        const flag = (el.getAttribute('iscorrectoption') || '').toLowerCase();
        return Object.assign({i: i, text: NORM(el.innerText).slice(0, 60),
                              correct: flag === 'true' ? true : (flag === 'false' ? false : null)}, RECT(el));
    });
    const button = re => [...document.querySelectorAll('button, a, input[type=submit], [role=button]')]
        .filter(VIS).find(b => re.test(NORM(b.innerText || b.value || b.getAttribute('aria-label')))
                               && !b.disabled);
    const start = button(/^(start|start playing|play|let's go|begin|get started)$/);
    const next = button(/^(next|continue|submit|done|finish)$/);
    return JSON.stringify({
        title: document.title.slice(0, 80),
        options: options,
        start: start ? RECT(start) : null,
        next: next ? RECT(next) : null,
        complete: /you('ve| have)? (earned|completed|finished)|well done|great job|congratulations|all done|points (earned|added)/.test(text),
    });
""")

SEARCH_BOX_JS = _script(r"""
    const box = [...document.querySelectorAll('#sb_form_q, textarea[name="q"], input[name="q"]')].find(VIS);
    return JSON.stringify(box ? RECT(box) : null);
""")


def _fill(script: str, **args) -> str:
    """A script with its arguments filled in as JSON."""
    return script.replace("__ARGS__", json.dumps(args))


class MSRewardsClaimer(MicrosoftClaimer):
    store_name = "msrewards"
    # One account, one sign-in: the Store and Rewards share the Microsoft profile.
    profile_name = "microsoft"

    def __init__(self) -> None:
        super().__init__()
        self.pace = max(25, cfg.msr_pace) / 100
        self.dashboard_tab = None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self, extra_games: list | None = None) -> None:
        """Collect what the dashboard offers today, once the day's record says there is something left."""
        account = mask_account(cfg.ms_email) or "Microsoft User"
        day = account_day()
        if await self._done_for(account, day):
            self.logger.info("Today's Rewards points are already collected.")
            return
        if not cfg.dryrun:
            # Nobody starts at the very second a schedule fires.
            await self.sleep(pause_seconds(5, 60, pace=self.pace))
        try:
            await self.start_browser()
            self.log_browser_ready()
            if not await self._ensure_logged_in():
                self.logger.error("Aborting Microsoft Rewards: not signed in.")
                return
            self.dashboard_tab = self.page
            if not await self._open_dashboard():
                return
            await self._collect(account, day)
        except Exception as exc:
            self.logger.exception("Fatal error during Microsoft Rewards flow")
            if cfg.notify_errors:
                await self.notify(f"{self.store_name} failed: {exc}")
        finally:
            await self.close_browser()

    async def _signed_in(self) -> None:
        """Note who we are; the Store's country lookup has no use here."""
        self.user = mask_account(cfg.ms_email) or "Microsoft User"
        self.log_signed_in(self.user)

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    async def _page_facts(self) -> dict:
        return await self._json_eval(self.page, PAGE_FACTS_JS)

    async def _rewards_ready(self) -> bool:
        """True on a Rewards page that shows no sign-in button."""
        facts = await self._page_facts()
        return bool(facts) and any(url_has_allowed_host(f"https://{facts.get('host', '')}/", host)
                                   for host in REWARDS_HOSTS) and not facts.get("signIn")

    async def _open_dashboard(self) -> bool:
        """Open the dashboard and get past Rewards' own sign-in, which is a step after the Microsoft one."""
        await self._open(REWARDS_URL)
        await self.sleep(pause_seconds(5, 9, pace=self.pace))
        await self._dismiss_cookies()
        facts = await self._page_facts()
        if facts.get("signIn"):
            self.logger.info("Rewards wants its own sign-in, clicking it.")
            await self._click_rect(facts["signIn"])
            await self.sleep(pause_seconds(6, 10, pace=self.pace))
            if not await self._rewards_ready():
                if cfg.ms_email and cfg.ms_password and not await self._is_logged_in():
                    await self._do_login(cfg.ms_email, cfg.ms_password)
                    await self.sleep(5)
                if not await self._rewards_ready():
                    signed = await self._wait_for_vnc_login(
                        self._rewards_ready,
                        custom_msg=self._vnc_notice(
                            "MS Rewards: sign-in needed",
                            "Rewards wants you signed in. Open the browser and finish it."),
                        store_key=self.store_name,
                    )
                    if not signed:
                        self.logger.warning("Rewards sign-in timed out, skipping.")
                        return False
            facts = await self._page_facts()
        if facts.get("notJoined"):
            self.logger.warning("This account has not joined Microsoft Rewards, so there is nothing to collect.")
            self.notify_games.append({"title": "Microsoft Rewards", "url": REWARDS_URL,
                                      "status": "skipped:not-enrolled"})
            return False
        if await self._human_challenge_present() and not await self._wait_out_challenge("MS Rewards"):
            self._captcha_stops_store(1, "dashboard")
            return False
        return True

    async def _read_state(self) -> dict | None:
        """The dashboard's own status data, read from inside the page; None when it will not answer."""
        try:
            raw = json.loads(await asyncio.wait_for(self.page.evaluate(DASHBOARD_JS, await_promise=True), 45))
        except Exception as exc:
            self.logger.debug("Dashboard data not readable: %s", exc)
            return None
        if not isinstance(raw, dict) or "error" in raw:
            self.logger.debug("Dashboard data not readable: %s", raw.get("error") if isinstance(raw, dict) else raw)
            return None
        today = datetime.now().strftime("%m/%d/%Y")
        state = parse_dashboard(raw, today)
        self.logger.debug("Dashboard: %s points, pc %s, %d open activit(ies)",
                          state["points"], state["pc"], len(state["tasks"]))
        return state

    # ------------------------------------------------------------------
    # The day's work
    # ------------------------------------------------------------------

    async def _collect(self, account: str, day: str) -> None:
        state = await self._read_state()
        unreadable = state is None
        if unreadable:
            self.logger.warning("The dashboard's status data could not be read, so only the searches run, "
                                "up to %d.", UNREADABLE_SEARCHES)
        tasks = list(state["tasks"]) if state else []
        random.shuffle(tasks)
        wanted = searches_needed(state["pc"] if state else None, cfg.msr_search_limit)
        before = state["points"] if state else None

        if cfg.dryrun:
            for task in tasks:
                self.logger.info("[DRYRUN] Would do '%s' (%s, %d point(s) left).", task.title, task.group, task.remaining)
                self.notify_games.append({"title": task.title, "url": REWARDS_URL, "status": "available (dry run)"})
            if wanted:
                self.notify_games.append({"title": f"{wanted} Bing search(es)", "url": BING_URL,
                                          "status": "available (dry run)"})
            return

        done, failed = 0, []
        for index, task in enumerate(tasks):
            if index:
                await self.sleep(pause_seconds(6, 25, pace=self.pace))
            outcome = await self._do_task(task)
            if outcome == "stop":
                break
            if outcome == "done":
                done += 1
            else:
                failed.append(task)
        if tasks:
            self.logger.info("Activities finished: %d of %d.", done, len(tasks))
        for task in failed:
            self.notify_games.append({"title": task.title, "url": REWARDS_URL, "status": "failed:activity"})
        if done:
            self.notify_games.append({"title": f"Rewards activities ({done} of {len(tasks)})",
                                      "url": REWARDS_URL, "status": "claimed"})

        searched = await self._do_searches(wanted)
        short = 0
        if wanted and searched:
            # One top-up pass, since a search is not always counted the first time round.
            await self._open(REWARDS_URL)
            await self.sleep(pause_seconds(5, 9, pace=self.pace))
            state = await self._read_state()
            left = searches_needed(state["pc"], cfg.msr_search_limit) if state and state["pc"] else 0
            if left:
                self.logger.info("%d more search(es) are still counted as open, one more round.", left)
                searched += await self._do_searches(min(left, 10))
            short = left
        if searched:
            self.notify_games.append({"title": f"Bing searches ({searched})", "url": BING_URL, "status": "claimed"})

        await self._open(REWARDS_URL)
        await self.sleep(pause_seconds(5, 9, pace=self.pace))
        final = await self._read_state()
        after = final["points"] if final else None
        if before is not None and after is not None and after > before:
            self.notify_games.append({"title": f"Balance {after:,} points (+{after - before:,})",
                                      "url": REWARDS_URL, "status": "claimed"})
        elif after is not None:
            self.logger.info("Rewards balance: %s points.", f"{after:,}")

        # The day counts as done only when the dashboard itself shows nothing left.
        everything = final is not None and not final["tasks"] and not (
            searches_needed(final["pc"], cfg.msr_search_limit) if final["pc"] else 0) and not short
        if everything or (unreadable and searched):
            await self._remember(account, day, "claimed")

    async def _do_task(self, task: Task) -> str:
        """Open one activity the way a person would and say how it went: done, failed or stop."""
        self.logger.info("Rewards activity: '%s' (%s).", task.title, task.group)
        try:
            return await asyncio.wait_for(self._run_task(task), timeout=ACTIVITY_TIMEOUT)
        except asyncio.TimeoutError:
            self.logger.warning("'%s' took longer than %ds, moving on.", task.title, ACTIVITY_TIMEOUT)
        except Exception as exc:
            self.logger.warning("'%s' went wrong: %s", task.title, exc)
        await self._back_to_dashboard()
        return "failed"

    async def _run_task(self, task: Task) -> str:
        await self._back_to_dashboard()
        rect = await self._scroll_to(FIND_TASK_JS, title=task.title, href=task.url)
        if rect is None:
            self.logger.warning("'%s' is not on the dashboard page, so it was left alone.", task.title)
            return "failed"
        known = {t.target.target_id for t in self.browser.tabs}
        await self._click_rect(rect)
        await self.sleep(pause_seconds(3.5, 6, pace=self.pace))

        await self.browser.update_targets()
        new = [t for t in self.browser.tabs if t.target.target_id not in known]
        opened = new[-1] if new else None
        if opened is not None:
            await opened.bring_to_front()
            self.page = opened
            self._mouse_xy = ()
        try:
            if await self._human_challenge_present() and not await self._wait_out_challenge("MS Rewards"):
                self._captcha_stops_store(1, "activity")
                return "stop"
            await self._work_the_page()
        finally:
            if opened is not None:
                try:
                    await opened.close()
                except Exception as exc:
                    self.logger.debug("Could not close the activity tab: %s", exc)
                self.page = self.dashboard_tab
                self._mouse_xy = ()
                await self.page.bring_to_front()
        await self._back_to_dashboard()
        state = await self._read_state()
        if state is None:
            return "done"
        still = {t.offer_id for t in state["tasks"]}
        if task.offer_id in still:
            self.logger.warning("'%s' still shows points left on the dashboard.", task.title)
            await self.take_screenshot(f"msrewards_{task.offer_id}")
            return "failed"
        return "done"

    async def _work_the_page(self) -> None:
        """Do what the open page asks: press Start, answer, press Next, until it says it is finished."""
        idle = 0
        for _ in range(MAX_ACTIVITY_ROUNDS):
            facts = await self._json_eval(self.page, PAGE_STATE_JS)
            if not facts:
                idle += 1
            elif facts.get("complete"):
                break
            elif facts.get("start"):
                await self._click_rect(facts["start"])
                idle = 0
            elif facts.get("options"):
                choice = pick_option(facts["options"])
                self.logger.debug("Answering '%s' on '%s'.", choice.get("text"), facts.get("title"))
                await self._click_rect(choice)
                await self.sleep(pause_seconds(1.5, 4, pace=self.pace))
                after = await self._json_eval(self.page, PAGE_STATE_JS)
                if after and after.get("next"):
                    await self._click_rect(after["next"])
                idle = 0
            elif facts.get("next"):
                await self._click_rect(facts["next"])
                idle = 0
            else:
                idle += 1
                if random.random() < 0.6:
                    width, height = await self._viewport()
                    await self._wheel_scroll(width * random.uniform(0.35, 0.65), height * random.uniform(0.4, 0.7),
                                             random.choice((240, 360, 480)))
            if idle >= 2:
                break
            await self.sleep(pause_seconds(2, 5, pace=self.pace))
        # A page earns its points after a moment on it, so nobody leaves the second it loads.
        await self.sleep(pause_seconds(3, 8, pace=self.pace))

    async def _back_to_dashboard(self) -> None:
        """Be on the dashboard in its own tab, with no activity tabs left open."""
        # The browser's tab list is only as fresh as its last refresh, and a tab opened a moment ago is missing.
        await self.browser.update_targets()
        for tab in list(self.browser.tabs):
            if tab.target.target_id != self.dashboard_tab.target.target_id:
                try:
                    await tab.close()
                except Exception as exc:
                    self.logger.debug("Could not close a stray tab: %s", exc)
        self.page = self.dashboard_tab
        await self.page.bring_to_front()
        location = await self._current_url()
        if not any(url_has_allowed_host(location, host) for host in REWARDS_HOSTS):
            await self._open(REWARDS_URL)
            await self.sleep(pause_seconds(4, 8, pace=self.pace))

    # ------------------------------------------------------------------
    # Searching
    # ------------------------------------------------------------------

    async def _do_searches(self, count: int) -> int:
        """Search Bing like someone with a few things on their mind, and say how many went through."""
        if count <= 0:
            return 0
        self.logger.info("Running %d Bing search(es), a few minutes apart in total.", count)
        queries = build_queries(count)
        await self._open(BING_URL)
        await self.sleep(pause_seconds(3, 7, pace=self.pace))
        done, misses = 0, 0
        for index, query in enumerate(queries):
            if await self._search_once(query):
                done, misses = done + 1, 0
            else:
                misses += 1
                if misses >= MAX_FAILED_SEARCHES:
                    self.logger.warning("Bing would not take %d searches in a row, stopping the searches.", misses)
                    await self.take_screenshot("msrewards_search")
                    break
            if index < len(queries) - 1:
                await self.sleep(search_gap(pace=self.pace))
        self.logger.info("Searched %d time(s).", done)
        return done

    async def _search_once(self, query: str) -> bool:
        if await self._human_challenge_present() and not await self._wait_out_challenge("MS Rewards"):
            self._captcha_stops_store(1, "search")
            return False
        box = await self._scroll_to(SEARCH_BOX_JS)
        if box is None:
            self.logger.debug("No search box on %s", await self._page_brief())
            await self._open(BING_URL)
            await self.sleep(pause_seconds(3, 6, pace=self.pace))
            return False
        await self._click_rect(box)
        await self.sleep(pause_seconds(0.3, 0.9, pace=self.pace))
        # Select what is in the box so the typing replaces it, as it would after a click and Ctrl+A.
        await self.page.evaluate("document.activeElement && document.activeElement.select && document.activeElement.select()")
        await self._human_type(query)
        await self.sleep(pause_seconds(0.4, 1.4, pace=self.pace))
        await self._press_enter()
        await self.sleep(pause_seconds(3, 6, pace=self.pace))
        if "/search" not in await self._current_url():
            return False
        # Look at the results for a while, maybe scroll down them.
        await self.sleep(pause_seconds(3, 9, pace=self.pace))
        if random.random() < 0.3:
            width, height = await self._viewport()
            await self._wheel_scroll(width * random.uniform(0.3, 0.6), height * random.uniform(0.4, 0.7),
                                     random.choice((240, 360, 480)))
        return True

    # ------------------------------------------------------------------
    # Hands: clicks and keys
    # ------------------------------------------------------------------

    async def _viewport(self) -> tuple[float, float]:
        try:
            width, height = json.loads(await self.page.evaluate("JSON.stringify([innerWidth, innerHeight])"))
            return float(width), float(height)
        except Exception:
            return 1280.0, 720.0

    async def _click_rect(self, rect: dict) -> None:
        await self._human_click(rect["x"], rect["y"], rect.get("w", 0), rect.get("h", 0))

    async def _scroll_to(self, script: str, **args) -> dict | None:
        """Find an element and wheel-scroll until it sits in view; None when the page has no such thing."""
        for _ in range(10):
            rect = await self._json_eval(self.page, _fill(script, **args))
            if not rect:
                return None
            if rect.get("inView", True):
                return rect
            width, height = await self._viewport()
            step = max(-600.0, min(600.0, rect["y"] - height / 2))
            await self._wheel_scroll(width * random.uniform(0.35, 0.65), height * random.uniform(0.4, 0.6), step)
        return await self._json_eval(self.page, _fill(script, **args)) or None

    async def _human_type(self, text: str) -> None:
        """Type like a keyboard does: a key down, a key up, uneven gaps, and the odd slip taken back."""
        for action, char, delay in typing_plan(text):
            if action == "back":
                await self.page.send(uc.cdp.input_.dispatch_key_event(
                    type_="rawKeyDown", key="Backspace", code="Backspace", windows_virtual_key_code=8))
                await self.page.send(uc.cdp.input_.dispatch_key_event(
                    type_="keyUp", key="Backspace", code="Backspace", windows_virtual_key_code=8))
            else:
                await self.page.send(uc.cdp.input_.dispatch_key_event(
                    type_="keyDown", text=char, unmodified_text=char, key=char))
                await asyncio.sleep(random.uniform(0.02, 0.07))
                await self.page.send(uc.cdp.input_.dispatch_key_event(type_="keyUp", key=char))
            await asyncio.sleep(delay * self.pace)

    async def _press_enter(self) -> None:
        await self.page.send(uc.cdp.input_.dispatch_key_event(
            type_="keyDown", text="\r", key="Enter", code="Enter", windows_virtual_key_code=13))
        await asyncio.sleep(random.uniform(0.04, 0.1))
        await self.page.send(uc.cdp.input_.dispatch_key_event(
            type_="keyUp", key="Enter", code="Enter", windows_virtual_key_code=13))

    # ------------------------------------------------------------------
    # Database: one record per day, so the browser opens only while something is left
    # ------------------------------------------------------------------

    async def _done_for(self, account: str, day: str) -> bool:
        async with async_session() as session:
            row = (await session.execute(select(ClaimedGame).where(
                ClaimedGame.store == self.store_name,
                ClaimedGame.user == account,
                ClaimedGame.game_id == f"day-{day}",
                ClaimedGame.status == "claimed",
            ))).scalar_one_or_none()
        return row is not None

    async def _remember(self, account: str, day: str, status: str) -> None:
        async with async_session() as session:
            obj, created = await get_or_create(
                session, store=self.store_name, user=account, game_id=f"day-{day}",
                title="Microsoft Rewards daily points", url=REWARDS_URL, status=status)
            if not created and obj.status != status:
                obj.status = status
            await session.commit()


async def claim_msrewards() -> dict:
    """Convenience entry point."""
    claimer = MSRewardsClaimer()
    await claimer.run()
    return {"store": "MS Rewards", "user": claimer.user, "games": claimer.notify_games}
