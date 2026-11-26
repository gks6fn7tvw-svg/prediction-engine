"""Read and check settings.yaml. Every setting has a safe default."""
from __future__ import annotations

import os
from dataclasses import replace, dataclass, field
from decimal import Decimal

import yaml

LIVE_CONFIRM_TEXT = "I accept real-money risk"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(PACKAGE_DIR, "settings.yaml")


class SettingsError(Exception):
    pass


def _dec(value, name: str, lo=None, hi=None, allow_none=False) -> Decimal | None:
    if value is None:
        if allow_none:
            return None
        raise SettingsError(f"{name} is missing")
    try:
        d = Decimal(str(value))
    except Exception:
        raise SettingsError(f"{name} must be a number, got {value!r}")
    if lo is not None and d < Decimal(str(lo)):
        raise SettingsError(f"{name} must be at least {lo}, got {value}")
    if hi is not None and d > Decimal(str(hi)):
        raise SettingsError(f"{name} must be at most {hi}, got {value}")
    return d


@dataclass
class Conditions:
    max_spread_cents: Decimal | None = None
    min_contracts_at_price: Decimal | None = None
    favorite_rising_seconds: Decimal | None = None
    btc_distance_min_dollars: Decimal | None = None
    min_score: Decimal | None = None          # only buy if the live score for that side is at least this (1-10)
    levels: tuple | None = None               # only buy at these levels (1, 2, 3); None = any level


@dataclass
class Size:
    mode: str                     # contracts | dollars | percent (of balance)
    value: Decimal
    max_contracts: Decimal        # 0 = no cap


SIZE_MODES = ("percent", "contracts", "dollars")


def _size(raw: dict, name: str, default_max) -> Size:
    mode = str(raw.get("mode", "contracts")).lower()
    if mode not in SIZE_MODES:
        raise SettingsError(f"{name}.mode must be percent, contracts or dollars, got {mode!r}")
    return Size(mode, _dec(raw.get("value", 1), f"{name}.value", 0),
                _dec(raw.get("max_contracts", default_max) or 0, f"{name}.max_contracts", 0))


@dataclass
class Play:
    name: str
    enabled: bool
    buy: str                      # favorite | underdog | up | down | pick (the side our chance says wins)
    min_price: Decimal
    max_price: Decimal
    act_from_minutes_left: Decimal
    act_until_minutes_left: Decimal
    slippage_cents: Decimal
    take_profit: Decimal | None
    stop_loss: Decimal | None
    conditions: Conditions
    size: Size | None = None      # None = use the default size
    subaccount: int = 0           # Kalshi subaccount for real money (0 = main account)
    pretend: bool = False         # always pretend money, even when the bot uses real money
    max_buys: int = 1             # buys per market (same side); more than 1 = add when a new chance shows up
    seconds_between_buys: Decimal = Decimal("60")

    @property
    def has_exits(self) -> bool:
        return self.take_profit is not None or self.stop_loss is not None


BOLDNESS = {"careful": Decimal("0.25"), "normal": Decimal("0.5"), "bold": Decimal("1")}


@dataclass
class Goal:
    """Goal mode: bet sizes are worked out automatically to grow the money toward a target."""
    enabled: bool = False
    target: Decimal = Decimal("100")
    boldness: str = "normal"        # careful / normal / bold = 1/4, 1/2 or all of the Kelly amount
    stop_below: Decimal = Decimal("0")
    max_bet_pct: Decimal = Decimal("25")


@dataclass
class Settings:
    mode: str
    live_confirm: str
    markets: list
    paper_balance: Decimal
    balance_share_pct: Decimal
    size_mode: str
    size_value: Decimal
    max_contracts: Decimal
    daily_loss_mode: str
    daily_loss_value: Decimal
    max_trades_per_day: int
    day_timezone: str
    plays: list = field(default_factory=list)
    fast_seconds: float = 1.0
    slow_seconds: float = 15.0
    folder: str = "data"
    api_base: str = "https://external-api.kalshi.com/trade-api/v2"
    goal: Goal = field(default_factory=Goal)
    watch: "Watch" = None

    def __post_init__(self):
        if self.watch is None:
            self.watch = Watch()

    @property
    def live(self) -> bool:
        return self.mode == "live"

    @property
    def default_size(self) -> Size:
        return Size(self.size_mode, self.size_value, self.max_contracts)

    def size_for(self, play: Play | None) -> Size:
        return play.size if play is not None and play.size is not None else self.default_size


