"""The adaptive LinkedIn pace ladder: pure decision logic.

The state is a JSON-native dict persisted by `src/linkedin/pace.py` in
`pace_budgets.adaptive_state` and shared by the collector and the processor under
their one advisory lock. This module never touches the network, the clock or the
database; `review` is a pure function of its arguments.

The ladder's length comes from config (`levels_seconds`, eight as shipped: 75, 50, 37.5,
30, 25, 18.75, 15 and 10.7 s, i.e. 1x to 7x) and so does its ceiling (`max_level`). After
every request it protects itself on the trailing window (30 percent or more refused drops
one level; 50 percent or four in a row drops two), climbs only on a clean answer after 200
requests at the level with a cautious upper refusal estimate under 12 percent, a refused
share in the recent window below the protect drop rate and no refusal in the last few
requests, remembers a level that caused a drop, eases back when the description backlog is
small, and brakes to 150 s when a real block signal arrives.

Above 3x (`is_above_speed(policy, level, 3.0)`) the same protections apply plus five
guards, all deterministic and all config values: a climb into or within the above-3x
levels needs `climb_min_requests_above_3x` (600) requests at the current level, at most
`above_3x_climbs_per_day` (one) such climb happens per Rome day, a rolling 24 h request
count of `rolling_24h_ceiling_requests` (5,000) blocks above-3x climbs and drops the ladder
back to 3x, a level above 3x is dropped the moment fewer than
`demand_hold_above_3x_waiting` (250) descriptions wait, and no climb targets a level above
4x once `llm_above_4x_stop_share` (75 percent) of the ladder's OpenRouter stop line is
spent. A protect drop that starts above 3x never lands above 3x, and any protect drop or
brake from above 3x blocks above-3x climbing for `above_3x_hold_hours` (24). The protect
drop runs before the two holds, so a refusal cluster is always recorded even when the
queue or the ceiling crosses at the same request.

`review` returns JSON-native values only: datetimes are ISO-8601 strings and every read
goes through `parse_iso`.
"""
import copy
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.jobstore import budget as budget_module
from src.llm_limits import limit_for
from src.linkedin.schedule import MAX_LADDER_LEVELS, AdaptivePolicy

STATE_VERSION = 1
# The bound the state can hold. The ladder's own length is len(policy.levels_seconds) and
# its ceiling is policy.max_level; both are validated against this constant at load time.
MAX_LEVELS = MAX_LADDER_LEVELS
OUTCOMES = ("clean", "refused", "neutral")
BLOCK_KINDS = ("http_403", "http_999", "captcha", "max_refusal_wait")
MOVE_KINDS = ("drop", "climb", "brake", "pause", "brake cleared", "demand", "ceiling",
              "resumed", "enabled")
WINDOW_SECONDS = 24 * 3600
WINDOW_BUCKET_SECONDS = 3600


@dataclass(frozen=True)
class Signals:
    demand_waiting: int = 0
    llm_requests_today: int = 0
    llm_day: date | None = None
    llm_stop_requests: int = 800
    refreshed_at: datetime | None = None


@dataclass(frozen=True)
class ReplayEvent:
    at: datetime
    outcome: str
    block: str | None = None
    wait_seconds: float | None = None
    day: date | None = None


@dataclass(frozen=True)
class ReplayResult:
    moves: list
    drops: list
    brakes: list
    pauses: list
    levels_reached: list
    final_state: dict


