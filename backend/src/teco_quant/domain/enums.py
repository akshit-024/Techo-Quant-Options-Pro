"""Domain enums.

String enums keep persisted values and JSON payloads stable across Python versions.
"""

from enum import StrEnum


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"
    MCX = "MCX"


class MarketKind(StrEnum):
    INDEX = "INDEX"
    STOCK = "STOCK"
    COMMODITY = "COMMODITY"


class PricingModel(StrEnum):
    BLACK_SCHOLES = "BLACK_SCHOLES"
    BLACK_76 = "BLACK_76"


class OptionType(StrEnum):
    CALL = "CE"
    PUT = "PE"


class TradingStyle(StrEnum):
    INTRADAY = "INTRADAY"
    POSITIONAL = "POSITIONAL"


class OperatingMode(StrEnum):
    QUICK = "QUICK"
    PRO = "PRO"


class DataSource(StrEnum):
    DHAN_REST = "DHAN_REST"
    DHAN_LIVE = "DHAN_LIVE"
    CSV = "CSV"
    MANUAL = "MANUAL"
    REPLAY = "REPLAY"


class DecisionState(StrEnum):
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class DecisionReason(StrEnum):
    DATA_INCOMPLETE = "DATA_INCOMPLETE"
    DATA_STALE = "DATA_STALE"
    INVALID_EXPIRY = "INVALID_EXPIRY"
    EXTREME_EXPIRY_RISK = "EXTREME_EXPIRY_RISK"
    EVENT_RISK = "EVENT_RISK"
    NO_LIQUID_STRIKE = "NO_LIQUID_STRIKE"
    NOT_AFFORDABLE = "NOT_AFFORDABLE"
    BELOW_WATCHLIST_SCORE = "BELOW_WATCHLIST_SCORE"
    WATCHLIST_ONLY = "WATCHLIST_ONLY"
    CONFLICTING_SCORES = "CONFLICTING_SCORES"
    PRICE_ACTION_PENDING = "PRICE_ACTION_PENDING"
    QUICK_MODE_ONLY = "QUICK_MODE_ONLY"
    BULLISH_CONFIRMED = "BULLISH_CONFIRMED"
    BEARISH_CONFIRMED = "BEARISH_CONFIRMED"


class ScoreBand(StrEnum):
    STRONG = "STRONG"
    TRADABLE = "TRADABLE"
    WATCHLIST = "WATCHLIST"
    REJECTED = "REJECTED"


class ValidationSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class SnapshotStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