def _levels(value, p: str) -> tuple | None:
    """[2, 3] or "2,3" -> (2, 3). Blank or all three = any level."""
    if value in (None, "", []):
        return None
    items = value if isinstance(value, (list, tuple)) else str(value).replace(" ", "").split(",")
    try:
        got = tuple(sorted({int(v) for v in items if str(v) != ""}))
    except ValueError:
        raise SettingsError(f"{p}.levels must be some of 1, 2, 3, like [2, 3]")
    if not got or any(v not in (1, 2, 3) for v in got):
        raise SettingsError(f"{p}.levels must be some of 1, 2, 3, like [2, 3]")
    return None if got == (1, 2, 3) else got


def _play(raw: dict, i: int) -> Play:
    name = str(raw.get("name") or f"play{i + 1}").strip()
    p = f"plays[{name}]"
    if "|" in name or len(name) > 40:
        raise SettingsError(f"play name {name!r}: use up to 40 characters and no | sign")
    buy = str(raw.get("buy", "favorite")).lower()
    if buy not in ("favorite", "underdog", "up", "down", "yes", "no", "pick", "our pick", "our_pick"):
        raise SettingsError(f"{p}.buy must be favorite, underdog, up, down or pick, got {buy!r}")
    buy = {"yes": "up", "no": "down", "our pick": "pick", "our_pick": "pick"}.get(buy, buy)
    lo = _dec(raw.get("min_price", 0.01), f"{p}.min_price", 0.01, 0.99)
    hi = _dec(raw.get("max_price", 0.99), f"{p}.max_price", 0.01, 0.99)
    if lo > hi:
        raise SettingsError(f"{p}.min_price ({lo}) is above max_price ({hi})")
    start = _dec(raw.get("act_from_minutes_left", 15), f"{p}.act_from_minutes_left", 0, 1440)
    end = _dec(raw.get("act_until_minutes_left", 0), f"{p}.act_until_minutes_left", 0, 1440)
    if end > start:
        raise SettingsError(f"{p}: act_until_minutes_left ({end}) must be smaller than "
                            f"act_from_minutes_left ({start}); the clock counts down")
    tp = _dec(raw.get("take_profit"), f"{p}.take_profit", 0.01, 0.99, allow_none=True)
    sl = _dec(raw.get("stop_loss"), f"{p}.stop_loss", 0.01, 0.99, allow_none=True)
    if tp is not None and sl is not None and sl >= tp:
        raise SettingsError(f"{p}: stop_loss ({sl}) must be below take_profit ({tp})")
    c = raw.get("conditions") or {}
    conditions = Conditions(
        max_spread_cents=_dec(c.get("max_spread_cents"), f"{p}.max_spread_cents", 0, 99, True),
        min_contracts_at_price=_dec(c.get("min_contracts_at_price"), f"{p}.min_contracts_at_price", 0, None, True),
        favorite_rising_seconds=_dec(c.get("favorite_rising_seconds"), f"{p}.favorite_rising_seconds", 1, 900, True),
        btc_distance_min_dollars=_dec(c.get("btc_distance_min_dollars"), f"{p}.btc_distance_min_dollars", 0, None, True),
        min_score=_dec(c.get("min_score"), f"{p}.min_score", 1, 10, True),
        levels=_levels(c.get("levels"), p),
    )
    size = raw.get("size")
    sub = _dec(raw.get("subaccount", 0) or 0, f"{p}.subaccount", 0, 63)
    if sub != sub.to_integral_value():
        raise SettingsError(f"{p}.subaccount must be a whole number from 0 to 63")
    buys = _dec(raw.get("max_buys_per_market", 1) or 1, f"{p}.max_buys_per_market", 1, 10)
    if buys != buys.to_integral_value():
        raise SettingsError(f"{p}.max_buys_per_market must be a whole number from 1 to 10")
    return Play(name, bool(raw.get("enabled", True)), buy, lo, hi, start, end,
                _dec(raw.get("slippage_cents", 0), f"{p}.slippage_cents", 0, 20), tp, sl, conditions,
                _size(size, f"{p}.size", 0) if isinstance(size, dict) and size else None, int(sub),
                bool(raw.get("pretend", False)), int(buys),
                _dec(raw.get("seconds_between_buys", 60), f"{p}.seconds_between_buys", 0, 900))