def parse_iso(value, default=None):
    """A timezone-aware datetime from an ISO string or datetime, else `default`."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else default
    if not isinstance(value, str):
        return default
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return default
    return parsed if parsed.tzinfo is not None else default


def _iso(value) -> str | None:
    stamp = parse_iso(value)
    return stamp.isoformat() if stamp is not None else None


def _int(value, default=0, minimum=0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


def _blank_stint(cooldown_hours: float) -> dict:
    return {
        "requests": 0,
        "refusals": 0,
        "successes": 0,
        "active_seconds": 0.0,
        "first_at": None,
        "last_at": None,
        "cooldown_hours": float(cooldown_hours),
        "blocked_until": None,
    }


def blank_state(policy: AdaptivePolicy | None = None) -> dict:
    first = policy.memory_first_hours if policy is not None else 2
    count = len(policy.levels_seconds) if policy is not None else MAX_LEVELS
    return {
        "version": STATE_VERSION,
        "active": False,
        "level": 1,
        "trailing": [],
        "window": [],
        "day_above_3x_climbs": 0,
        "above_3x_hold_until": None,
        "levels": {str(level): _blank_stint(first) for level in range(1, count + 1)},
        "day": None,
        "day_requests": 0,
        "day_refusals": 0,
        "day_drops": 0,
        "day_brakes": 0,
        "last_move": None,
        "brake": None,
        "connection_errors": [],
        "paused": None,
        "signals": None,
        "last_ease_at": None,
    }


def _normalize_stint(raw, cooldown_hours: float) -> dict:
    stint = _blank_stint(cooldown_hours)
    if not isinstance(raw, dict):
        return stint
    stint["requests"] = _int(raw.get("requests"))
    stint["refusals"] = _int(raw.get("refusals"))
    stint["successes"] = _int(raw.get("successes"))
    seconds = raw.get("active_seconds", 0.0)
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0:
        stint["active_seconds"] = float(seconds)
    stint["first_at"] = _iso(raw.get("first_at"))
    stint["last_at"] = _iso(raw.get("last_at"))
    cooldown = raw.get("cooldown_hours", cooldown_hours)
    if isinstance(cooldown, (int, float)) and not isinstance(cooldown, bool) and cooldown > 0:
        stint["cooldown_hours"] = float(cooldown)
    stint["blocked_until"] = _iso(raw.get("blocked_until"))
    return stint


def normalize_state(loaded, policy: AdaptivePolicy) -> dict:
    """A safe, JSON-native state from anything. Never raises."""
    if not isinstance(loaded, dict) or loaded.get("version") != STATE_VERSION:
        return blank_state(policy)
    state = blank_state(policy)
    for key, value in loaded.items():
        if key in ("levels", "trailing", "version"):
            continue
        state[key] = copy.deepcopy(value)
    state["version"] = STATE_VERSION
    state["active"] = bool(loaded.get("active", False))
    ceiling = min(policy.max_level, len(policy.levels_seconds), MAX_LEVELS)
    state["level"] = max(1, min(ceiling, _int(loaded.get("level"), 1, 1)))
    state["window"] = _normalize_window(loaded.get("window"))
    state["day_above_3x_climbs"] = _int(loaded.get("day_above_3x_climbs"))
    state["above_3x_hold_until"] = _iso(loaded.get("above_3x_hold_until"))
    levels = loaded.get("levels")
    if isinstance(levels, dict):
        for level in range(1, len(state["levels"]) + 1):
            state["levels"][str(level)] = _normalize_stint(
                levels.get(str(level)), policy.memory_first_hours)
    trailing = loaded.get("trailing")
    if isinstance(trailing, list):
        state["trailing"] = [outcome for outcome in trailing
                             if outcome in OUTCOMES][-policy.protect_window_requests:]
    for key in ("day_requests", "day_refusals", "day_drops", "day_brakes"):
        state[key] = _int(loaded.get(key))
    day = loaded.get("day")
    state["day"] = day if isinstance(day, str) else None
    for key in ("last_move", "brake", "paused", "signals"):
        state[key] = copy.deepcopy(loaded.get(key)) if isinstance(loaded.get(key), dict) else None
    errors = loaded.get("connection_errors")
    state["connection_errors"] = [value for value in errors
                                  if isinstance(value, str)] if isinstance(errors, list) else []
    state["last_ease_at"] = _iso(loaded.get("last_ease_at"))
    if state["signals"] is not None:
        state["signals"]["at"] = _iso(state["signals"].get("at"))
    if state["brake"] is not None:
        state["brake"]["since"] = _iso(state["brake"].get("since"))
    if state["last_move"] is not None:
        state["last_move"]["at"] = _iso(state["last_move"].get("at"))
    return state


def current_level(state) -> int:
    if not isinstance(state, dict):
        return 1
    return max(1, min(MAX_LEVELS, _int(state.get("level"), 1, 1)))


def speed_multiple(policy: AdaptivePolicy, level: int) -> float:
    """How much faster `level` is than the base spacing (level 1 is 1x)."""
    level = max(1, min(len(policy.levels_seconds), int(level)))
    return policy.levels_seconds[0] / policy.levels_seconds[level - 1]


def is_above_speed(policy: AdaptivePolicy, level: int, speed: float) -> bool:
    """True when `level` is faster than `speed` times the base spacing (above 3x for 3.0)."""
    return speed_multiple(policy, level) > speed


def hold_level(policy: AdaptivePolicy, speed: float = 3.0) -> int:
    """The highest level that is not faster than `speed` times the base: the 3x level as
    shipped, and where every above-3x hold and drop lands."""
    best = 1
    for level in range(1, min(policy.max_level, len(policy.levels_seconds)) + 1):
        if speed_multiple(policy, level) <= speed:
            best = level
    return best


def level_text(policy: AdaptivePolicy, level: int) -> str:
    """`6/8 (18.75s, 4x)`: the level against the configured length, its spacing and speed."""
    spacing = policy.levels_seconds[max(1, min(len(policy.levels_seconds), int(level))) - 1]
    return (f"{level}/{len(policy.levels_seconds)} ({spacing:g}s, "
            f"{round(speed_multiple(policy, level), 1):g}x)")


def effective_spacing(policy: AdaptivePolicy, state, *, base: float | None = None) -> float:
    if not policy.enabled or not isinstance(state, dict) or not state.get("active"):
        return base if base is not None else policy.levels_seconds[0]
    if state.get("brake"):
        return policy.emergency_spacing_seconds
    level = current_level(state)
    return policy.levels_seconds[min(level, len(policy.levels_seconds)) - 1]


def paused_day(state) -> date | None:
    if not isinstance(state, dict) or not isinstance(state.get("paused"), dict):
        return None
    value = state["paused"].get("day")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def signals_from_state(state) -> Signals | None:
    if not isinstance(state, dict) or not isinstance(state.get("signals"), dict):
        return None
    raw = state["signals"]
    day = raw.get("llm_day")
    try:
        llm_day = date.fromisoformat(day) if isinstance(day, str) else None
    except ValueError:
        llm_day = None
    return Signals(
        demand_waiting=_int(raw.get("demand")),
        llm_requests_today=_int(raw.get("llm")),
        llm_day=llm_day,
        llm_stop_requests=_int(raw.get("llm_stop"), 800, 1),
        refreshed_at=parse_iso(raw.get("at")),
    )


def wilson_upper(refusals: int, requests: int, z: float = 1.96) -> float:
    requests = max(0, int(requests))
    refusals = max(0, min(int(refusals), requests))
    if requests == 0:
        return 1.0
    p = refusals / requests
    denominator = 1 + z * z / requests
    centre = p + z * z / (2 * requests)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * requests)) / requests)
    return min(1.0, (centre + margin) / denominator)


def _rate(stint: dict) -> float:
    seconds = stint.get("active_seconds", 0.0)
    if seconds <= 0 or stint.get("successes", 0) <= 0:
        return 0.0
    return stint["successes"] / (seconds / 3600.0)


def _signals_fresh(policy: AdaptivePolicy, signals: Signals | None, now: datetime) -> bool:
    if signals is None:
        return False
    if signals.refreshed_at is None:
        return True
    return now - signals.refreshed_at <= timedelta(seconds=2 * policy.signal_refresh_seconds)


def _normalize_window(raw) -> list:
    """A safe rolling-window list of [hour_start_iso, count], newest 26 buckets kept."""
    if not isinstance(raw, list):
        return []
    buckets = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        stamp, count = parse_iso(entry[0]), _int(entry[1])
        if stamp is None:
            continue
        buckets.append([stamp.astimezone(timezone.utc).isoformat(), count])
    buckets.sort(key=lambda item: item[0])
    return buckets[-(WINDOW_SECONDS // WINDOW_BUCKET_SECONDS + 2):]


def _bucket_start(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _window_add(state: dict, now: datetime) -> None:
    """Count this request in its hourly bucket and drop the buckets whose whole hour is
    already outside the window. Hourly buckets over-count by up to one hour, which makes
    the ceiling bind early rather than late."""
    start = _bucket_start(now)
    buckets = _normalize_window(state.get("window"))
    stamped = [(parse_iso(key), value) for key, value in buckets]
    horizon = now - timedelta(seconds=WINDOW_SECONDS)
    kept = [[stamp.isoformat(), value] for stamp, value in stamped
            if stamp is not None
            and stamp + timedelta(seconds=WINDOW_BUCKET_SECONDS) > horizon]
    for entry in kept:
        if entry[0] == start.isoformat():
            entry[1] += 1
            break
    else:
        kept.append([start.isoformat(), 1])
    kept.sort(key=lambda item: item[0])
    state["window"] = kept[-(WINDOW_SECONDS // WINDOW_BUCKET_SECONDS + 2):]


def rolling_24h_requests(state, now: datetime) -> int:
    """Requests in the last 24 hours, from the state's hourly buckets. A bucket counts
    while its end is still inside the window, so the count never covers less than 24 h."""
    if not isinstance(state, dict):
        return 0
    horizon = now - timedelta(seconds=WINDOW_SECONDS)
    total = 0
    for key, value in _normalize_window(state.get("window")):
        stamp = parse_iso(key)
        if stamp is not None and stamp + timedelta(seconds=WINDOW_BUCKET_SECONDS) > horizon:
            total += value
    return total


def _level_stint(state: dict, level: int) -> dict:
    return state["levels"][str(max(1, min(len(state["levels"]), level)))]


def _window_calm(policy: AdaptivePolicy, trailing: list) -> bool:
    """True when the recent window shows no refusal pressure: the refused share is
    below the protect drop rate and the last requests include no refusal, so a climb
    never starts on a refused answer or in the middle of a refusal streak."""
    if not trailing:
        return True
    if trailing.count("refused") / len(trailing) >= policy.protect_drop_rate:
        return False
    return "refused" not in trailing[-policy.protect_drop_streak:]


def _above_3x_blockers(policy: AdaptivePolicy, state: dict, now: datetime,
                       signals: Signals | None, level: int, zone=None) -> list[str]:
    """Why the ladder may not climb into or within the levels above 3x. Empty when the
    next level is not above 3x, so levels 1 to 3x keep exactly their old gates."""
    target = level + 1
    if target > policy.max_level or not is_above_speed(policy, target, 3.0):
        return []
    blockers = []
    count = rolling_24h_requests(state, now)
    if count >= policy.rolling_24h_ceiling_requests:
        blockers.append(f"rolling 24h {count}/{policy.rolling_24h_ceiling_requests} requests")
    used = _int(state.get("day_above_3x_climbs"))
    if used >= policy.above_3x_climbs_per_day:
        blockers.append(f"climb already used today ({used}/{policy.above_3x_climbs_per_day})")
    hold = parse_iso(state.get("above_3x_hold_until"))
    if hold is not None and hold > now:
        stamp = hold.astimezone(zone or ZoneInfo("UTC")).strftime("%H:%M")
        blockers.append(f"held until {stamp} Rome" if zone is not None else f"held until {stamp}")
    stint = _level_stint(state, level)
    if stint["requests"] < policy.climb_min_requests_above_3x:
        blockers.append(f"{stint['requests']}/{policy.climb_min_requests_above_3x} "
                        "requests at this level")
    if is_above_speed(policy, target, 4.0) and signals is not None:
        limit = int(round(policy.llm_above_4x_stop_share * signals.llm_stop_requests))
        if signals.llm_requests_today >= limit:
            blockers.append(f"OpenRouter {signals.llm_requests_today}/{limit} above 4x")
    return blockers


def above_3x_guard(policy: AdaptivePolicy, state, now: datetime,
                   signals: Signals | None = None, zone=None) -> str | None:
    """The first above-3x blocker, or None when nothing above 3x is holding the ladder."""
    state = normalize_state(state, policy)
    if signals is None:
        signals = signals_from_state(state)
    blockers = _above_3x_blockers(policy, state, now, signals, current_level(state), zone)
    return blockers[0] if blockers else None


def above_3x_status(policy: AdaptivePolicy, state, now: datetime, zone=None) -> str:
    """The `above 3x:` text the status page and the pace log both print."""
    state = normalize_state(state, policy)
    level = current_level(state)
    if level >= policy.max_level:
        return "at the ceiling" if is_above_speed(policy, policy.max_level, 3.0) else "n/a"
    if not is_above_speed(policy, level + 1, 3.0):
        return "n/a"
    blockers = climb_blockers(policy, state, now, signals_from_state(state), zone=zone)
    return blockers[0] if blockers else "ready"


def climb_blockers(policy: AdaptivePolicy, state: dict, now: datetime,
                   signals: Signals | None, zone=None,
                   throughput_check: bool = True) -> list[str]:
    state = normalize_state(state, policy)
    level = current_level(state)
    if not state["active"]:
        return ["not active"]
    if state.get("paused") is not None:
        return ["paused"]
    if state.get("brake") is not None:
        return ["climbing frozen"]
    if level >= policy.max_level:
        return ["top level"]
    if signals is None:
        signals = signals_from_state(state)
    blockers = _above_3x_blockers(policy, state, now, signals, level, zone)
    stint = _level_stint(state, level)
    if stint["requests"] < policy.climb_min_requests:
        blockers.append(f"{stint['requests']}/{policy.climb_min_requests} requests at this level")
    target = _level_stint(state, level + 1)
    blocked = parse_iso(target.get("blocked_until"))
    if blocked is not None and blocked > now:
        stamp = blocked.astimezone(zone or ZoneInfo("UTC")).strftime("%H:%M")
        suffix = " Rome" if zone is not None else ""
        blockers.append(f"level {level + 1} blocked until {stamp}{suffix}")
    if not _signals_fresh(policy, signals, now):
        blockers.append("signals unavailable")
    upper = wilson_upper(stint["refusals"], stint["requests"])
    if upper >= policy.climb_max_upper_refusal_rate:
        blockers.append(f"cautious upper {upper:.1%} >= {policy.climb_max_upper_refusal_rate:.0%}")
    if not _window_calm(policy, state["trailing"]):
        blockers.append("refusals in the recent window")
    if signals is not None and _signals_fresh(policy, signals, now):
        if signals.demand_waiting < policy.demand_min_waiting:
            blockers.append(f"backlog {signals.demand_waiting} < {policy.demand_min_waiting}")
        if signals.llm_requests_today >= signals.llm_stop_requests:
            blockers.append(
                f"OpenRouter {signals.llm_requests_today}/{signals.llm_stop_requests}")
    if throughput_check and level > 1:
        lower = _level_stint(state, level - 1)
        if lower["requests"] >= policy.climb_min_requests and _rate(stint) <= _rate(lower):
            blockers.append(f"successes/hour {_rate(stint):.0f} <= {_rate(lower):.0f}")
    return blockers


def _reset_stint(state: dict, level: int) -> None:
    state["levels"][str(level)] = _blank_stint(_level_stint(state, level)["cooldown_hours"])


def _move(state: dict, now: datetime, kind: str, source: int, target: int, reason: str) -> None:
    state["last_move"] = {
        "at": now.isoformat(),
        "kind": kind,
        "from": source,
        "to": target,
        "reason": reason,
    }


def _clean_stretch(policy: AdaptivePolicy) -> int:
    """The clean answers that make a brake episode over, or a persistent block real:
    half of the stretch that clears the brake."""
    return max(1, policy.emergency_clear_requests // 2)


def _enter_brake(state: dict, now: datetime, reason: str,
                 policy: AdaptivePolicy) -> None:
    """Enter the brake, or extend the current episode. A clean stretch of at least
    half the clearing length ends the episode, so a stray block signal after it starts
    a new clock instead of pausing LinkedIn for the day. A block retried inside one
    episode only extends it: one job-specific 403 must not look like four brakes."""
    brake = state.get("brake")
    previous = brake if isinstance(brake, dict) else {}
    clean_before_block = _int(previous.get("clean"))
    episode_over = clean_before_block >= _clean_stretch(policy)
    since = None if episode_over else parse_iso(previous.get("since"))
    old_level = current_level(state)
    if is_above_speed(policy, old_level, 3.0):
        state["above_3x_hold_until"] = (
            now + timedelta(hours=policy.above_3x_hold_hours)).isoformat()
    state["brake"] = {
        "since": (since or now).isoformat(),
        "reason": reason,
        "clean": 0,
        "last_block_at": now.isoformat(),
        "clean_before_block": clean_before_block,
    }
    state["level"] = 1
    _reset_stint(state, 1)
    state["trailing"] = []
    if since is None:
        state["day_brakes"] += 1
        _move(state, now, "brake", old_level, 1, reason)


def _brake_persisted(state: dict, now: datetime, policy: AdaptivePolicy) -> bool:
    """True when a block has persisted for the whole pause window. Age alone is not
    enough: the traffic between the previous block and this one must also still be
    short of the clean stretch that would have cleared the brake, otherwise two stray
    block signals an hour apart would shut LinkedIn down for the day."""
    brake = state.get("brake")
    if not isinstance(brake, dict):
        return False
    since = parse_iso(brake.get("since"))
    if since is None or now - since < timedelta(seconds=policy.pause_after_block_seconds):
        return False
    return _int(brake.get("clean_before_block")) < _clean_stretch(policy)


def _pause(state: dict, now: datetime, day: date) -> None:
    brake = state.get("brake") or {}
    state["paused"] = {"day": day.isoformat(), "reason": brake.get("reason", "block")}
    _move(state, now, "pause", 1, 1, brake.get("reason", "block"))


def review(state: dict, policy: AdaptivePolicy, now: datetime, *, day: date,
           outcome: str, level: int, block: str | None = None,
           signals: Signals | None = None,
           throughput_check: bool = True) -> dict:
    state = copy.deepcopy(normalize_state(state, policy))
    if outcome not in OUTCOMES:
        outcome = "neutral"
    if not policy.enabled:
        state["active"] = False
        state["level"] = 1
        state["trailing"] = []
        state["brake"] = None
        state["paused"] = None
        _move(state, now, "disabled", 1, 1, "adaptive off")
        return state

    if state.get("paused") is not None:
        if paused_day(state) != day:
            state["paused"] = None
            state["brake"] = None
            state["level"] = 1
            state["trailing"] = []
            _reset_stint(state, 1)
            _move(state, now, "resumed", 1, 1, "next day")
        else:
            return state

    if not state["active"]:
        state["active"] = True
        state["level"] = 1
        state["trailing"] = []
        _move(state, now, "enabled", 1, 1, "adaptive on")

    if state.get("day") != day.isoformat():
        if (state.get("day") is not None
                and state["day_requests"] >= policy.clean_day_min_requests
                and state["day_drops"] == 0 and state["day_brakes"] == 0):
            for stint in state["levels"].values():
                if stint["cooldown_hours"] > float(policy.memory_first_hours):
                    stint["cooldown_hours"] = max(
                        float(policy.memory_first_hours), stint["cooldown_hours"] / 2)
        state["day"] = day.isoformat()
        state["day_requests"] = 0
        state["day_refusals"] = 0
        state["day_drops"] = 0
        state["day_brakes"] = 0
        state["day_above_3x_climbs"] = 0

    level = max(1, min(len(policy.levels_seconds), int(level)))
    state["trailing"] = (state["trailing"] + [outcome])[-policy.protect_window_requests:]
    state["day_requests"] += 1
    _window_add(state, now)
    if outcome == "refused":
        state["day_refusals"] += 1
    if state.get("brake") is None:
        stint = _level_stint(state, level)
        last = parse_iso(stint.get("last_at"))
        if last is not None:
            gap = max(0.0, (now - last).total_seconds())
            stint["active_seconds"] += min(gap, policy.climb_max_gap_seconds)
        if stint.get("first_at") is None:
            stint["first_at"] = now.isoformat()
        stint["last_at"] = now.isoformat()
        stint["requests"] += 1
        if outcome == "clean":
            stint["successes"] += 1
        elif outcome == "refused":
            stint["refusals"] += 1

    if block is not None:
        if block == "connection":
            state["connection_errors"].append(now.isoformat())
            window = timedelta(seconds=policy.emergency_connection_window_seconds)
            state["connection_errors"] = [
                stamp for stamp in state["connection_errors"]
                if parse_iso(stamp) is not None and now - parse_iso(stamp) <= window]
            if len(state["connection_errors"]) >= policy.emergency_connection_errors:
                _enter_brake(state, now, block, policy)
                if _brake_persisted(state, now, policy):
                    _pause(state, now, day)
                return state
        else:
            _enter_brake(state, now, block, policy)
            if _brake_persisted(state, now, policy):
                _pause(state, now, day)
            return state

    brake = state.get("brake")
    if brake is not None:
        if outcome == "clean":
            brake["clean"] = _int(brake.get("clean")) + 1
        elif outcome == "refused":
            brake["clean"] = 0
        share = (state["trailing"].count("refused") / len(state["trailing"])
                 if state["trailing"] else 0.0)
        if brake["clean"] >= policy.emergency_clear_requests and share < policy.protect_drop_rate:
            state["brake"] = None
            state["level"] = 1
            state["trailing"] = []
            _reset_stint(state, 1)
            _move(state, now, "brake cleared", 1, 1, "clean stretch")
        return state

    level = current_level(state)
    if level > 1:
        window = policy.protect_window_requests
        streak = 0
        for value in reversed(state["trailing"]):
            if value == "refused":
                streak += 1
            else:
                break
        share = (state["trailing"].count("refused") / len(state["trailing"])
                 if len(state["trailing"]) >= policy.protect_min_requests else 0.0)
        drop = 0
        reason = ""
        if streak >= policy.protect_drop_streak:
            drop, reason = 2, f"protect: {streak} refusals in a row"
        elif share >= policy.protect_drop_two_rate:
            drop, reason = 2, f"protect: trailing {window} refused {round(share * 100)}%"
        elif share >= policy.protect_drop_rate:
            drop, reason = 1, f"protect: trailing {window} refused {round(share * 100)}%"
        if drop:
            stint = _level_stint(state, level)
            wait = stint["cooldown_hours"]
            stint["blocked_until"] = (now + timedelta(hours=wait)).isoformat()
            stint["cooldown_hours"] = min(float(policy.memory_max_hours), wait * 2)
            state["day_drops"] += 1
            target = max(1, level - drop)
            if is_above_speed(policy, level, 3.0):
                # A cluster above 3x is evidence against the whole above-3x regime, not
                # against one step: never land above 3x, and do not climb back up today.
                target = min(target, hold_level(policy))
                state["above_3x_hold_until"] = (
                    now + timedelta(hours=policy.above_3x_hold_hours)).isoformat()
            state["level"] = target
            _reset_stint(state, target)
            state["trailing"] = []
            _move(state, now, "drop", level, target, reason)
            return state

    fresh = _signals_fresh(policy, signals, now)
    if is_above_speed(policy, level, 3.0):
        if (fresh and signals is not None
                and signals.demand_waiting < policy.demand_hold_above_3x_waiting):
            target = hold_level(policy)
            state["level"] = target
            _reset_stint(state, target)
            state["trailing"] = []
            _move(state, now, "demand", level, target, "queue empty above 3x")
            return state
        count = rolling_24h_requests(state, now)
        if count >= policy.rolling_24h_ceiling_requests:
            target = hold_level(policy)
            state["level"] = target
            _reset_stint(state, target)
            state["trailing"] = []
            _move(state, now, "ceiling", level, target,
                  f"rolling 24h {count}/{policy.rolling_24h_ceiling_requests}")
            return state
    if fresh and signals is not None and level > 1:
        last_ease = parse_iso(state.get("last_ease_at"))
        if (signals.demand_waiting < policy.demand_ease_waiting
                and (last_ease is None
                     or now - last_ease >= timedelta(
                         seconds=policy.demand_ease_interval_seconds))):
            target = level - 1
            state["level"] = target
            _reset_stint(state, target)
            state["trailing"] = []
            state["last_ease_at"] = now.isoformat()
            _move(state, now, "demand", level, target, "demand fell")
            return state

    if outcome == "clean" and not climb_blockers(policy, state, now, signals,
                                                 throughput_check=throughput_check):
        stint = _level_stint(state, level)
        upper = wilson_upper(stint["refusals"], stint["requests"])
        rate = _rate(stint)
        lower = _rate(_level_stint(state, level - 1)) if level > 1 else 0.0
        target = min(level + 1, policy.max_level)
        state["level"] = target
        if is_above_speed(policy, target, 3.0):
            state["day_above_3x_climbs"] += 1
        _reset_stint(state, target)
        state["trailing"] = []
        _move(state, now, "climb", level, target,
              f"climb: cautious upper {upper:.1%}, {rate:.0f}/h vs {lower:.0f}/h")
    return state


def read_signals(conn, now: datetime, policy: AdaptivePolicy, *, limits=None) -> Signals:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM jobs WHERE stage = 'seen' AND tier IS NOT NULL "
            "AND description_status IN ('pending', 'rate_limited') "
            "AND next_attempt_at <= %s", (now,))
        demand = cur.fetchone()[0]
    usage = budget_module.used_today(conn, "openrouter", now)
    limit = limit_for("openrouter", "*", limits)
    if limit is not None and limit.unit == "requests" and limit.per_day is not None:
        stop = int(policy.llm_climb_stop_share * limit.per_day)
    else:
        stop = 800
    return Signals(demand, usage.requests, budget_module.utc_day(now), stop, now)


def maybe_refresh_signals(conn, state, policy: AdaptivePolicy, now: datetime,
                          reader=read_signals) -> dict:
    state = normalize_state(state, policy)
    current = signals_from_state(state)
    due = (current is None or current.refreshed_at is None
           or now - current.refreshed_at >= timedelta(seconds=policy.signal_refresh_seconds)
           or current.llm_day != budget_module.utc_day(now))
    if not due:
        return state
    try:
        fresh = reader(conn, now, policy)
    except Exception as exc:  # noqa: BLE001 - a signal fault must not stop the ladder
        print(f"[pace] adaptive signal read failed: {type(exc).__name__}: {exc}")
        return state
    state["signals"] = {
        "demand": fresh.demand_waiting,
        "llm": fresh.llm_requests_today,
        "llm_day": fresh.llm_day.isoformat() if fresh.llm_day is not None else None,
        "llm_stop": fresh.llm_stop_requests,
        "at": (fresh.refreshed_at or now).isoformat(),
    }
    return state


def event_from_csv_row(row: dict) -> ReplayEvent:
    at = datetime.fromisoformat(row["at"])
    if at.tzinfo is None:
        at = at.replace(tzinfo=ZoneInfo("UTC"))
    status = row.get("status") or ""
    outcome = {"ok": "clean", "refused": "refused"}.get(row.get("outcome", ""), "neutral")
    wait = row.get("wait_seconds")
    wait_seconds = float(wait) if wait not in (None, "") else None
    return ReplayEvent(at=at, outcome=outcome, block=None, wait_seconds=wait_seconds,
                       day=at.date())


def replay(events: list[ReplayEvent], policy: AdaptivePolicy, *,
           signals: Signals | None = None, start_level: int = 1,
           start_state: dict | None = None,
           throughput_check: bool = False,
           max_wait_seconds: float | None = None) -> ReplayResult:
    state = normalize_state(start_state, policy) if start_state is not None else blank_state(policy)
    if start_state is None:
        state["active"] = True
    state["level"] = max(1, min(len(policy.levels_seconds), int(start_level)))
    moves, drops, brakes, pauses, levels = [], [], [], [], []
    for event in sorted(events, key=lambda item: item.at):
        block = event.block
        if (block is None and max_wait_seconds is not None
                and event.outcome == "refused" and event.wait_seconds is not None
                and event.wait_seconds >= max_wait_seconds):
            block = "max_refusal_wait"
        before = state.get("last_move")
        state = review(state, policy, event.at, day=event.day or event.at.date(),
                       outcome=event.outcome, level=current_level(state), block=block,
                       signals=signals, throughput_check=throughput_check)
        after = state.get("last_move")
        if after is not None and after != before and after.get("kind") in MOVE_KINDS:
            moves.append((event.at, after["kind"], after["from"], after["to"],
                          after.get("reason", "")))
            if after["kind"] == "drop":
                drops.append((event.at, after["from"], after["to"], after.get("reason", "")))
            elif after["kind"] == "brake":
                brakes.append((event.at, after.get("reason", "")))
            elif after["kind"] == "pause":
                pauses.append(event.day or event.at.date())
        if not levels or levels[-1] != current_level(state):
            levels.append(current_level(state))
    return ReplayResult(moves, drops, brakes, pauses, levels, state)
