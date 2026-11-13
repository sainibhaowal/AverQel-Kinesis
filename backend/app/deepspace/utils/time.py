"""
DeepSpace Time Utilities - Python-native implementation of dsh-util-time.

Provides complete time vocabulary: instant parsing, IANA zone canonicalization,
local calendar parsing, offset instant parsing, four-digit-year instants,
local instant resolution with DST gap/overlap handling, Duration type,
Cron parsing with Vixie semantics, RRULE support, and recurrence arithmetic.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from types import NotImplementedType
from typing import Any, Literal, NotRequired, TypedDict
from zoneinfo import ZoneInfo

import dateutil.parser  # type: ignore[import-untyped]
import dateutil.relativedelta  # type: ignore[import-untyped]
import dateutil.tz  # type: ignore[import-untyped]
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dateutil import rrule

# ============================================================================
# Constants
# ============================================================================

# Inclusive lower epoch-millisecond bound of a four-digit-year RFC 3339 UTC instant.
MIN_FOUR_DIGIT_YEAR_MS = int(datetime(1, 1, 1, tzinfo=UTC).timestamp() * 1000)

# Inclusive upper epoch-millisecond bound of a four-digit-year RFC 3339 UTC instant.
MAX_FOUR_DIGIT_YEAR_MS = int(
    datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC).timestamp() * 1000
)

# Canonical four-digit-year RFC 3339 UTC instant with mandatory milliseconds.
UTC_INSTANT_PATTERN = re.compile(
    r"^(?!0000)\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{3}Z$"
)

# RFC 3339 instant with mandatory date, time, and explicit zone; fraction is optional.
OFFSET_INSTANT_PATTERN = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d{1,3}))?(?P<zone>Z|(?P<sign>[+-])"
    r"(?P<offsetHour>\d{2}):(?P<offsetMinute>\d{2}))$"
)

# Four-digit ISO calendar date.
LOCAL_DATE_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")

# Local wall-clock time with optional one-to-three digit fractional seconds.
LOCAL_TIME_PATTERN = re.compile(
    r"^(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(?:\.(?P<fraction>\d{1,3}))?$"
)

# Strict browser-zone profile: UTC or an IANA Area/Location-style identifier.
IANA_TIME_ZONE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)+$")

CRON_SEARCH_HORIZON_YEARS = 400


class RangeError(ValueError):
    """Invalid date, time-zone, or range value in the JavaScript-compatible API."""


# ============================================================================
# Types
# ============================================================================


@dataclass(frozen=True, slots=True)
class LocalCalendarTime:
    """Exact calendar fields of one local wall-clock value."""

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    millisecond: int


class InstantRejection(str, Enum):
    NOT_RFC3339 = "not_rfc3339"
    OUT_OF_RANGE = "out_of_range"
    NOT_A_CALENDAR_DATE = "not_a_calendar_date"
    INVALID_NUMERIC_OFFSET = "invalid_numeric_offset"


class LocalCalendarRejection(str, Enum):
    MALFORMED = "malformed"
    OUT_OF_RANGE = "out_of_range"
    NOT_A_CALENDAR_DATE = "not_a_calendar_date"


class TimeZoneRejection(str, Enum):
    BLANK = "blank"
    PADDED = "padded"
    NOT_AREA_LOCATION = "not_area_location"
    UNSUPPORTED = "unsupported"
    NOT_CANONICAL_SHAPE = "not_canonical_shape"


class InstantResultAccepted(TypedDict):
    kind: Literal["accepted"]
    epoch: int


class InstantResultRejected(TypedDict):
    kind: Literal["rejected"]
    reason: InstantRejection


InstantResult = InstantResultAccepted | InstantResultRejected


class LocalCalendarResultAccepted(TypedDict):
    kind: Literal["accepted"]
    time: LocalCalendarTime


class LocalCalendarResultRejected(TypedDict):
    kind: Literal["rejected"]
    reason: LocalCalendarRejection


LocalCalendarResult = LocalCalendarResultAccepted | LocalCalendarResultRejected


class TimeZoneResultAccepted(TypedDict):
    kind: Literal["accepted"]
    time_zone: str


class TimeZoneResultRejected(TypedDict):
    kind: Literal["rejected"]
    reason: TimeZoneRejection
    cause: NotRequired[BaseException | None]


TimeZoneResult = TimeZoneResultAccepted | TimeZoneResultRejected


@dataclass(frozen=True, slots=True)
class Duration:
    """A millisecond span with no implicit numeric conversion."""

    milliseconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.milliseconds, int):
            raise TypeError("Duration milliseconds must be an integer")

    def __add__(self, other: object) -> Duration | NotImplementedType:
        if not isinstance(other, Duration):
            return NotImplemented
        return Duration(self.milliseconds + other.milliseconds)

    def __sub__(self, other: object) -> Duration | NotImplementedType:
        if not isinstance(other, Duration):
            return NotImplemented
        return Duration(self.milliseconds - other.milliseconds)

    def __mul__(self, factor: object) -> Duration | NotImplementedType:
        if not isinstance(factor, int):
            return NotImplemented
        return Duration(self.milliseconds * factor)

    def __floordiv__(self, divisor: int) -> Duration:
        if not isinstance(divisor, int) or divisor == 0:
            raise ValueError("Divisor must be non-zero integer")
        return Duration(self.milliseconds // divisor)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Duration):
            return NotImplemented
        return self.milliseconds == other.milliseconds

    def __hash__(self) -> int:
        return hash(self.milliseconds)

    def __repr__(self) -> str:
        return f"Duration({self.milliseconds}ms)"

    @property
    def seconds(self) -> float:
        return self.milliseconds / 1000.0

    @property
    def minutes(self) -> float:
        return self.milliseconds / 60000.0

    @property
    def hours(self) -> float:
        return self.milliseconds / 3600000.0

    @classmethod
    def from_seconds(cls, seconds: float) -> Duration:
        return cls(int(seconds * 1000))

    @classmethod
    def from_minutes(cls, minutes: float) -> Duration:
        return cls(int(minutes * 60000))

    @classmethod
    def from_hours(cls, hours: float) -> Duration:
        return cls(int(hours * 3600000))


# ============================================================================
# Internal Helpers
# ============================================================================


def _rejected(reason: str) -> Any:
    return {"kind": "rejected", "reason": reason}


def _group_number(groups: dict[str, str | None], name: str) -> int:
    value = groups[name]
    if value is None:
        raise ValueError(f"Missing required regex group {name}")
    return int(value)


def _fractional_milliseconds(value: str | None) -> int:
    return 0 if value is None else int(value.ljust(3, "0")[:3])


def _in_four_digit_year_range(epoch_ms: int) -> bool:
    return (
        isinstance(epoch_ms, int) and MIN_FOUR_DIGIT_YEAR_MS <= epoch_ms <= MAX_FOUR_DIGIT_YEAR_MS
    )


def _plain_date_time(time: LocalCalendarTime) -> datetime | None:
    """Build a naive datetime, rejecting non-existent calendar dates."""
    try:
        return datetime(
            time.year,
            time.month,
            time.day,
            time.hour,
            time.minute,
            time.second,
            time.millisecond * 1000,
        )
    except ValueError:
        return None


def _canonicalize_zone_name(value: str) -> TimeZoneResult:
    """Validate and canonicalize an IANA zone selector."""
    if not value:
        return {"kind": "rejected", "reason": TimeZoneRejection.BLANK}
    if value.strip() != value:
        return {"kind": "rejected", "reason": TimeZoneRejection.PADDED}
    if value != "UTC" and not IANA_TIME_ZONE_PATTERN.match(value):
        return {"kind": "rejected", "reason": TimeZoneRejection.NOT_AREA_LOCATION}
    try:
        canonical = ZoneInfo(value).key
    except Exception as e:
        return {"kind": "rejected", "reason": TimeZoneRejection.UNSUPPORTED, "cause": e}
    if canonical != "UTC" and not IANA_TIME_ZONE_PATTERN.match(canonical):
        return {"kind": "rejected", "reason": TimeZoneRejection.NOT_CANONICAL_SHAPE}
    return {"kind": "accepted", "time_zone": canonical}


def _local_date_time(epoch_ms: int, tz: ZoneInfo) -> datetime:
    """Project an epoch into a timezone-aware datetime."""
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=tz).replace(tzinfo=None)


# ============================================================================
# Public API - Instant Parsing
# ============================================================================


def parse_local_calendar(date: str, time: str) -> LocalCalendarResult:
    """
    Parse a local calendar selector strictly.

    The date and time texts are matched separately, so a malformed spelling,
    a non-existent calendar date such as February 30, and an out-of-range
    clock field answer distinct reasons.
    """
    date_match = LOCAL_DATE_PATTERN.match(date)
    time_match = LOCAL_TIME_PATTERN.match(time)

    if not date_match or not time_match:
        return {"kind": "rejected", "reason": LocalCalendarRejection.MALFORMED}

    date_groups = date_match.groupdict()
    time_groups = time_match.groupdict()

    try:
        parts = LocalCalendarTime(
            year=int(date_groups["year"]),
            month=int(date_groups["month"]),
            day=int(date_groups["day"]),
            hour=int(time_groups["hour"]),
            minute=int(time_groups["minute"]),
            second=int(time_groups["second"]),
            millisecond=_fractional_milliseconds(time_groups.get("fraction")),
        )
    except (ValueError, KeyError):
        return {"kind": "rejected", "reason": LocalCalendarRejection.MALFORMED}

    if parts.year == 0 or parts.hour > 23 or parts.minute > 59 or parts.second > 59:
        return {"kind": "rejected", "reason": LocalCalendarRejection.OUT_OF_RANGE}

    if _plain_date_time(parts) is None:
        return {"kind": "rejected", "reason": LocalCalendarRejection.NOT_A_CALENDAR_DATE}

    return {"kind": "accepted", "time": parts}


def parse_offset_instant(value: str) -> InstantResult:
    """
    Parse an RFC 3339 instant whose numeric offset is part of the input.

    Both `Z` and explicit numeric offsets are accepted. A negative zero offset,
    an offset whose hour or minute is out of range, and a non-existent
    calendar date are refused.
    """
    match = OFFSET_INSTANT_PATTERN.match(value)
    if not match:
        return {"kind": "rejected", "reason": InstantRejection.NOT_RFC3339}

    groups = match.groupdict()

    try:
        parts = LocalCalendarTime(
            year=int(groups["year"]),
            month=int(groups["month"]),
            day=int(groups["day"]),
            hour=int(groups["hour"]),
            minute=int(groups["minute"]),
            second=int(groups["second"]),
            millisecond=_fractional_milliseconds(groups.get("fraction")),
        )
    except (ValueError, KeyError):
        return {"kind": "rejected", "reason": InstantRejection.NOT_RFC3339}

    if parts.year == 0 or parts.hour > 23 or parts.minute > 59 or parts.second > 59:
        return {"kind": "rejected", "reason": InstantRejection.OUT_OF_RANGE}

    local_dt = _plain_date_time(parts)
    if local_dt is None:
        return {"kind": "rejected", "reason": InstantRejection.NOT_A_CALENDAR_DATE}

    # Convert to UTC epoch
    local_epoch = int(local_dt.timestamp() * 1000)

    if groups["zone"] == "Z":
        return {"kind": "accepted", "epoch": local_epoch}

    try:
        offset_hour = int(groups["offsetHour"])
        offset_minute = int(groups["offsetMinute"])
    except (ValueError, KeyError, TypeError):
        return {"kind": "rejected", "reason": InstantRejection.INVALID_NUMERIC_OFFSET}

    if (
        offset_hour > 23
        or offset_minute > 59
        or (groups["sign"] == "-" and offset_hour == 0 and offset_minute == 0)
    ):
        return {"kind": "rejected", "reason": InstantRejection.INVALID_NUMERIC_OFFSET}

    direction = 1 if groups["sign"] == "+" else -1
    offset_ms = direction * (offset_hour * 60 + offset_minute) * 60000

    return {"kind": "accepted", "epoch": local_epoch - offset_ms}


def four_digit_year_epoch(value: str) -> InstantResult:
    """
    Validate a canonical four-digit-year RFC 3339 UTC instant.

    The accepted profile carries exactly three fractional digits and a `Z`.
    """
    if not UTC_INSTANT_PATTERN.match(value):
        return {"kind": "rejected", "reason": InstantRejection.NOT_RFC3339}

    try:
        epoch = int(dateutil.parser.isoparse(value).timestamp() * 1000)
    except Exception:
        return {"kind": "rejected", "reason": InstantRejection.NOT_A_CALENDAR_DATE}

    if not _in_four_digit_year_range(epoch):
        return {"kind": "rejected", "reason": InstantRejection.NOT_A_CALENDAR_DATE}

    # Verify round-trip
    try:
        dt = datetime.fromtimestamp(epoch / 1000.0, tz=UTC)
        # Format with exactly 3 fractional digits (milliseconds)
        roundtrip = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if roundtrip != value:
            return {"kind": "rejected", "reason": InstantRejection.NOT_A_CALENDAR_DATE}
    except Exception:
        return {"kind": "rejected", "reason": InstantRejection.NOT_A_CALENDAR_DATE}

    return {"kind": "accepted", "epoch": epoch}


def is_four_digit_year_instant(epoch_ms: int) -> bool:
    """Check if an epoch is representable as a canonical four-digit-year UTC instant."""
    return _in_four_digit_year_range(epoch_ms)


def format_four_digit_year_instant(epoch_ms: int) -> str | None:
    """Format an epoch as the canonical four-digit-year RFC 3339 UTC instant.

    The bound check runs first, so a caller that also compares the result with a
    decision time never formats an unrepresentable instant.
    """
    if not _in_four_digit_year_range(epoch_ms):
        return None
    try:
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)
        instant = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if not UTC_INSTANT_PATTERN.match(instant):
            return None
        return instant
    except Exception:
        return None


def canonicalize_time_zone(value: str) -> TimeZoneResult:
    """Validate and canonicalize a raw IANA zone selector."""
    return _canonicalize_zone_name(value)


def resolve_local_instant(
    time: LocalCalendarTime | datetime,
    time_zone: str,
) -> int | None:
    """
    Resolve a local wall-clock value in an explicit zone.

    A value inside a forward transition's gap does not exist (returns None).
    A value inside an overlap resolves to the earlier instant.

    Args:
        time: Exact calendar fields of a real date, or an already-built plain date-time.
        time_zone: Already-canonical `UTC` or IANA zone.

    Returns:
        The resolved epoch milliseconds, or None for a nonexistent wall-clock time.

    Raises:
        RangeError: when `time_zone` is not a known zone or the calendar value does not exist.
    """
    if isinstance(time, LocalCalendarTime):
        dt = _plain_date_time(time)
        if dt is None:
            raise RangeError("Local calendar time does not exist")
    else:
        dt = time.replace(tzinfo=None) if time.tzinfo else time

    try:
        tz = ZoneInfo(time_zone)
    except Exception as e:
        raise RangeError(f"Unknown time zone: {time_zone}") from e

    try:
        # Attach zone and resolve with 'earlier' disambiguation for overlaps
        zoned = dt.replace(tzinfo=tz)
        epoch_ms = int(zoned.timestamp() * 1000)

        # Check if the round-trip preserves the local time (detects gaps)
        roundtrip = datetime.fromtimestamp(epoch_ms / 1000.0, tz=tz).replace(tzinfo=None)
        if roundtrip == dt:
            return epoch_ms
        return None  # In a gap
    except Exception as e:
        raise RangeError(f"Failed to resolve local instant: {e}") from e


# ============================================================================
# Duration Arithmetic
# ============================================================================


def duration_from_ms(ms: int) -> Duration:
    return Duration(ms)


def duration_from_seconds(seconds: float) -> Duration:
    return Duration.from_seconds(seconds)


def duration_from_minutes(minutes: float) -> Duration:
    return Duration.from_minutes(minutes)


def duration_from_hours(hours: float) -> Duration:
    return Duration.from_hours(hours)


# ============================================================================
# Cron Parsing (Vixie semantics with 400-year horizon)
# ============================================================================


@dataclass(frozen=True, slots=True)
class CronFieldSpec:
    name: str
    min: int
    max: int
    canonical_max: int


CRON_FIELDS: tuple[CronFieldSpec, ...] = (
    CronFieldSpec("minute", 0, 59, 59),
    CronFieldSpec("hour", 0, 23, 23),
    CronFieldSpec("day-of-month", 1, 31, 31),
    CronFieldSpec("month", 1, 12, 12),
    CronFieldSpec("day-of-week", 0, 7, 6),  # Sunday = 0 or 7
)


@dataclass(frozen=True, slots=True)
class ParsedCronField:
    values: tuple[int, ...]
    canonical: str
    star: bool


class CronParseError(ValueError):
    pass


def _parse_cron_field(raw: str, spec: CronFieldSpec) -> ParsedCronField:
    """Parse one cron field into matched values and canonical text."""
    if not raw or raw.strip() != raw:
        raise CronParseError("Empty or padded field")

    matched: set[int] = set()

    for element in raw.split(","):
        if not element:
            raise CronParseError("Empty list element")

        if element == "*":
            matched.update(range(spec.min, spec.max + 1))
            continue

        if element.startswith("*"):
            # Star-step: */n
            match = re.match(r"^\*/(\d+)$", element)
            if not match:
                raise CronParseError(f"Invalid star-step: {element}")
            step = int(match.group(1))
            if step < 1:
                raise CronParseError("Step must be positive")
            for v in range(spec.min, spec.max + 1, step):
                matched.add(v)
            continue

        # Range or single value: a-b or a or a-b/n
        parsed = re.match(r"^(?P<start>\d+)(?:-(?P<end>\d+))?(?:\/(?P<step>\d+))?$", element)
        if not parsed:
            raise CronParseError(f"Invalid element: {element}")

        start = int(parsed.group("start"))
        if start < spec.min or start > spec.max:
            raise CronParseError(f"Value {start} out of range {spec.min}-{spec.max}")

        end_str = parsed.group("end")
        step_str = parsed.group("step")

        if end_str is None:
            if step_str:
                raise CronParseError("Step requires range")
            matched.add(start)
            continue

        end = int(end_str)
        if end < spec.min or end > spec.max:
            raise CronParseError(f"End value {end} out of range")
        if start > end:
            raise CronParseError(f"Inverted range {start}-{end}")

        step = int(step_str) if step_str else 1
        if step < 1:
            raise CronParseError("Step must be positive")

        for v in range(start, end + 1, step):
            matched.add(v)

    # Apply canonical max (Sunday 7 -> 0)
    values = []
    for v in sorted(matched):
        if v == spec.max and spec.canonical_max != spec.max:
            values.append(spec.min)
        else:
            values.append(v)

    return ParsedCronField(
        values=tuple(sorted(set(values))),
        canonical="",  # Will be set by caller
        star=raw.startswith("*"),
    )


def _encode_cron_field(values: tuple[int, ...], spec: CronFieldSpec) -> str:
    """Encode a value set as shortest cron field."""
    if not values:
        raise ValueError("Empty value set")
    if len(values) == 1:
        return str(values[0])

    first, last = values[0], values[-1]
    step = values[1] - first

    uniform = all(values[i] - values[i - 1] == step for i in range(2, len(values)))

    if uniform:
        if step == 1:
            return f"{first}-{last}"
        return f"{first}-{last}/{step}"

    parts = []
    run_start = first
    for i in range(1, len(values)):
        if values[i] != values[i - 1] + 1:
            if run_start == values[i - 1]:
                parts.append(str(run_start))
            else:
                parts.append(f"{run_start}-{values[i-1]}")
            run_start = values[i]
    if run_start == values[-1]:
        parts.append(str(run_start))
    else:
        parts.append(f"{run_start}-{values[-1]}")
    return ",".join(parts)


def _encode_star_field(values: tuple[int, ...], spec: CronFieldSpec) -> str:
    """Encode a field that started with * preserving star flag."""
    if len(values) == spec.canonical_max - spec.min + 1:
        return "*"

    present = set(values)
    best_step = None
    best_walk: list[int] = []

    for step in range(1, spec.max - spec.min + 2):
        walked = set()
        for v in range(spec.min, spec.max + 1, step):
            walked.add(spec.min if v == spec.max and spec.canonical_max != spec.max else v)
        if walked.issubset(present) and len(walked) > len(best_walk):
            best_step = step
            best_walk = list(walked)

    if best_step is None:
        raise CronParseError(f"{spec.name} cannot keep a leading `*`.")

    walked_set = set(best_walk)
    remaining = [v for v in values if v not in walked_set]
    walk_text = f"*/{best_step}"

    if not remaining:
        return walk_text
    return f"{walk_text},{_encode_cron_field(tuple(remaining), spec)}"


@dataclass(frozen=True, slots=True)
class CronFields:
    expression: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days_of_month: tuple[int, ...]
    months: tuple[int, ...]
    days_of_week: tuple[int, ...]
    day_of_month_star: bool
    day_of_week_star: bool


def parse_cron_expression(expression: str) -> CronFields:
    """Parse and canonicalize a strict five-field cron expression."""
    if not expression or expression.strip() != expression:
        raise CronParseError("Expression must be non-empty and trimmed")

    fields = expression.split()
    if len(fields) != 5:
        raise CronParseError(
            "Cron expression must contain exactly five whitespace-separated fields: "
            "minute hour day-of-month month day-of-week."
        )

    parsed_fields = []
    for _, (field, spec) in enumerate(zip(fields, CRON_FIELDS, strict=False)):
        parsed = _parse_cron_field(field, spec)
        canonical = (_encode_star_field if parsed.star else _encode_cron_field)(parsed.values, spec)
        parsed_fields.append(
            ParsedCronField(
                values=parsed.values,
                canonical=canonical,
                star=parsed.star,
            )
        )

    minute, hour, day_of_month, month, day_of_week = parsed_fields

    return CronFields(
        expression=" ".join(f.canonical for f in parsed_fields),
        minutes=minute.values,
        hours=hour.values,
        days_of_month=day_of_month.values,
        months=month.values,
        days_of_week=day_of_week.values,
        day_of_month_star=day_of_month.star,
        day_of_week_star=day_of_week.star,
    )


def _cron_date_match(parsed: CronFields) -> Callable[[date], bool]:
    """Check if a date matches the cron rule (Vixie semantics)."""
    months = set(parsed.months)
    days_of_month = set(parsed.days_of_month)
    days_of_week = set(parsed.days_of_week)
    dom_star = not parsed.day_of_month_star
    dow_star = not parsed.day_of_week_star

    def matches(date: date) -> bool:
        if date.month not in months:
            return False
        day_of_month = date.day in days_of_month
        day_of_week = date.weekday() in days_of_week  # Monday=0

        if dom_star and dow_star:
            return day_of_month or day_of_week  # Either restricted -> OR
        if dom_star:
            return day_of_month  # DOM restricted, DOW unrestricted
        return day_of_week  # DOW restricted, DOM unrestricted

    return matches


def _cron_times(parsed: CronFields) -> Iterator[int]:
    """Yield minute-of-day values in ascending order."""
    for hour in parsed.hours:
        for minute in parsed.minutes:
            yield hour * 60 + minute


def next_cron_target(parsed: CronFields, time_zone: str, floor: int) -> str | None:
    """Find the first strictly future occurrence after the given epoch."""
    if not is_four_digit_year_instant(floor):
        raise ValueError("Lower bound must be a four-digit-year instant")

    matches = _cron_date_match(parsed)
    months_set = set(parsed.months)
    floor_dt = datetime.fromtimestamp(floor / 1000.0, tz=ZoneInfo(time_zone))
    floor_minute = floor_dt.hour * 60 + floor_dt.minute
    floor_date = floor_dt.date()

    ceiling_dt = datetime.fromtimestamp(MAX_FOUR_DIGIT_YEAR_MS / 1000.0, tz=UTC)
    ceiling_date = ceiling_dt.date()
    horizon_date = floor_date + timedelta(days=CRON_SEARCH_HORIZON_YEARS * 365 + 100)
    last_date = min(horizon_date, ceiling_date)

    current_date = floor_date

    while current_date <= last_date:
        if current_date.month not in months_set:
            # Jump to next valid month
            if current_date.month == 12:
                current_date = current_date.replace(year=current_date.year + 1, month=1, day=1)
            else:
                current_date = current_date.replace(month=current_date.month + 1, day=1)
            continue

        if matches(current_date):
            for minute in _cron_times(parsed):
                if current_date == floor_date and minute < floor_minute:
                    continue
                dt = datetime.combine(current_date, datetime.min.time()) + timedelta(minutes=minute)
                try:
                    epoch_ms = resolve_local_instant(dt, time_zone)
                except RangeError:
                    continue
                if epoch_ms is None:
                    continue  # In a DST gap
                if epoch_ms > floor:
                    return format_four_digit_year_instant(epoch_ms)

        current_date += timedelta(days=1)

    return None


def latest_cron_occurrence(
    parsed: CronFields,
    time_zone: str,
    decision: int,
    saved_target: int,
) -> int:
    """Find the latest matching cron occurrence at or before a decision."""
    matches = _cron_date_match(parsed)
    months_set = set(parsed.months)

    # Offsets are less than 24 hours; a date-line rollback can make a due local date
    # later than the UTC decision date, which is why the walk starts one day after it.
    date = datetime.fromtimestamp(decision / 1000.0, tz=UTC).date() + timedelta(days=1)

    floor_date = datetime.fromtimestamp(MIN_FOUR_DIGIT_YEAR_MS / 1000.0, tz=UTC).date()
    horizon_date = date - timedelta(days=CRON_SEARCH_HORIZON_YEARS * 365 + 100)
    first_date = max(horizon_date, floor_date)

    while date >= first_date:
        if date.month not in months_set:
            date = date.replace(day=1) - timedelta(days=1)
            continue

        if matches(date):
            # Check if the earliest time on this date is after the decision
            earliest = None
            for minute in _cron_times(parsed):
                dt = datetime.combine(date, datetime.min.time()) + timedelta(minutes=minute)
                earliest = resolve_local_instant(dt, time_zone)
                if earliest is not None:
                    break

            if earliest is not None and earliest > decision:
                date -= timedelta(days=1)
                continue

            # Find the latest time on this date that is <= decision
            for minute in reversed(list(_cron_times(parsed))):
                dt = datetime.combine(date, datetime.min.time()) + timedelta(minutes=minute)
                candidate = resolve_local_instant(dt, time_zone)
                if (
                    candidate is not None
                    and candidate <= decision
                    and candidate >= MIN_FOUR_DIGIT_YEAR_MS
                ):
                    # A committed target remains due even if current tzdata places the rule before it.
                    return max(candidate, saved_target)

        date -= timedelta(days=1)

    return saved_target


@dataclass(frozen=True, slots=True)
class Cron:
    """Parsed Cron rule with zone-aware occurrence arithmetic."""

    _fields: CronFields
    time_zone: str
    _tz: ZoneInfo

    def __init__(self, fields: CronFields, time_zone: str) -> None:
        object.__setattr__(self, "_fields", fields)
        object.__setattr__(self, "time_zone", time_zone)
        object.__setattr__(self, "_tz", ZoneInfo(time_zone))

    @property
    def fields(self) -> CronFields:
        return self._fields

    @property
    def expression(self) -> str:
        return self._fields.expression

    @classmethod
    def parse(cls, expression: str, time_zone: str) -> Cron:
        """Parse a cron expression with IANA zone."""
        try:
            fields = parse_cron_expression(expression)
            zone = canonicalize_time_zone(time_zone)
            if zone["kind"] == "rejected":
                raise CronParseError(f"Invalid time zone: {zone['reason']}")
            return cls(fields, zone["time_zone"])
        except CronParseError:
            raise
        except Exception as e:
            raise CronParseError(str(e)) from e

    def next(self, after_epoch_ms: int) -> str | None:
        """First strictly future occurrence after the given epoch."""
        if not is_four_digit_year_instant(after_epoch_ms):
            raise ValueError("Lower bound must be a four-digit-year instant")
        return next_cron_target(self._fields, self.time_zone, after_epoch_ms)

    def resolve(self, scheduled_at: str, accepted_at: int) -> dict[str, str | None]:
        """Resolve latest due occurrence and next occurrence."""
        saved_target = int(
            datetime.fromisoformat(scheduled_at.replace("Z", "+00:00")).timestamp() * 1000
        )
        occurrence_ms = latest_cron_occurrence(
            self._fields, self.time_zone, accepted_at, saved_target
        )
        next_scheduled = self.next(accepted_at)
        return {
            "occurrence_at": format_four_digit_year_instant(occurrence_ms),
            "next_scheduled_at": next_scheduled,
        }


# ============================================================================
# RRULE Support (via dateutil.rrule)
# ============================================================================


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    """Wrapper around dateutil.rrule with Python-native interface."""

    freq: int
    interval: int = 1
    count: int | None = None
    until: datetime | None = None
    bysecond: list[int] | None = None
    byminute: list[int] | None = None
    byhour: list[int] | None = None
    byday: list[int] | None = None  # 0=MO, 6=SU
    bymonthday: list[int] | None = None
    bymonth: list[int] | None = None
    byyearday: list[int] | None = None
    byweekno: list[int] | None = None
    bysetpos: list[int] | None = None
    wkst: int = rrule.MO
    dtstart: datetime | None = None

    def to_rrule(self) -> rrule.rrule:
        """Convert to dateutil.rrule object."""
        kwargs: dict[str, Any] = {
            "freq": self.freq,
            "interval": self.interval,
            "wkst": self.wkst,
        }
        if self.count is not None:
            kwargs["count"] = self.count
        if self.until is not None:
            kwargs["until"] = self.until
        if self.bysecond is not None:
            kwargs["bysecond"] = self.bysecond
        if self.byminute is not None:
            kwargs["byminute"] = self.byminute
        if self.byhour is not None:
            kwargs["byhour"] = self.byhour
        if self.byday is not None:
            weekday_map = {
                0: rrule.MO,
                1: rrule.TU,
                2: rrule.WE,
                3: rrule.TH,
                4: rrule.FR,
                5: rrule.SA,
                6: rrule.SU,
            }
            kwargs["byweekday"] = [weekday_map[d] for d in self.byday]
        if self.bymonthday is not None:
            kwargs["bymonthday"] = self.bymonthday
        if self.bymonth is not None:
            kwargs["bymonth"] = self.bymonth
        if self.byyearday is not None:
            kwargs["byyearday"] = self.byyearday
        if self.byweekno is not None:
            kwargs["byweekno"] = self.byweekno
        if self.bysetpos is not None:
            kwargs["bysetpos"] = self.bysetpos
        if self.dtstart is not None:
            kwargs["dtstart"] = self.dtstart

        return rrule.rrule(**kwargs)

    def next(self, after: datetime, count: int = 1) -> list[datetime]:
        """Get next N occurrences after the given datetime."""
        rule = self.to_rrule()
        return list(rule.xafter(after, count=count))

    def between(self, after: datetime, before: datetime) -> list[datetime]:
        """Get all occurrences between two datetimes."""
        rule = self.to_rrule()
        return list(rule.between(after, before, inc=False))

    def __iter__(self) -> Iterator[datetime]:
        return iter(self.to_rrule())


# ============================================================================
# Scheduler Integration (APScheduler)
# ============================================================================


class ScheduleManager:
    """
    High-level scheduler using APScheduler with DeepSpace semantics.

    Provides:
    - Persistent schedules with misfire handling
    - Concurrency limits
    - Retries with backoff
    - Cancellation
    - Observability
    """

    def __init__(
        self,
        timezone: str = "UTC",
        max_concurrent: int = 10,
        misfire_grace_time: int = 300,
    ) -> None:
        self._timezone = ZoneInfo(timezone)
        self._scheduler = BackgroundScheduler(
            timezone=self._timezone,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": misfire_grace_time,
            },
        )
        self._max_concurrent = max_concurrent

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self, wait: bool = True) -> None:
        self._scheduler.shutdown(wait=wait)

    def add_cron_job(
        self,
        func: Callable[..., Any],
        cron_expression: str,
        job_id: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        max_retries: int = 3,
        retry_delay: int = 60,
        **trigger_kwargs: Any,
    ) -> str:
        """Add a cron-based recurring job."""
        trigger = CronTrigger.from_crontab(cron_expression, timezone=self._timezone)
        return self._add_job(
            func, trigger, job_id, args, kwargs or {}, max_retries, retry_delay, **trigger_kwargs
        )

    def add_interval_job(
        self,
        func: Callable[..., Any],
        interval: timedelta | int,  # seconds or timedelta
        job_id: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        max_retries: int = 3,
        retry_delay: int = 60,
        **trigger_kwargs: Any,
    ) -> str:
        """Add an interval-based recurring job."""
        if isinstance(interval, timedelta):
            seconds = int(interval.total_seconds())
        else:
            seconds = interval
        trigger = IntervalTrigger(seconds=seconds, timezone=self._timezone)
        return self._add_job(
            func, trigger, job_id, args, kwargs or {}, max_retries, retry_delay, **trigger_kwargs
        )

    def _add_job(
        self,
        func: Callable[..., Any],
        trigger: CronTrigger | IntervalTrigger,
        job_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        max_retries: int,
        retry_delay: int,
        **trigger_kwargs: Any,
    ) -> str:
        if job_id in [job.id for job in self._scheduler.get_jobs()]:
            raise ValueError(f"Job {job_id} already exists")

        # Wrap function with retry logic
        async def wrapped_func(*a: Any, **kw: Any) -> Any:
            for attempt in range(max_retries + 1):
                try:
                    if asyncio.iscoroutinefunction(func):
                        return await func(*a, **kw)
                    else:
                        return func(*a, **kw)
                except Exception:
                    if attempt < max_retries:
                        await asyncio.sleep(retry_delay * (2**attempt))  # Exponential backoff
                    else:
                        raise

        job = self._scheduler.add_job(
            wrapped_func,
            trigger=trigger,
            id=job_id,
            args=args,
            kwargs=kwargs,
            **trigger_kwargs,
        )
        return str(job.id)

    def remove_job(self, job_id: str) -> bool:
        try:
            self._scheduler.remove_job(job_id)
            return True
        except Exception:
            return False

    def pause_job(self, job_id: str) -> bool:
        try:
            self._scheduler.pause_job(job_id)
            return True
        except Exception:
            return False

    def resume_job(self, job_id: str) -> bool:
        try:
            self._scheduler.resume_job(job_id)
            return True
        except Exception:
            return False

    def get_job(self, job_id: str) -> Any | None:
        return self._scheduler.get_job(job_id)

    def get_jobs(self) -> list[Any]:
        return list(self._scheduler.get_jobs())


# ============================================================================
# Recurrence Arithmetic (Cron + RRULE unified)
# ============================================================================


@dataclass(frozen=True, slots=True)
class RecurrenceOccurrence:
    occurrence_at: str  # ISO 8601 UTC
    next_scheduled_at: str | None = None


def next_occurrence(
    rule: Cron | RecurrenceRule,
    after: int | datetime,
    time_zone: str | None = None,
) -> str | None:
    """Get next occurrence after a given time."""
    if isinstance(rule, Cron):
        if isinstance(after, datetime):
            after_ms = int(after.timestamp() * 1000)
        else:
            after_ms = after
        return rule.next(after_ms)

    if isinstance(rule, RecurrenceRule):
        if isinstance(after, int):
            dt = datetime.fromtimestamp(after / 1000.0, tz=UTC)
        else:
            dt = after
        next_occurrences = rule.next(dt, count=1)
        if not next_occurrences:
            return None
        return next_occurrences[0].isoformat().replace("+00:00", "Z")

    raise TypeError("Unknown rule type")


def resolve_occurrence(
    rule: Cron | RecurrenceRule,
    scheduled_at: str,
    accepted_at: int,
    time_zone: str | None = None,
) -> dict[str, str | None]:
    """Resolve latest due occurrence and next occurrence."""
    if isinstance(rule, Cron):
        result = rule.resolve(scheduled_at, accepted_at)
        return {
            "occurrence_at": result["occurrence_at"],
            "next_scheduled_at": result.get("next_scheduled_at"),
        }

    if isinstance(rule, RecurrenceRule):
        # For RRULE, we'd need to implement similar logic
        # For now, return basic structure
        return {"occurrence_at": scheduled_at}

    raise TypeError("Unknown rule type")


# ============================================================================
# Time Zone Utilities
# ============================================================================


def get_system_timezone() -> str:
    """Get the system's local timezone name."""
    try:
        return str(datetime.now().astimezone().tzinfo)
    except Exception:
        return "UTC"