def parse(raw: dict) -> Settings:
    if not isinstance(raw, dict):
        raise SettingsError("settings file is empty or not in the right format")
    mode = str(raw.get("mode", "paper")).lower()
    if mode not in ("paper", "live"):
        raise SettingsError(f"mode must be paper or live, got {mode!r}")
    live_confirm = str(raw.get("live_confirm") or "")
    if mode == "live" and live_confirm != LIVE_CONFIRM_TEXT:
        raise SettingsError(f'mode is live, so live_confirm must say exactly: "{LIVE_CONFIRM_TEXT}"')
    markets = raw.get("markets") or ["KXBTC15M"]
    if not isinstance(markets, list) or not all(isinstance(m, str) and m for m in markets):
        raise SettingsError("markets must be a list like [KXBTC15M]")
    size = _size(raw.get("size") or {}, "size", 1)
    loss = raw.get("daily_loss_limit") or {}
    loss_mode = str(loss.get("mode", "dollars")).lower()
    if loss_mode not in ("percent", "dollars"):
        raise SettingsError(f"daily_loss_limit.mode must be percent or dollars, got {loss_mode!r}")
    plays = [_play(p or {}, i) for i, p in enumerate(raw.get("plays") or [])]
    names = [p.name for p in plays]
    if len(set(names)) != len(names):
        raise SettingsError("each play needs its own name")
    if not any(p.enabled for p in plays):
        raise SettingsError("turn on at least one play (enabled: true)")
    if mode == "live":
        # Kalshi keeps one side per market in each subaccount: two plays sharing one
        # would cancel each other's opposite bets and leave the records wrong.
        used: dict[int, str] = {}
        for p in plays:
            if p.pretend:
                continue          # pretend plays never touch Kalshi money
            if p.enabled and p.subaccount in used:
                raise SettingsError(
                    f"with real money, every play that's on needs its own Kalshi subaccount: "
                    f"'{p.name}' and '{used[p.subaccount]}' both use subaccount {p.subaccount}")
            if p.enabled:
                used[p.subaccount] = p.name
    polling = raw.get("polling") or {}
    files = raw.get("files") or {}
    s = Settings(
        mode=mode,
        live_confirm=live_confirm,
        markets=markets,
        paper_balance=_dec(raw.get("paper_balance", 20), "paper_balance", 0),
        balance_share_pct=_dec(raw.get("balance_share_pct", 50), "balance_share_pct", 0, 100),
        size_mode=size.mode,
        size_value=size.value,
        max_contracts=size.max_contracts,
        daily_loss_mode=loss_mode,
        daily_loss_value=_dec(loss.get("value", 5), "daily_loss_limit.value", 0),
        max_trades_per_day=int(raw.get("max_trades_per_day", 0) or 0),
        day_timezone=str(raw.get("day_timezone", "Pacific/Honolulu")),
        plays=plays,
        fast_seconds=float(polling.get("fast_seconds", 1)),
        slow_seconds=float(polling.get("slow_seconds", 15)),
        folder=str(files.get("folder", "data")),
        api_base=str(raw.get("api_base", "https://external-api.kalshi.com/trade-api/v2")),
        goal=_goal(raw.get("goal") or {}),
        watch=_watch(raw.get("last_minute") or {}),
    )
    if s.fast_seconds < 0.5 or s.slow_seconds < s.fast_seconds:
        raise SettingsError("polling: fast_seconds must be at least 0.5 and slow_seconds at least fast_seconds")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(s.day_timezone)
    except Exception:
        raise SettingsError(f"day_timezone {s.day_timezone!r} is not a known time zone")
    return s


def load(path: str) -> Settings:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        raise SettingsError(f"no settings file at {path}. Copy settings.example.yaml to settings.yaml first.")
    except yaml.YAMLError as e:
        raise SettingsError(f"settings file has a typo: {e}")
    return parse(raw)


def _goal(raw: dict) -> Goal:
    boldness = str(raw.get("boldness", "normal")).lower()
    if boldness not in BOLDNESS:
        raise SettingsError(f"goal.boldness must be careful, normal or bold, got {boldness!r}")
    g = Goal(enabled=bool(raw.get("enabled", False)),
             target=_dec(raw.get("target", 100), "goal.target", 0.01),
             boldness=boldness,
             stop_below=_dec(raw.get("stop_below", 0) or 0, "goal.stop_below", 0),
             max_bet_pct=_dec(raw.get("max_bet_pct", 25), "goal.max_bet_pct", 1, 100))
    if g.enabled and g.stop_below >= g.target:
        raise SettingsError("goal: the stop amount must be below the target")
    return g


