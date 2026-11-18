"""Pure publication computation.

``compute_day`` is deliberately free of I/O and locks: given one consistent
snapshot of a probe's readings and calibration certificates for a UTC day it
returns the calibrated segments above 8 C and the minutes that have readings
but no covering certificate. The service layer calls it inside a single
write transaction, so one published version always derives from one committed
state of readings and certificates.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import namedtuple

from .timeutil import day_bounds, day_to_iso, minute_to_iso

THRESHOLD_C = 8.0

Calibration = namedtuple("Calibration", ["valid_from", "valid_to", "offset_c"])
Reading = namedtuple("Reading", ["minute", "temperature_c"])


class ComputeError(RuntimeError):
    """Raised when a day cannot be computed (the publish tx rolls back)."""


def _round3(value: float) -> float:
    return round(float(value), 3)


def find_offset(calibrations: list[Calibration], minute: int) -> float | None:
    """Return the offset covering *minute* or ``None`` (certificate gap).

    Intervals are half-open ``[valid_from, valid_to)`` and non-overlapping.
    """
    starts = [c.valid_from for c in calibrations]
    idx = bisect_right(starts, minute) - 1
    if idx < 0:
        return None
    cal = calibrations[idx]
    if cal.valid_from <= minute < cal.valid_to:
        return float(cal.offset_c)
    return None


def compute_day(
    probe_id: str,
    day: int,
    readings: list[Reading],
    calibrations: list[Calibration],
) -> dict:
    """Compute the publication payload for one probe / UTC day.

    Rules implemented:

    * calibrated value = raw reading + certificate offset;
    * a minute without a reading simply does not exist; a minute with a
      reading but without a covering certificate is listed as uncalibrated;
    * consecutive (difference == 1 minute) calibrated minutes strictly above
      8 C form one segment; a missing minute or an uncalibrated minute breaks
      the run (half-open ``[start, end)`` in UTC minutes).
    """
    day_start, day_end = day_bounds(day)

    # Defensive snapshot sanity checks; service layer normally prevents these.
    in_day = [r for r in readings if day_start <= r.minute < day_end]
    if any(r.minute < day_start or r.minute >= day_end for r in readings):
        raise ComputeError("reading outside the requested day reached compute_day")
    for cal in calibrations:
        if cal.valid_from >= cal.valid_to:
            raise ComputeError("certificate interval must be half-open and non-empty")

    # Non-overlap is enforced on writes, but re-check the snapshot to keep the
    # offset lookup unambiguous.
    ordered = sorted(calibrations, key=lambda c: c.valid_from)
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.valid_from < prev.valid_to:
            raise ComputeError(
                f"overlapping calibration certificates for probe {probe_id}"
            )

    by_minute = {r.minute: float(r.temperature_c) for r in in_day}
    minutes = sorted(by_minute)

    uncalibrated: list[int] = []
    calibrated: list[tuple[int, float]] = []
    for minute in minutes:
        offset = find_offset(ordered, minute)
        if offset is None:
            uncalibrated.append(minute)
        else:
            calibrated.append((minute, by_minute[minute] + offset))

    segments: list[dict] = []
    run: list[tuple[int, float]] = []

    def flush() -> None:
        if not run:
            return
        start_minute = run[0][0]
        end_minute = run[-1][0] + 1  # half-open
        peak = max(v for _, v in run)
        segments.append(
            {
                "start_minute": start_minute,
                "end_minute": end_minute,
                "start_utc": minute_to_iso(start_minute),
                "end_utc": minute_to_iso(end_minute),
                "duration_minutes": end_minute - start_minute,
                "max_calibrated_c": _round3(peak),
            }
        )

    # Explicit state machine over calibrated minutes:
    # a run continues only while consecutive minutes stay strictly > 8 C;
    # any non-hot or non-contiguous minute closes the current run.
    prev_minute: int | None = None
    for minute, value in calibrated:
        above = value > THRESHOLD_C
        contiguous = prev_minute is not None and minute == prev_minute + 1
        if above and contiguous:
            run.append((minute, value))
        else:
            flush()
            run = [(minute, value)] if above else []
        prev_minute = minute
    flush()

    return {
        "probe_id": probe_id,
        "day": day,
        "day_utc": day_to_iso(day),
        "threshold_c": THRESHOLD_C,
        "total_reading_minutes": len(minutes),
        "calibrated_minutes": len(calibrated),
        "uncalibrated_minutes": uncalibrated,
        "uncalibrated_minutes_utc": [minute_to_iso(m) for m in uncalibrated],
        "segments": segments,
    }