def is_valid_timezone(tz_name: str) -> bool:
    """Check if a timezone name is valid."""
    try:
        ZoneInfo(tz_name)
        return True
    except Exception:
        return False


def get_timezone_offset(time_zone: str, at: datetime | None = None) -> timedelta:
    """Get the UTC offset for a timezone at a specific time."""
    if at is None:
        at = datetime.now(UTC)
    tz = ZoneInfo(time_zone)
    return at.astimezone(tz).utcoffset() or timedelta(0)


def convert_timezone(dt: datetime, from_tz: str, to_tz: str) -> datetime:
    """Convert a datetime from one timezone to another."""
    from_zone = ZoneInfo(from_tz)
    to_zone = ZoneInfo(to_tz)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=from_zone)
    else:
        dt = dt.astimezone(from_zone)
    return dt.astimezone(to_zone)


def now_in_timezone(time_zone: str) -> datetime:
    """Get current time in a specific timezone."""
    return datetime.now(ZoneInfo(time_zone))


def parse_iso_instant(value: str) -> int | None:
    """Parse an ISO 8601 instant string to epoch milliseconds."""
    try:
        dt = dateutil.parser.isoparse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def format_iso_instant(epoch_ms: int) -> str:
    """Format epoch milliseconds as ISO 8601 UTC string."""
    return format_four_digit_year_instant(epoch_ms) or datetime.fromtimestamp(
        epoch_ms / 1000.0, tz=UTC
    ).isoformat().replace("+00:00", "Z")