@dataclass
class Watch:
    """Last-minute watch: logs what a guard and a follow idea WOULD do. It never trades."""
    enabled: bool = True
    guard_seconds: Decimal = Decimal("90")     # guard looks this many seconds before the close
    guard_margin: Decimal = Decimal("20")      # ...and acts once the estimate is this many $ against the bet
    follow_seconds: Decimal = Decimal("60")    # follow looks this many seconds before the close
    follow_margin: Decimal = Decimal("30")     # ...when the estimate is this many $ past the target
    follow_max_price: Decimal = Decimal("0.85")  # ...and that side costs at most this
    crowd_seconds: Decimal = Decimal("90")     # crowd: in the final stretch...
    crowd_price: Decimal = Decimal("0.50")     # ...if your bet's price falls to this, the crowd has switched
    score: bool = True                         # live 1-10 score for each side (our chance vs Kalshi's price)
    rsi_guess: bool = True                     # red 10-second guess on the RSI graph (for fun)

    @property
    def seconds(self) -> Decimal:
        """How long before each close the bot watches (and checks every second)."""
        return (max(self.guard_seconds, self.follow_seconds, self.crowd_seconds, Decimal("60"))
                if self.enabled else Decimal("0"))


def _watch(raw: dict) -> Watch:
    def cents(v, name):
        d = _dec(v, name, 1, 99)
        return d / 100 if d > 1 else d
    w = Watch(enabled=bool(raw.get("enabled", True)),
              guard_seconds=_dec(raw.get("guard_seconds", 90), "last_minute.guard_seconds", 5, 600),
              guard_margin=_dec(raw.get("guard_margin_dollars", 20), "last_minute.guard_margin_dollars", 0),
              follow_seconds=_dec(raw.get("follow_seconds", 60), "last_minute.follow_seconds", 5, 600),
              follow_margin=_dec(raw.get("follow_margin_dollars", 30), "last_minute.follow_margin_dollars", 0),
              follow_max_price=cents(raw.get("follow_max_cents", 85), "last_minute.follow_max_cents"),
              crowd_seconds=_dec(raw.get("crowd_seconds", 90), "last_minute.crowd_seconds", 5, 600),
              crowd_price=cents(raw.get("crowd_cents", 50), "last_minute.crowd_cents"),
              score=bool(raw.get("score", True)),
              rsi_guess=bool(raw.get("rsi_guess", True)))
    return w


def split_pretend(settings: Settings) -> tuple[Settings, Settings | None]:
    """Real money with some plays set to "pretend money only": two sets of settings.

    The real-money bot keeps every play but only the real ones are on; the pretend bot
    (paper mode, its own pretend money and records) has only the pretend ones on. Plays
    that are off in one still finish any bet they already hold there.
    """
    if settings.mode != "live" or not any(p.pretend and p.enabled for p in settings.plays):
        return settings, None
    real = replace(settings, plays=[replace(p, enabled=p.enabled and not p.pretend) for p in settings.plays])
    fake = replace(settings, mode="paper", live_confirm="",
                   plays=[replace(p, enabled=p.enabled and p.pretend) for p in settings.plays],
                   watch=Watch(enabled=False, score=False))
    return real, fake


def mode_folder(settings: Settings, mode: str | None = None) -> str:
    """Where a mode keeps its trade log, memory and log file (paper and live never mix)."""
    folder = settings.folder
    if not os.path.isabs(folder):
        folder = os.path.join(PACKAGE_DIR, folder)
    return os.path.join(folder, mode or settings.mode)


def load_raw(path: str) -> dict:
    """The settings file as plain data, without checking it."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return raw if isinstance(raw, dict) else {}


def save_raw(path: str, raw: dict) -> Settings:
    """Check new settings, then write them in one step. Raises SettingsError if they are bad."""
    checked = parse(raw)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("# Saved by the bot's website. Prices are in dollars (0.70 = 70 cents).\n")
        yaml.safe_dump(raw, fh, sort_keys=False, default_flow_style=False)
    os.replace(tmp, path)
    return checked