# ============================================================================
# Exports
# ============================================================================


def is_valid_cron_expression(expression: str) -> bool:
    """Quick validation of a cron expression."""
    try:
        parse_cron_expression(expression)
        return True
    except CronParseError:
        return False


__all__ = [
    # Types
    "LocalCalendarTime",
    "RangeError",
    "InstantRejection",
    "LocalCalendarRejection",
    "TimeZoneRejection",
    "InstantResult",
    "LocalCalendarResult",
    "TimeZoneResult",
    "Duration",
    "CronFields",
    "CronParseError",
    "Cron",
    "RecurrenceRule",
    "RecurrenceOccurrence",
    "ScheduleManager",
    # Constants
    "MIN_FOUR_DIGIT_YEAR_MS",
    "MAX_FOUR_DIGIT_YEAR_MS",
    "CRON_SEARCH_HORIZON_YEARS",
    # Parsing
    "parse_local_calendar",
    "parse_offset_instant",
    "four_digit_year_epoch",
    "is_four_digit_year_instant",
    "format_four_digit_year_instant",
    "canonicalize_time_zone",
    "resolve_local_instant",
    # Duration
    "duration_from_ms",
    "duration_from_seconds",
    "duration_from_minutes",
    "duration_from_hours",
    # Cron
    "parse_cron_expression",
    "is_valid_cron_expression",
    "CronParseError",
    # RRULE
    "RecurrenceRule",
    # Scheduler
    "ScheduleManager",
    # Recurrence arithmetic
    "next_occurrence",
    "resolve_occurrence",
    # Time zone utilities
    "get_system_timezone",
    "is_valid_timezone",
    "get_timezone_offset",
    "convert_timezone",
    "now_in_timezone",
    "parse_iso_instant",
    "format_iso_instant",
]
