#!/usr/bin/env python
"""Reconcile two asset layers on a shared ID and refuse every match the geometry disproves.

Refuses the ID itself when it is not unique, and refuses a status column that
holds one value on every row.

Two departments track the same assets and agree to key on one number. The join
on that number succeeds, most rows match, and a recycled number links a live
asset in one district to a retired record in another. Nothing reports an error,
because the join did what it was told. The separation between the two matched
points is the only evidence that the pair is wrong, and no join computes it.

The obvious tools already do most of this. A pandas merge with indicator=True,
arcpy.AddJoin_management or a LEFT JOIN all match the rows correctly and name
the side each came from. The author's own nalmatch does the same for two tables
and refuses an identifier rule that would merge two records into one key. His
geocodesift finds records stacked on one coordinate and gates a batch on the
result. None of the five measures the distance between the two rows a join just
paired, so none of them can say the match is wrong. That is the gap: the join
succeeded and the geometry disproves it.

    python idreuse.py --self-test
    python idreuse.py fire.csv utilities.csv --id-field HYDRANT_NO
    python idreuse.py fire.csv utilities.csv --confirm-distance 100 --reuse-distance 800
    python idreuse.py fire.csv utilities.csv --status-field OPERABLE
    python idreuse.py fire.csv utilities.csv --out classified.csv --apply

Nothing is written without --apply.

Exit codes: 0 no match was disproved, 1 at least one match was disproved, 2 a
file could not be read or written, 3 the anchor was refused, 64 usage error.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import io
import math
import os
import shutil
import sys
import tempfile

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Separation at or under which a matched pair is CONFIRMED, in the linear units
# of the input CRS. 150 is set from one measured reconciliation of two hydrant
# layers, where roughly four fifths of all ID-matched pairs fell inside it. It
# is a survey-and-capture tolerance, not a statement about any asset class, so
# measure your own pair before trusting it.
DEFAULT_CONFIRM_DISTANCE = 150.0

# Separation above which a matched pair is REUSED: too far apart to be one
# object however the two crews captured it. Between this and the confirm
# distance the pair is DRIFT, which is neither confirmed nor rejected and is
# the band a human has to look at.
DEFAULT_REUSE_DISTANCE = 500.0

# Printed after every distance. The tool never converts anything; this only
# labels the units the coordinates already arrived in.
DEFAULT_UNITS = "ft"

# How many records are listed under each class. The count is the finding; the
# sample is there so somebody can open one record and agree.
DEFAULT_SAMPLE = 10

# Columns read from both sides when no flag names others.
DEFAULT_ID_FIELD = "ASSET_ID"
DEFAULT_X_FIELD = "X"
DEFAULT_Y_FIELD = "Y"

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# The five classes every input row lands in, exactly one each.
CONFIRMED = "CONFIRMED"
DRIFT = "DRIFT"
REUSED = "REUSED"
UNMATCHED = "UNMATCHED"
UNVERIFIED = "UNVERIFIED"

CLASSES = (CONFIRMED, DRIFT, REUSED, UNMATCHED, UNVERIFIED)

# The columns --out writes. This is a new file, not a copy of either input,
# because there are two inputs and one of them has no row for half the keys.
OUT_COLUMNS = ("side", "csv_line", "key", "class", "distance", "x", "y",
               "reason")


class Row(object):
    """One input row, reduced to the four values this tool reasons about."""

    def __init__(self, index, side, key, x, y, status=None):
        self.index = index
        self.side = side
        self.key = key
        self.x = x
        self.y = y
        self.status = status
        self.verdict = None
        self.reason = ""

    @property
    def line(self):
        """The line this row is on in its CSV, which is what the operator has
        open. Index 0 is the first data row, and line 1 is the header."""
        return self.index + 2

    def __repr__(self):
        return "Row(%s, %d, %r, %s)" % (self.side, self.line, self.key,
                                        self.verdict)


class Pair(object):
    """Two rows a shared key brought together, and what the geometry says."""

    def __init__(self, key, left, right, distance, verdict, reason, note=None):
        self.key = key
        self.left = left
        self.right = right
        self.distance = distance
        self.verdict = verdict
        self.reason = reason
        self.note = note

    def __repr__(self):
        return "Pair(%r, %s, %s)" % (self.key, self.verdict, self.distance)


class Repeat(object):
    """One anchor value that more than one row on one side claims."""

    def __init__(self, side, key, lines):
        self.side = side
        self.key = key
        self.lines = list(lines)

    def __repr__(self):
        return "Repeat(%s, %r, %d)" % (self.side, self.key, len(self.lines))


class Report(object):
    """What the two layers prove about each other, or why nothing was checked."""

    def __init__(self, left, right, pairs, repeats, status_note, units,
                 confirm, reuse):
        self.left = left
        self.right = right
        self.pairs = pairs
        self.repeats = repeats
        self.status_note = status_note
        self.units = units
        self.confirm = confirm
        self.reuse = reuse
        self.counts = dict((name, 0) for name in CLASSES)
        for row in list(left) + list(right):
            if row.verdict is not None:
                self.counts[row.verdict] += 1

    @property
    def total(self):
        """Every input row from both sides."""
        return len(self.left) + len(self.right)

    @property
    def refused(self):
        """An anchor that repeats is refused, and nothing else was computed."""
        return bool(self.repeats)

    @property
    def classified(self):
        """Rows that reached a class. Equals total unless the run was refused."""
        return sum(self.counts.values())

    @property
    def disproved(self):
        """Pairs the geometry disproved. This is what the exit code reads."""
        return [p for p in self.pairs if p.verdict == REUSED]

    def __repr__(self):
        if self.refused:
            return "Report(REFUSED, %d repeat(s))" % len(self.repeats)
        return "Report(%d row(s), %d disproved)" % (self.total,
                                                    len(self.disproved))


# ----------------------------------------------------------------- pure core

def _is_finite(value):
    """True for a real number. NaN and infinity are not numbers this can read.

    NaN is the dangerous one. Every comparison against NaN is False, so a NaN
    separation clears neither threshold and falls out of the ladder as REUSED:
    the tool would invent a recycled ID out of an empty cell. Anything that is
    not a number reads as unusable rather than raising, because a CSV cell
    holds anything.
    """
    try:
        return not (math.isnan(value) or math.isinf(value))
    except TypeError:
        return False


def to_number(text):
    """A float from a CSV cell, or None when the cell is blank or not a number.

    "nan", "inf" and an overflowing literal such as 1e400 are cells, not
    coordinates. float() accepts all three. They read as absent instead, which
    is honest and already has a class of its own.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        value = float(text)
        return value if _is_finite(value) else None
    text = text.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if _is_finite(value) else None


def separation(ax, ay, bx, by):
    """Straight-line distance between two points, or None when one is unusable.

    Both layers are assumed to be in one projected CRS, so this is Pythagoras
    and no geometry library is needed. Feeding it degrees produces a number
    with no meaning, which is why --units labels every distance the report
    prints and why the README says to project first.
    """
    for value in (ax, ay, bx, by):
        if not _is_finite(value):
            return None
    distance = math.hypot(float(ax) - float(bx), float(ay) - float(by))
    # Two finite coordinates can still overflow: hypot(1e308, -1e308) is inf.
    # An infinite separation is not a measurement, and letting it out would
    # reach classify_separation, raise, and exit 64 as though the operator had
    # named a column wrong. It is an unusable coordinate, which has a class.
    return distance if _is_finite(distance) else None


def classify_separation(distance, confirm=DEFAULT_CONFIRM_DISTANCE,
                        reuse=DEFAULT_REUSE_DISTANCE, units=DEFAULT_UNITS):
    """(class, reason) for one matched pair, from its separation alone.

    Three outcomes, not two. A pair between the two thresholds is DRIFT: the
    geometry neither confirms it nor disproves it, and a tool that forced that
    band into one of the other two would be guessing on the operator's behalf.
    """
    if confirm < 0:
        raise ValueError("the confirm distance cannot be negative, got %r"
                         % (confirm,))
    if reuse < confirm:
        raise ValueError("the reuse distance %r is below the confirm distance "
                         "%r" % (reuse, confirm))
    if not _is_finite(confirm) or not _is_finite(reuse):
        raise ValueError("the distance thresholds must be real numbers")
    if distance is None:
        return UNVERIFIED, ("no usable coordinate on one side or both, so the "
                            "geometry proves nothing either way")
    if not _is_finite(distance):
        raise ValueError("the separation must be a real number, got %r"
                         % (distance,))
    if distance < 0:
        raise ValueError("the separation cannot be negative, got %r"
                         % (distance,))
    if distance <= confirm:
        return CONFIRMED, ("%.1f %s apart, inside the confirm distance of "
                           "%.1f" % (distance, units, confirm))
    if distance <= reuse:
        return DRIFT, ("%.1f %s apart, between the confirm distance of %.1f "
                       "and the reuse distance of %.1f"
                       % (distance, units, confirm, reuse))
    return REUSED, ("%.1f %s apart, past the reuse distance of %.1f. One key, "
                    "two objects." % (distance, units, reuse))


def repeated_anchors(rows, side):
    """Every anchor value more than one row on this side claims, in file order.

    Blank anchors are left out. A row with no anchor is a hole in the data, not
    a key that repeats, and pooling the two would refuse a run over the one
    thing an anchor check cannot repair.
    """
    order = []
    lines = {}
    for row in rows:
        if not row.key:
            continue
        if row.key not in lines:
            lines[row.key] = []
            order.append(row.key)
        lines[row.key].append(row.line)
    return [Repeat(side, key, lines[key]) for key in order
            if len(lines[key]) > 1]


def status_signal(values, column):
    """(usable, message) for a column offered as a live-or-retired signal.

    This is the check the disaster turns on. A crew filters on an operable flag
    before the join, the flag is set on every row in both layers, the filter
    removes nothing, and the run looks defended. A column with one distinct
    value cannot separate anything, so it is refused as a signal and named.
    Nothing is ever filtered on it here either way.
    """
    seen = []
    for value in values:
        text = "" if value is None else str(value).strip()
        if text not in seen:
            seen.append(text)
    if not seen:
        return False, ("status column %r has no rows to read, so it carries no "
                       "signal" % (column,))
    if len(seen) == 1:
        return False, ("status column %r holds the single value %r on all %d "
                       "row(s). It cannot separate a live record from a "
                       "retired one, so it is refused as a status signal and "
                       "nothing is filtered on it."
                       % (column, seen[0], len(values)))
    return True, ("status column %r holds %d distinct value(s), so a pair "
                  "whose two sides disagree is reported"
                  % (column, len(seen)))


def status_disagreement(left, right, column):
    """Message when two paired rows disagree on the status column, else None."""
    if left.status is None or right.status is None:
        return None
    if left.status == right.status:
        return None
    return ("the two sides disagree on %s: %r on the left, %r on the right"
            % (column, left.status, right.status))


def reconcile(left, right, confirm=DEFAULT_CONFIRM_DISTANCE,
              reuse=DEFAULT_REUSE_DISTANCE, units=DEFAULT_UNITS,
              status_column=None):
    """Classify every row of both sides. No file, no network.

    The anchor is checked for repeats first and the run stops there if it
    repeats, because a key claimed by two rows cannot say which of them the
    other layer meant. Reporting a classification under such a key would be
    publishing an answer this tool already knows is unsound.
    """
    repeats = repeated_anchors(left, "left") + repeated_anchors(right, "right")
    if repeats:
        return Report(left, right, [], repeats, None, units, confirm, reuse)

    # classify_separation validates the thresholds, and a run with no pair at
    # all would never reach it. Call it once up front so a bad threshold is
    # refused whatever the data holds.
    classify_separation(0.0, confirm, reuse, units)

    status_note = None
    if status_column:
        usable, message = status_signal(
            [r.status for r in list(left) + list(right)], status_column)
        status_note = message
        if not usable:
            # Refused as a signal. The rows keep their values; nothing reads
            # them again.
            status_column = None

    right_by_key = dict((r.key, r) for r in right if r.key)

    pairs = []
    for row in left:
        if not row.key:
            row.verdict = UNVERIFIED
            row.reason = "no anchor value on this row, so it cannot be matched"
            continue
        other = right_by_key.get(row.key)
        if other is None:
            row.verdict = UNMATCHED
            row.reason = "no row on the right side carries this key"
            continue
        distance = separation(row.x, row.y, other.x, other.y)
        verdict, reason = classify_separation(distance, confirm, reuse, units)
        note = None
        if status_column:
            note = status_disagreement(row, other, status_column)
        row.verdict = other.verdict = verdict
        row.reason = other.reason = reason
        pairs.append(Pair(row.key, row, other, distance, verdict, reason, note))

    # Whatever the loop above did not reach. A right row whose key is on the
    # left already has its verdict from the pair, because the anchor is unique
    # on both sides by the time this runs, so anything still unclassified here
    # has either no key or no counterpart.
    for row in right:
        if row.verdict is not None:
            continue
        if not row.key:
            row.verdict = UNVERIFIED
            row.reason = "no anchor value on this row, so it cannot be matched"
        else:
            row.verdict = UNMATCHED
            row.reason = "no row on the left side carries this key"

    return Report(left, right, pairs, [], status_note, units, confirm, reuse)


def gate(report):
    """Exit code for a report, which is the whole point of running this."""
    if report.refused:
        return 3
    return 1 if report.disproved else 0


def describe(report, sample=DEFAULT_SAMPLE):
    """Render a report as the lines the CLI prints."""
    if sample < 0:
        raise ValueError("sample cannot be negative")
    if report.refused:
        out = ["REFUSED   the anchor is not unique, so nothing was compared"]
        for rep in report.repeats[:sample]:
            out.append("      %s key %r is on %d rows: line %s"
                       % (rep.side, rep.key, len(rep.lines),
                          ", ".join(str(n) for n in rep.lines)))
        if len(report.repeats) > sample:
            out.append("      ...and %d more" % (len(report.repeats) - sample))
        out.append("      Fix the anchor or name a different column. A key two "
                   "rows claim cannot say which row the other layer meant.")
        out.append("")
        out.append("VERDICT: REFUSED")
        return out

    out = []
    if report.status_note:
        out.append("STATUS    %s" % report.status_note)
    out.append("rows classified: %d of %d" % (report.classified, report.total))
    for name in CLASSES:
        out.append("  %-11s %6d" % (name, report.counts[name]))
    out.append("pairs: %d on a shared key, at confirm %.1f %s and reuse "
               "%.1f %s" % (len(report.pairs), report.confirm, report.units,
                            report.reuse, report.units))

    worst = sorted([p for p in report.pairs if p.distance is not None],
                   key=lambda p: (-p.distance, p.key))
    disproved = [p for p in worst if p.verdict == REUSED]
    if disproved:
        out.append("")
        out.append("matches the geometry disproves, worst first:")
        for pair in disproved[:sample]:
            out.append("  %s  %.1f %s apart, left line %d, right line %d"
                       % (pair.key, pair.distance, report.units,
                          pair.left.line, pair.right.line))
        if len(disproved) > sample:
            out.append("  ...and %d more" % (len(disproved) - sample))

    drifting = [p for p in worst if p.verdict == DRIFT]
    if drifting:
        out.append("")
        out.append("matches in the drift band, for a human to look at:")
        for pair in drifting[:sample]:
            out.append("  %s  %.1f %s apart, left line %d, right line %d"
                       % (pair.key, pair.distance, report.units,
                          pair.left.line, pair.right.line))
        if len(drifting) > sample:
            out.append("  ...and %d more" % (len(drifting) - sample))

    notes = [p for p in report.pairs if p.note]
    if notes:
        out.append("")
        out.append("pairs whose two sides disagree on the status column:")
        for pair in notes[:sample]:
            out.append("  %s  %s" % (pair.key, pair.note))
        if len(notes) > sample:
            out.append("  ...and %d more" % (len(notes) - sample))

    out.append("")
    if report.disproved:
        out.append("VERDICT: %d match(es) disproved by the geometry. Do not "
                   "publish this join." % len(report.disproved))
    else:
        out.append("VERDICT: no match was disproved by the geometry.")
    return out


def classified_rows(report):
    """Every input row as an --out record, left side first, in file order."""
    rows = []
    by_row = {}
    for pair in report.pairs:
        by_row[id(pair.left)] = pair.distance
        by_row[id(pair.right)] = pair.distance
    for row in list(report.left) + list(report.right):
        distance = by_row.get(id(row))
        rows.append({
            "side": row.side,
            "csv_line": row.line,
            "key": row.key,
            "class": row.verdict or "",
            "distance": "" if distance is None else "%.3f" % distance,
            "x": "" if row.x is None else repr(row.x),
            "y": "" if row.y is None else repr(row.y),
            "reason": row.reason,
        })
    return rows


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the decision core. No file, no network, no credentials."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def row(side, index, key, x, y, status=None):
        return Row(index, side, key, x, y, status)

    def verdict(distance, confirm=DEFAULT_CONFIRM_DISTANCE,
                reuse=DEFAULT_REUSE_DISTANCE):
        return classify_separation(distance, confirm, reuse)[0]

    print("idreuse self-test: no file, no network, no credentials")
    print("-" * 68)

    # ---- the three separations the thresholds were set from
    check(verdict(51.0) == CONFIRMED,
          "a pair 51 units apart is CONFIRMED, which is the ordinary case")
    check(verdict(300.0) == DRIFT,
          "a pair 300 units apart is DRIFT, neither confirmed nor rejected")
    check(verdict(248940.0) == REUSED,
          "a pair 248940 units apart is REUSED, never a match"
          "  <-- pinned defect")
    check(classify_separation(248940.0)[1].endswith("One key, two objects."),
          "the refusal says in words that one key covers two objects")

    # ---- the thresholds at their exact boundary
    check(verdict(0.0) == CONFIRMED, "two rows on the same point are CONFIRMED")
    check(verdict(150.0) == CONFIRMED,
          "exactly the confirm distance is CONFIRMED, the threshold is "
          "inclusive")
    check(verdict(150.001) == DRIFT,
          "a thousandth past the confirm distance is DRIFT")
    check(verdict(500.0) == DRIFT,
          "exactly the reuse distance is DRIFT, the threshold is inclusive")
    check(verdict(500.001) == REUSED,
          "a thousandth past the reuse distance is REUSED")
    check(verdict(200.0, 100.0, 150.0) == REUSED,
          "tighter thresholds move the same pair into REUSED")
    check(verdict(200.0, 300.0, 900.0) == CONFIRMED,
          "looser thresholds move it back into CONFIRMED")
    check(verdict(0.0, 0.0, 0.0) == CONFIRMED,
          "two thresholds of zero are legal and only an exact point confirms")
    check(verdict(0.1, 0.0, 0.0) == REUSED,
          "and a tenth of a unit off then disproves the match")
    check(verdict(300.0, 200.0, 200.0) == REUSED,
          "two equal thresholds are legal, the drift band is then empty")
    check(verdict(200.0, 200.0, 200.0) == CONFIRMED,
          "and exactly that distance still confirms")

    # ---- what the classifier refuses to answer at all
    check(classify_separation(None)[0] == UNVERIFIED,
          "a pair with no separation is UNVERIFIED  <-- pinned defect")
    check("proves nothing" in classify_separation(None)[1],
          "and says the geometry proves nothing, rather than implying a match")
    raises(lambda: classify_separation(float("nan")),
           "a NaN separation raises instead of falling out as REUSED"
           "  <-- pinned defect")
    raises(lambda: classify_separation(float("inf")),
           "an infinite separation raises")
    raises(lambda: classify_separation(-1.0), "a negative separation raises")
    raises(lambda: classify_separation("100"),
           "a separation that is still a string raises")
    raises(lambda: classify_separation(100.0, -1.0),
           "a negative confirm distance raises")
    raises(lambda: classify_separation(100.0, 500.0, 150.0),
           "a reuse distance under the confirm distance raises")
    raises(lambda: classify_separation(100.0, float("nan")),
           "a NaN confirm distance raises")
    raises(lambda: classify_separation(100.0, 0.0, float("inf")),
           "an infinite reuse distance raises")
    check(classify_separation(51.0, units="m")[1].endswith(
              "51.0 m apart, inside the confirm distance of 150.0"),
          "the units label is printed with the distance, never converted")

    # ---- the separation itself
    check(separation(0.0, 0.0, 3.0, 4.0) == 5.0,
          "a 3-4-5 triangle measures 5 units")
    check(separation(1000.0, 2000.0, 1000.0, 2000.0) == 0.0,
          "a point measured against itself is zero apart")
    check(separation(-3.0, -4.0, 0.0, 0.0) == 5.0,
          "negative coordinates measure the same distance")
    check(separation(0.0, 0.0, 0.0, None) is None,
          "a missing coordinate yields no separation  <-- pinned defect")
    check(separation(None, None, None, None) is None,
          "two missing points yield no separation")
    check(separation(0.0, 0.0, float("nan"), 0.0) is None,
          "a NaN coordinate yields no separation, not a NaN distance"
          "  <-- pinned defect")
    check(separation(0.0, 0.0, float("inf"), 0.0) is None,
          "an infinite coordinate yields no separation")
    check(separation(0.0, 0.0, "3.0", "4.0") is None,
          "a coordinate left as text yields no separation, not a crash")
    check(separation(0, 0, 3, 4) == 5.0,
          "integer coordinates are read as numbers")
    big = separation(0.0, 0.0, 1e150, 1e150)
    check(_is_finite(big) and big > 1e150,
          "two very distant points still measure a finite distance")

    # ---- the cells a coordinate column really holds
    check(to_number("1234.5") == 1234.5, "a coordinate cell is read as a float")
    check(to_number("  1234.5  ") == 1234.5, "a padded cell is still read")
    check(to_number("") is None, "a blank cell reads None, not zero")
    check(to_number(None) is None, "a missing cell reads None")
    check(to_number("n/a") is None, "a cell that is not a number reads None")
    check(to_number("nan") is None,
          "a cell reading nan is absent, not a number  <-- pinned defect")
    check(to_number("inf") is None, "a cell reading inf is absent")
    check(to_number("-1e400") is None, "a cell that overflows is absent")
    check(to_number("1e308") == 1e308, "a huge but finite cell is still read")
    check(to_number(1234.5) == 1234.5, "a cell that is already a float is read")
    check(to_number(1234) == 1234.0, "a cell that is already an int is read")
    check(to_number(float("nan")) is None,
          "a NaN that is already a float is still absent")
    check(_is_finite(1.0) is True and _is_finite("1.0") is False,
          "a string is not a finite number, however it reads")

    # ---- the anchor, which is checked before anything else
    ok_rows = [row("left", 0, "H-1", 0.0, 0.0), row("left", 1, "H-2", 1.0, 1.0)]
    check(repeated_anchors(ok_rows, "left") == [],
          "a unique anchor reports no repeat")
    dupe_rows = [row("left", 0, "H-1", 0.0, 0.0),
                 row("left", 1, "H-2", 1.0, 1.0),
                 row("left", 2, "H-1", 9.0, 9.0)]
    reps = repeated_anchors(dupe_rows, "left")
    check(len(reps) == 1 and reps[0].key == "H-1",
          "an anchor on two rows is reported as a repeat  <-- pinned defect")
    check(reps[0].lines == [2, 4],
          "and both CSV lines are named, because either could be the right row")
    check(reps[0].side == "left", "the repeat names the side it is on")
    check(repr(reps[0]) == "Repeat(left, 'H-1', 2)",
          "a repeat prints its side, key and count for debugging")
    blanks = [row("left", 0, "", 0.0, 0.0), row("left", 1, "", 1.0, 1.0)]
    check(repeated_anchors(blanks, "left") == [],
          "two blank anchors are not a repeated key  <-- pinned defect")
    triple = repeated_anchors([row("left", i, "H-1", 0.0, 0.0)
                               for i in range(3)], "left")
    check(triple[0].lines == [2, 3, 4],
          "an anchor on three rows names all three lines")
    two_keys = repeated_anchors([row("left", 0, "B", 0.0, 0.0),
                                 row("left", 1, "A", 0.0, 0.0),
                                 row("left", 2, "B", 0.0, 0.0),
                                 row("left", 3, "A", 0.0, 0.0)], "left")
    check([r.key for r in two_keys] == ["B", "A"],
          "repeats are reported in the order the file first shows them")

    # ---- the status column, which is the defence that did nothing
    usable, message = status_signal(["1", "1", "1", "1"], "OPERABLE")
    check(usable is False,
          "a status column holding one value on every row is refused as a "
          "signal  <-- pinned defect")
    check("'OPERABLE'" in message and "'1'" in message,
          "and the refusal names the column and the single value it holds"
          "  <-- pinned defect")
    check("nothing is filtered on it" in message,
          "and says plainly that nothing was filtered")
    check(status_signal(["1", "1", "0"], "OPERABLE")[0] is True,
          "a column with two values carries a signal")
    check(status_signal([" 1 ", "1"], "OPERABLE")[0] is False,
          "a padded value is the same value, so the column is still refused")
    check(status_signal(["1", None], "OPERABLE")[0] is True,
          "a blank on one row is a second value, so the column carries signal")
    check(status_signal([], "OPERABLE")[0] is False,
          "a column with no rows carries no signal")
    check("no rows to read" in status_signal([], "OPERABLE")[1],
          "and says so rather than naming a value it never saw")
    check("3 distinct" in status_signal(["A", "B", "C"], "STATUS")[1],
          "a usable column reports how many values it holds")
    check(status_disagreement(row("left", 0, "H-1", 0.0, 0.0, "1"),
                              row("right", 0, "H-1", 0.0, 0.0, "1"),
                              "OPERABLE") is None,
          "two sides that agree on the status raise no note")
    check(status_disagreement(row("left", 0, "H-1", 0.0, 0.0, "1"),
                              row("right", 0, "H-1", 0.0, 0.0, "0"),
                              "OPERABLE") is not None,
          "two sides that disagree on the status raise a note")
    check("'1' on the left" in status_disagreement(
              row("left", 0, "H-1", 0.0, 0.0, "1"),
              row("right", 0, "H-1", 0.0, 0.0, "0"), "OPERABLE"),
          "and the note names both values and the side each is on")
    check(status_disagreement(row("left", 0, "H-1", 0.0, 0.0, None),
                              row("right", 0, "H-1", 0.0, 0.0, "0"),
                              "OPERABLE") is None,
          "no status column on one side means no disagreement to report")

    # ---- the reconciliation, end to end over the decision core
    def layers(left_spec, right_spec, **kw):
        left = [row("left", i, k, x, y, s)
                for i, (k, x, y, s) in enumerate(left_spec)]
        right = [row("right", i, k, x, y, s)
                 for i, (k, x, y, s) in enumerate(right_spec)]
        return reconcile(left, right, **kw)

    near = [("H-1", 1000.0, 1000.0, None)]
    same = [("H-1", 1000.0, 1051.0, None)]
    rep = layers(near, same)
    check(rep.counts[CONFIRMED] == 2,
          "a pair 51 units apart classifies BOTH rows CONFIRMED")
    check(len(rep.pairs) == 1 and rep.pairs[0].verdict == CONFIRMED,
          "and is reported as one pair, not as two rows")
    check(rep.classified == rep.total,
          "every input row landed in exactly one class")
    check(gate(rep) == 0, "a confirmed layer pair passes the gate")
    check(rep.disproved == [], "and nothing was disproved")

    far = [("H-1", 1000.0, 249940.0, None)]
    rep = layers(near, far)
    check(rep.counts[REUSED] == 2,
          "a pair 248940 units apart classifies both rows REUSED")
    check(gate(rep) == 1, "and fails the gate  <-- pinned defect")
    check(len(rep.disproved) == 1, "one match was disproved")
    check(rep.disproved[0].key == "H-1", "and the disproved pair names its key")

    mid = [("H-1", 1000.0, 1300.0, None)]
    rep = layers(near, mid)
    check(rep.counts[DRIFT] == 2, "a pair 300 units apart is DRIFT on both rows")
    check(gate(rep) == 0,
          "and DRIFT does not fail the gate: it is not auto-rejected"
          "  <-- pinned defect")

    # ---- the two failure modes that must never pool
    rep = layers([("H-1", 0.0, 0.0, None), ("H-9", 0.0, 0.0, None)],
                 [("H-1", 0.0, 0.0, None), ("H-8", 5000.0, 5000.0, None)])
    check(rep.counts[UNMATCHED] == 2,
          "a key on one side only is UNMATCHED, one row on each side")
    check(rep.counts[REUSED] == 0,
          "and is counted apart from REUSED, so the two never pool"
          "  <-- pinned defect")
    check(rep.counts[CONFIRMED] == 2, "while the shared key still confirms")
    check(rep.classified == rep.total == 4,
          "all four rows landed in exactly one class")
    check(gate(rep) == 0,
          "an unmatched key alone does not fail the gate: half a layer pair "
          "is normal")
    left_only = [r for r in rep.left if r.verdict == UNMATCHED]
    check(left_only[0].reason.endswith("right side carries this key"),
          "an unmatched left row says which side has nothing")
    right_only = [r for r in rep.right if r.verdict == UNMATCHED]
    check(right_only[0].reason.endswith("left side carries this key"),
          "and an unmatched right row says the same in the other direction")

    # ---- a row with no coordinate, and a row with no anchor
    rep = layers([("H-1", None, None, None)], [("H-1", 0.0, 0.0, None)])
    check(rep.counts[UNVERIFIED] == 2,
          "a pair with no coordinate on one side is UNVERIFIED"
          "  <-- pinned defect")
    check(rep.pairs[0].distance is None,
          "and the pair carries no distance at all")
    check(gate(rep) == 0, "an unverified pair does not fail the gate")
    rep = layers([("", 0.0, 0.0, None)], [("H-1", 0.0, 0.0, None)])
    check(rep.counts[UNVERIFIED] == 1 and rep.counts[UNMATCHED] == 1,
          "a row with no anchor is UNVERIFIED and the other side is UNMATCHED")
    check([r for r in rep.left][0].reason.startswith("no anchor value"),
          "and the row with no anchor says that is what it is missing")
    rep = layers([("", 0.0, 0.0, None)], [("", 0.0, 0.0, None)])
    check(rep.counts[UNVERIFIED] == 2,
          "two blank anchors are two unverified rows, never a match on ''"
          "  <-- pinned defect")
    check(rep.pairs == [], "and no pair at all is built from them")

    # ---- the partition, which is the invariant the report rests on
    mixed = layers(
        [("H-1", 0.0, 0.0, None), ("H-2", 0.0, 0.0, None),
         ("H-3", 0.0, 0.0, None), ("H-4", None, None, None),
         ("H-5", 0.0, 0.0, None), ("", 0.0, 0.0, None)],
        [("H-1", 0.0, 51.0, None), ("H-2", 0.0, 300.0, None),
         ("H-3", 0.0, 9000.0, None), ("H-4", 0.0, 0.0, None),
         ("H-6", 0.0, 0.0, None)])
    check(mixed.total == 11, "eleven rows went in")
    check(mixed.classified == 11, "eleven rows came out classified")
    check(sum(mixed.counts.values()) == mixed.total,
          "the classes sum to the input row count  <-- pinned defect")
    check((mixed.counts[CONFIRMED], mixed.counts[DRIFT], mixed.counts[REUSED],
           mixed.counts[UNMATCHED], mixed.counts[UNVERIFIED])
          == (2, 2, 2, 2, 3),
          "and every class holds the rows it should")
    check(len(mixed.pairs) == 4, "four keys were on both sides")
    check(all(r.verdict is not None for r in mixed.left + mixed.right),
          "not one row was left without a class")
    check(repr(mixed).startswith("Report(11 row(s), 1 disproved)"),
          "a report prints its row count and its disproved count")
    check(repr(mixed.left[0]) == "Row(left, 2, 'H-1', CONFIRMED)",
          "a row prints its side, CSV line, key and class for debugging")
    check(repr(mixed.pairs[0]) == "Pair('H-1', CONFIRMED, 51.0)",
          "a pair prints its key, class and separation")

    # ---- an empty side, and two empty sides
    rep = layers([], [])
    check(rep.total == 0 and rep.classified == 0 and rep.pairs == [],
          "two empty layers classify nothing and crash on nothing")
    check(gate(rep) == 0,
          "and pass the gate: there is no match to disprove")
    rep = layers([("H-1", 0.0, 0.0, None)], [])
    check(rep.counts[UNMATCHED] == 1,
          "every row is UNMATCHED when the other side is empty")

    # ---- the anchor refusal stops everything before it starts
    rep = layers([("H-1", 0.0, 0.0, None), ("H-1", 9000.0, 9000.0, None)],
                 [("H-1", 0.0, 0.0, None)])
    check(rep.refused is True,
          "a repeated anchor refuses the run  <-- pinned defect")
    check(rep.pairs == [],
          "and not one pair is built, because the join never ran"
          "  <-- pinned defect")
    check(rep.classified == 0,
          "and not one row is classified under an anchor that repeats")
    check(gate(rep) == 3,
          "a refusal exits 3, which is not the 1 a disproved match exits")
    check(len(rep.repeats) == 1 and rep.repeats[0].lines == [2, 3],
          "the refusal names every row the repeated key is on")
    check(repr(rep) == "Report(REFUSED, 1 repeat(s))",
          "a refused report prints as refused")
    rep = layers([("H-1", 0.0, 0.0, None)],
                 [("H-1", 0.0, 0.0, None), ("H-1", 0.0, 0.0, None)])
    check(rep.refused is True,
          "a repeat on the right side refuses the run just as hard")
    check(rep.repeats[0].side == "right", "and names the side it is on")

    # ---- the status column through a whole reconciliation
    rep = layers([("H-1", 0.0, 0.0, "1"), ("H-2", 0.0, 0.0, "1")],
                 [("H-1", 0.0, 51.0, "1"), ("H-2", 0.0, 9000.0, "1")],
                 status_column="OPERABLE")
    check("refused as a status signal" in rep.status_note,
          "a status column that is 1 on every row is refused, and the run "
          "continues  <-- pinned defect")
    check(rep.counts[REUSED] == 2,
          "and the pair the geometry disproves is still found")
    check(all(p.note is None for p in rep.pairs),
          "no disagreement is reported from a column that holds one value")
    rep = layers([("H-1", 0.0, 0.0, "1"), ("H-2", 0.0, 0.0, "1")],
                 [("H-1", 0.0, 51.0, "0"), ("H-2", 0.0, 9000.0, "1")],
                 status_column="OPERABLE")
    check(rep.status_note.startswith("status column 'OPERABLE' holds 2"),
          "a status column with two values is reported as usable")
    check(len([p for p in rep.pairs if p.note]) == 1,
          "and the one pair whose sides disagree carries a note")
    check(rep.pairs[0].note is not None and rep.pairs[1].note is None,
          "on the pair that disagrees and not on the one that does not")
    rep = layers([("H-1", 0.0, 0.0, None)], [("H-1", 0.0, 51.0, None)])
    check(rep.status_note is None,
          "no status column named means no status line at all")

    # ---- thresholds are validated even when there is no pair to apply them to
    raises(lambda: layers([], [], confirm=-1.0),
           "a negative confirm distance refuses a run with no rows in it"
           "  <-- pinned defect")
    raises(lambda: layers([], [], confirm=500.0, reuse=100.0),
           "so does a reuse distance under the confirm distance")
    check(layers([("H-1", 0.0, 0.0, None)], [("H-1", 0.0, 200.0, None)],
                 confirm=1000.0, reuse=2000.0).counts[CONFIRMED] == 2,
          "wider thresholds confirm a pair the defaults would have drifted")

    # ---- the report the CLI prints
    lines = describe(mixed)
    check(any(l == "rows classified: 11 of 11" for l in lines),
          "the report says how many rows it classified and out of how many")
    check(any(l.startswith("  CONFIRMED") for l in lines),
          "and prints a count for every class")
    check(any("matches the geometry disproves, worst first:" in l
              for l in lines),
          "the disproved matches are listed under their own heading")
    check(any("matches in the drift band" in l for l in lines),
          "and the drift band is listed apart from them")
    check(any(l.startswith("  H-3  9000.0 ft apart, left line 4, right line 4")
              for l in lines),
          "each disproved match names its key, its separation and both lines")
    check(lines[-1].startswith("VERDICT: 1 match(es) disproved"),
          "the verdict counts the matches the geometry disproved")
    clean = describe(layers(near, same))
    check(clean[-1] == "VERDICT: no match was disproved by the geometry.",
          "a clean pair of layers says so in words")
    check(not any("disproves" in l for l in clean),
          "and lists no disproved matches at all")
    check(not any("drift band" in l for l in clean),
          "nor a drift band that is empty")
    ref = describe(layers([("H-1", 0.0, 0.0, None), ("H-1", 1.0, 1.0, None)],
                          [("H-1", 0.0, 0.0, None)]))
    check(ref[0].startswith("REFUSED"), "a refused run prints REFUSED first")
    check(ref[-1] == "VERDICT: REFUSED", "and REFUSED last")
    check(not any("rows classified" in l for l in ref),
          "and prints no count of anything else  <-- pinned defect")
    check(any("cannot say which row the other layer meant" in l for l in ref),
          "and says why the run stopped")
    many_dupes = layers([("H-%d" % i, 0.0, 0.0, None) for i in range(12)]
                        + [("H-%d" % i, 1.0, 1.0, None) for i in range(12)],
                        [("H-0", 0.0, 0.0, None)])
    ref = describe(many_dupes, sample=10)
    check(any(l == "      ...and 2 more" for l in ref),
          "a refusal stops after the sample and counts the rest")
    check(not any(l.startswith("      left key") for l in
                  describe(many_dupes, sample=0)),
          "a sample of 0 lists no repeat at all, only the refusal")
    raises(lambda: describe(mixed, sample=-1), "a negative sample raises")

    wide = layers([("H-%d" % i, 0.0, 0.0, None) for i in range(12)],
                  [("H-%d" % i, 0.0, 9000.0, None) for i in range(12)])
    lines = describe(wide, sample=10)
    check(sum(1 for l in lines if " ft apart, left line " in l) == 10,
          "exactly ten of the twelve disproved matches are listed")
    check(any(l == "  ...and 2 more" for l in lines),
          "and the report counts the ones it did not list")
    drifters = layers([("H-%d" % i, 0.0, 0.0, None) for i in range(12)],
                      [("H-%d" % i, 0.0, 300.0, None) for i in range(12)])
    check(any(l == "  ...and 2 more" for l in describe(drifters, sample=10)),
          "the drift band is sampled the same way")
    exact = layers([("H-%d" % i, 0.0, 0.0, None) for i in range(10)],
                   [("H-%d" % i, 0.0, 9000.0, None) for i in range(10)])
    check(not any(l.startswith("  ...and") for l in describe(exact, sample=10)),
          "ten disproved matches at a sample of ten print no ...and 0 more")
    check(any(l == "  ...and 1 more" for l in describe(exact, sample=9)),
          "and one fewer in the sample counts exactly one unlisted")
    exact_drift = layers([("H-%d" % i, 0.0, 0.0, None) for i in range(10)],
                         [("H-%d" % i, 0.0, 300.0, None) for i in range(10)])
    check(not any(l.startswith("  ...and")
                  for l in describe(exact_drift, sample=10)),
          "the drift band counts the boundary the same way")
    exact_ref = layers([("H-%d" % i, 0.0, 0.0, None) for i in range(10)]
                       + [("H-%d" % i, 1.0, 1.0, None) for i in range(10)],
                       [("H-0", 0.0, 0.0, None)])
    check(not any(l.startswith("      ...and")
                  for l in describe(exact_ref, sample=10)),
          "and so does a refusal listing exactly as many repeats as the sample")
    exact_note = layers([("H-%d" % i, 0.0, 0.0, "1") for i in range(10)],
                        [("H-%d" % i, 0.0, 51.0, "0") for i in range(10)],
                        status_column="OPERABLE")
    check(not any(l.startswith("  ...and")
                  for l in describe(exact_note, sample=10)),
          "and so does the status disagreement list")

    noted = layers([("H-%d" % i, 0.0, 0.0, "1") for i in range(12)],
                   [("H-%d" % i, 0.0, 51.0, "0") for i in range(12)],
                   status_column="OPERABLE")
    note_lines = describe(noted, sample=10)
    check(any("disagree on the status column" in l for l in note_lines),
          "pairs whose sides disagree on status are listed")
    check(sum(1 for l in note_lines if l == "  ...and 2 more") == 1,
          "and are sampled the same way as everything else")
    check(len([p for p in noted.pairs if p.note]) == 12,
          "all twelve disagreements are counted even though ten are listed")

    # ---- the worst-first ordering the report depends on
    ordered = layers([("A", 0.0, 0.0, None), ("B", 0.0, 0.0, None),
                      ("C", 0.0, 0.0, None)],
                     [("A", 0.0, 900.0, None), ("B", 0.0, 9000.0, None),
                      ("C", 0.0, 4000.0, None)])
    keys = [l.strip().split()[0] for l in describe(ordered)
            if l.startswith("  B ") or l.startswith("  A ")
            or l.startswith("  C ")]
    check(keys == ["B", "C", "A"],
          "disproved matches are listed worst first, not in file order")
    tied = layers([("B", 0.0, 0.0, None), ("A", 0.0, 0.0, None)],
                  [("B", 0.0, 9000.0, None), ("A", 0.0, 9000.0, None)])
    tied_keys = [l.strip().split()[0] for l in describe(tied)
                 if l.startswith("  A ") or l.startswith("  B ")]
    check(tied_keys == ["A", "B"],
          "two matches at the same distance are ordered by key, so the report "
          "does not shuffle")

    # ---- the rows --out writes
    out_rows = classified_rows(mixed)
    check(len(out_rows) == 11, "every input row gets an output row")
    check([r["side"] for r in out_rows].count("left") == 6,
          "the left side's six rows are all there")
    check(out_rows[0]["class"] == CONFIRMED and out_rows[0]["key"] == "H-1",
          "each output row carries its key and its class")
    check(out_rows[0]["distance"] == "51.000",
          "a matched row carries the separation that classified it")
    check(out_rows[5]["distance"] == "",
          "a row with no pair carries no distance")
    check(out_rows[3]["distance"] == "",
          "and neither does a pair whose separation could not be measured")
    check(out_rows[5]["x"] == "0.0" and out_rows[3]["x"] == "",
          "a coordinate is written back as it was read, or left empty")
    check(out_rows[5]["y"] == "0.0" and out_rows[3]["y"] == "",
          "and the Y column is written the same way as the X column, which is "
          "the half of the pair a test that only reads X never sees")
    check(all(r["reason"] for r in out_rows),
          "every output row carries the reason for its class")
    check(classified_rows(layers([("H-1", 0.0, 0.0, None), ("H-1", 1.0, 1.0,
                                                            None)],
                                 [("H-1", 0.0, 0.0, None)]))[0]["class"] == "",
          "a refused run writes no class for any row  <-- pinned defect")

    # ---- argument handling
    a = _parse(["l.csv", "r.csv"])
    check(a.apply is False, "--apply defaults to OFF")
    check(a.out is None, "--out defaults to nothing written")
    check(a.status_field is None, "--status-field defaults to off")
    check(a.right_id_field is None and a.right_x_field is None
          and a.right_y_field is None and a.right_status_field is None,
          "the right side defaults to the same columns as the left")
    # The values, not the constants the defaults are built from. Compared with
    # the constant, each of these passes whatever the constant is changed to,
    # and the README documents a number.
    check(a.confirm_distance == 150.0, "--confirm-distance defaults to 150")
    check(a.reuse_distance == 500.0, "--reuse-distance defaults to 500")
    check(a.units == "ft", "--units defaults to ft")
    check(a.sample == 10, "--sample defaults to 10")
    check(a.id_field == "ASSET_ID", "--id-field defaults to ASSET_ID")
    check(a.x_field == "X" and a.y_field == "Y",
          "--x-field and --y-field default to X and Y")
    check(a.left == "l.csv" and a.right == "r.csv",
          "the two layers are read positionally, for an ArcGIS script tool")
    check(_parse(["--self-test"]).self_test, "--self-test parses")
    check(_parse(["--self-test"]).left is None,
          "no layer is needed to run the self-test")
    check(_parse(["l.csv", "r.csv", "--id-field", "HYD"]).id_field == "HYD",
          "--id-field is read")
    check(_parse(["l.csv", "r.csv", "--right-id-field", "NUM"]
                 ).right_id_field == "NUM", "--right-id-field is read")
    check(_parse(["l.csv", "r.csv", "--x-field", "POINT_X"]).x_field
          == "POINT_X", "--x-field is read")
    check(_parse(["l.csv", "r.csv", "--y-field", "POINT_Y"]).y_field
          == "POINT_Y", "--y-field is read")
    check(_parse(["l.csv", "r.csv", "--right-x-field", "LON"]).right_x_field
          == "LON", "--right-x-field is read")
    check(_parse(["l.csv", "r.csv", "--right-y-field", "LAT"]).right_y_field
          == "LAT", "--right-y-field is read")
    check(_parse(["l.csv", "r.csv", "--status-field", "OPERABLE"]
                 ).status_field == "OPERABLE", "--status-field is read")
    check(_parse(["l.csv", "r.csv", "--right-status-field", "ACTIVE"]
                 ).right_status_field == "ACTIVE",
          "--right-status-field is read")
    check(_parse(["l.csv", "r.csv", "--confirm-distance", "75"]
                 ).confirm_distance == 75.0, "--confirm-distance is read")
    check(_parse(["l.csv", "r.csv", "--reuse-distance", "1000"]
                 ).reuse_distance == 1000.0, "--reuse-distance is read")
    check(_parse(["l.csv", "r.csv", "--units", "m"]).units == "m",
          "--units is read")
    check(_parse(["l.csv", "r.csv", "--sample", "0"]).sample == 0,
          "--sample is read")
    check(_parse(["l.csv", "r.csv", "--out", "o.csv"]).out == "o.csv",
          "--out is read")
    check(_parse(["l.csv", "r.csv", "--apply"]).apply is True,
          "--apply is read")

    # ---- the field maps the two sides are read with
    args = _parse(["l.csv", "r.csv"])
    check(side_fields(args, "left") == {"key": "ASSET_ID", "x": "X", "y": "Y",
                                        "status": None},
          "both sides read the same columns by default")
    args = _parse(["l.csv", "r.csv", "--right-id-field", "NUM",
                   "--right-x-field", "LON", "--right-y-field", "LAT",
                   "--status-field", "OPERABLE",
                   "--right-status-field", "ACTIVE"])
    check(side_fields(args, "right") == {"key": "NUM", "x": "LON", "y": "LAT",
                                         "status": "ACTIVE"},
          "the right side reads its own columns when they are named")
    check(side_fields(args, "left") == {"key": "ASSET_ID", "x": "X", "y": "Y",
                                        "status": "OPERABLE"},
          "and the left side is untouched by the right side's overrides")
    args = _parse(["l.csv", "r.csv", "--status-field", "OPERABLE"])
    check(side_fields(args, "right")["status"] == "OPERABLE",
          "one --status-field names the column on both sides")

    # ---- extraction off a raw CSV row
    fields = {"key": "ASSET_ID", "x": "X", "y": "Y", "status": None}
    rows = extract_rows([{"ASSET_ID": " H-1 ", "X": "1000.5", "Y": "2000.5"}],
                        "left", fields)
    check(rows[0].key == "H-1", "the anchor is trimmed as it is read")
    check((rows[0].x, rows[0].y) == (1000.5, 2000.5),
          "the coordinate columns are read as numbers")
    check(rows[0].line == 2, "the first data row is CSV line 2")
    check(rows[0].status is None, "no status column means no status value")
    check(extract_rows([{"ASSET_ID": None}], "left", fields)[0].key == "",
          "a missing anchor cell reads as empty, never as the word None")
    check(extract_rows([{}], "left", fields)[0].x is None,
          "a missing coordinate column reads None")
    check(extract_rows([{"ASSET_ID": "H-1", "X": "nan", "Y": "1"}], "left",
                       fields)[0].x is None,
          "a NaN coordinate cell cannot reach the separation  "
          "<-- pinned defect")
    stat_fields = dict(fields, status="OPERABLE")
    check(extract_rows([{"OPERABLE": " 1 "}], "left", stat_fields)[0].status
          == "1", "a status value is trimmed as it is read")
    check(extract_rows([{}], "left", stat_fields)[0].status == "",
          "a missing status cell reads as empty, not as None")
    check(extract_rows([{"ASSET_ID": 12345}], "left", fields)[0].key == "12345",
          "an anchor that arrived as a number is read as its own text")

    check(missing_columns(fields, ["ASSET_ID", "X", "Y"]) == [],
          "a header holding every column reports nothing missing")
    check(missing_columns(fields, ["ASSET_ID"]) == ["X", "Y"],
          "the columns that are absent are named, in the order they are read")
    check(missing_columns(stat_fields, ["ASSET_ID", "X", "Y"]) == ["OPERABLE"],
          "a named status column that is absent is missing too")
    check(missing_columns(fields, []) == ["ASSET_ID", "X", "Y"],
          "an empty header is missing all three")

    # ---- real files on disk. Everything below writes into one temp directory
    # and deletes it again. No network, no database, no credentials.
    tmp = tempfile.mkdtemp(prefix="idreuse-selftest-")

    def tmpfile(name, text, encoding="utf-8"):
        path = os.path.join(tmp, name)
        with open(path, "w", newline="", encoding=encoding) as handle:
            handle.write(text)
        return path

    def run_cli(argv):
        """main() with its output captured, so the self-test stays readable."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    # Two hydrant layers in one projected CRS, in feet. H-1 is a clean match,
    # H-2 drifts, H-3 is the recycled number, H-4 has no coordinate on one
    # side, H-5 is only in the fire layer and U-9 only in the utility layer.
    fire_text = (
        "HYDRANT_NO,X,Y,OPERABLE\n"
        "H-1,620100.0,1580200.0,1\n"
        "H-2,620500.0,1580600.0,1\n"
        "H-3,621000.0,1581000.0,1\n"
        "H-4,621400.0,1581400.0,1\n"
        "H-5,621800.0,1581800.0,1\n")
    util_text = (
        "HYDRANT_NO,X,Y,OPERABLE\n"
        "H-1,620100.0,1580251.0,1\n"
        "H-2,620500.0,1580900.0,1\n"
        "H-3,621000.0,1829940.0,1\n"
        "H-4,,,1\n"
        "U-9,622200.0,1582200.0,1\n")
    fire_csv = tmpfile("fire.csv", fire_text)
    util_csv = tmpfile("utilities.csv", util_text)
    base = [fire_csv, util_csv, "--id-field", "HYDRANT_NO"]

    code, out, err = run_cli(base)
    check(code == 1, "the two hydrant layers fail the gate")
    check("VERDICT: 1 match(es) disproved" in out,
          "because one match is disproved by the geometry")
    check("H-3  248940.0 ft apart, left line 4, right line 4" in out,
          "and it is named with its separation and both CSV lines")
    check("rows classified: 10 of 10" in out,
          "every row of both files is classified")
    check(err == "", "and a complete header reports nothing missing")
    check("H-2" in out and "drift band" in out,
          "the drifting match is reported apart from the disproved one")

    code, out, err = run_cli(base + ["--reuse-distance", "300000"])
    check(code == 0,
          "a reuse distance past the worst pair passes the gate")
    check("VERDICT: no match was disproved" in out,
          "and says nothing was disproved")
    code, out, err = run_cli(base + ["--confirm-distance", "300",
                                     "--reuse-distance", "300"])
    check(("  %-11s %6d" % (DRIFT, 0)) in out,
          "two equal thresholds leave the drift band empty")
    code, out, err = run_cli(base + ["--confirm-distance", "10"])
    check(("  %-11s %6d" % (CONFIRMED, 0)) in out,
          "a confirm distance under every pair confirms nothing")
    code, out, err = run_cli(base + ["--units", "m"])
    check("248940.0 m apart" in out,
          "--units relabels every distance and converts nothing")
    code, out, err = run_cli(base + ["--sample", "0"])
    check("H-3  248940.0" not in out and "VERDICT: 1 match(es)" in out,
          "--sample 0 lists no match and still reports the verdict")

    # ---- the status column, through the CLI, on the layers from the story
    code, out, err = run_cli(base + ["--status-field", "OPERABLE"])
    check("refused as a status signal" in out,
          "OPERABLE is 1 on all ten rows, so it is refused as a signal"
          "  <-- pinned defect")
    check("'OPERABLE'" in out and "'1'" in out,
          "and the refusal names the column and the value")
    check(code == 1,
          "the run continues and still fails on the match it disproved"
          "  <-- pinned defect")
    live_text = util_text.replace("H-3,621000.0,1829940.0,1",
                                  "H-3,621000.0,1829940.0,0")
    live_csv = tmpfile("utilities-live.csv", live_text)
    code, out, err = run_cli([fire_csv, live_csv, "--id-field", "HYDRANT_NO",
                              "--status-field", "OPERABLE"])
    check("holds 2 distinct value(s)" in out,
          "a status column with two values is usable")
    check("disagree on the status column" in out and "H-3" in out,
          "and the disproved pair is also flagged as disagreeing on it")

    # ---- the anchor refusal, on a file where the number really does repeat
    dupe_csv = tmpfile("fire-dupe.csv", fire_text + "H-1,999999.0,999999.0,1\n")
    code, out, err = run_cli([dupe_csv, util_csv, "--id-field", "HYDRANT_NO"])
    check(code == 3,
          "a repeated anchor exits 3, not the 1 a disproved match exits"
          "  <-- pinned defect")
    check("left key 'H-1' is on 2 rows: line 2, 7" in out,
          "and both rows the key is on are named")
    check("rows classified" not in out and "CONFIRMED" not in out,
          "and nothing else is reported at all  <-- pinned defect")

    # ---- the UTF-8 BOM that Excel and Table To Table both write
    bom_csv = tmpfile("fire-bom.csv", fire_text, encoding="utf-8-sig")
    bom_rows, bom_header = read_csv(bom_csv)
    check(bom_header[0] == "HYDRANT_NO",
          "a UTF-8 BOM is stripped from the first column name"
          "  <-- pinned defect")
    check(bom_rows[0]["HYDRANT_NO"] == "H-1",
          "and the anchor column of a BOM file is still addressable by name")
    check(run_cli([bom_csv, util_csv, "--id-field", "HYDRANT_NO"])[0] == 1,
          "a BOM file reconciles the same as one without  <-- pinned defect")

    # ---- the two sides naming their columns differently
    odd_csv = tmpfile(
        "odd.csv",
        "NUM,POINT_X,POINT_Y,ACTIVEFLAG\n"
        "H-1,620100.0,1580251.0,1\n"
        "H-3,621000.0,1829940.0,1\n")
    code, out, err = run_cli([fire_csv, odd_csv, "--id-field", "HYDRANT_NO"])
    check(code == 64, "a right side that names nothing expected is refused")
    check("utilities" not in err and "columns not in" in err,
          "and the error names the columns that are absent")
    check("NUM, POINT_X, POINT_Y, ACTIVEFLAG" in err,
          "and lists the columns the file does have")
    code, out, err = run_cli([fire_csv, odd_csv, "--id-field", "HYDRANT_NO",
                              "--right-id-field", "NUM",
                              "--right-x-field", "POINT_X",
                              "--right-y-field", "POINT_Y"])
    check(code == 1, "the three right-side overrides make the same file usable")
    check("H-3  248940.0 ft apart" in out,
          "and the recycled number is found through them")
    check(err == "", "with nothing reported missing")

    # ---- --out and --apply
    out_path = os.path.join(tmp, "classified.csv")
    code, out, err = run_cli(base + ["--out", out_path])
    check(not os.path.exists(out_path),
          "--out without --apply writes nothing at all  <-- pinned defect")
    check("was not written" in out, "--out without --apply says what it skipped")
    code, out, err = run_cli(base + ["--out", out_path, "--apply"])
    check(code == 1 and os.path.exists(out_path),
          "--apply writes the classification and keeps the gate's exit code")
    check("wrote %s" % out_path in out, "the run names the file it wrote")
    written, written_header = read_csv(out_path)
    check(written_header == list(OUT_COLUMNS),
          "the classification carries the eight columns it documents")
    check(len(written) == 10, "one row per input row from both files")
    check([r["class"] for r in written].count(REUSED) == 2,
          "the disproved pair is written on both its rows")
    check(written[2]["key"] == "H-3" and written[2]["distance"] == "248940.000",
          "and carries the separation that disproved it")
    check(written[4]["class"] == UNMATCHED and written[4]["distance"] == "",
          "an unmatched row is written with no distance")
    check(written[8]["side"] == "right" and written[8]["csv_line"] == "5",
          "every row names its side and its line in its own file")
    check(run_cli(base + ["--out", tmp, "--apply"])[0] == 2,
          "a write that fails exits 2, not the gate's 1")
    refused_out = os.path.join(tmp, "refused.csv")
    code, out, err = run_cli([dupe_csv, util_csv, "--id-field", "HYDRANT_NO",
                              "--out", refused_out, "--apply"])
    check(code == 3 and not os.path.exists(refused_out),
          "a refused run writes nothing, because it classified nothing"
          "  <-- pinned defect")

    # ---- the exits a scheduled job reads
    check(run_cli([])[0] == 64, "a run with no layer is a usage error")
    check(run_cli([fire_csv])[0] == 64, "one layer is a usage error too")
    check(run_cli(base + ["--apply"])[0] == 64,
          "--apply without --out is a usage error")
    check(run_cli(base + ["--confirm-distance", "-1"])[0] == 64,
          "a negative confirm distance is a usage error")
    check(run_cli(base + ["--reuse-distance", "10"])[0] == 64,
          "a reuse distance under the confirm distance is a usage error")
    code, out, err = run_cli(base + ["--sample", "-1"])
    check(code == 64 and "--sample cannot be negative" in err,
          "a negative sample is a usage error, not a traceback")
    check(run_cli([os.path.join(tmp, "absent.csv"), util_csv])[0] == 2,
          "a layer that is not there exits 2, not the gate's 1")
    check(run_cli([tmp, util_csv])[0] == 2,
          "a directory where a layer should be exits 2")
    huge_csv = tmpfile("huge.csv", "HYDRANT_NO,X,Y\n\"%s\",1,1\n"
                       % ("A" * 200000))
    check(run_cli([huge_csv, util_csv, "--id-field", "HYDRANT_NO"])[0] == 2,
          "a CSV field over the csv module's own limit exits 2, not 1"
          "  <-- pinned defect")
    empty_csv = tmpfile("empty.csv", "")
    code, out, err = run_cli([empty_csv, util_csv, "--id-field", "HYDRANT_NO"])
    check(code == 64 and "columns not in" in err,
          "an empty CSV has no columns, which is a usage error naming them")
    header_only = tmpfile("header.csv", "HYDRANT_NO,X,Y,OPERABLE\n")
    code, out, err = run_cli([header_only, util_csv, "--id-field",
                              "HYDRANT_NO"])
    check(code == 0 and "rows classified: 5 of 5" in out,
          "a layer with no rows classifies the other side and passes")
    argv_before = sys.argv
    try:
        sys.argv = ["idreuse.py", fire_csv, util_csv, "--id-field",
                    "HYDRANT_NO", "--reuse-distance", "300000"]
        check(run_cli(None)[0] == 0,
              "main with no argv reads the arguments after the program name")
    finally:
        sys.argv = argv_before

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass, which is the one failure no other
    # assertion here could see. The probe's own output is swallowed so a green
    # run prints no FAIL line.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")
    raises(lambda: (_ for _ in ()).throw(ValueError("x")),
           "and raises() accepts the ValueError it is looking for")

    shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def read_csv(path):
    """Raw rows and the header of a CSV, as dicts."""
    # utf-8-sig, because Esri's Table To Table and Excel both write a UTF-8
    # BOM. Read as plain UTF-8 the first header cell arrives as a BOM with
    # HYDRANT_NO after it, the anchor column reads as absent on every row, and
    # the run stops for a reason nothing in the output explains.
    with open(path, "r", newline="", encoding="utf-8-sig",
              errors="replace") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def side_fields(args, side):
    """The four column names one side is read with."""
    if side == "right":
        return {
            "key": args.right_id_field or args.id_field,
            "x": args.right_x_field or args.x_field,
            "y": args.right_y_field or args.y_field,
            "status": args.right_status_field or args.status_field,
        }
    return {"key": args.id_field, "x": args.x_field, "y": args.y_field,
            "status": args.status_field}


def missing_columns(fields, header):
    """Named columns that are not in this header, in the order they are read.

    Every one of them is required. Without the anchor there is no join, and
    without a coordinate there is no geometry to disprove it with, so a
    warning would leave the tool reporting UNVERIFIED on every row and looking
    like a data problem rather than a wrong column name.
    """
    names = [fields["key"], fields["x"], fields["y"]]
    if fields.get("status"):
        names.append(fields["status"])
    return [name for name in names if name not in header]


def extract_rows(raw_rows, side, fields):
    """Pull the four values this tool reasons about out of each CSV row."""
    out = []
    for index, raw in enumerate(raw_rows):
        key = raw.get(fields["key"])
        status = None
        if fields.get("status"):
            value = raw.get(fields["status"])
            status = "" if value is None else str(value).strip()
        out.append(Row(index, side,
                       "" if key is None else str(key).strip(),
                       to_number(raw.get(fields["x"])),
                       to_number(raw.get(fields["y"])),
                       status))
    return out


def write_classified(path, report):
    """Write one row per input row, with the class and the separation."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUT_COLUMNS))
        writer.writeheader()
        for row in classified_rows(report):
            writer.writerow(row)


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="idreuse.py",
        description="Reconcile two asset layers on a shared ID and refuse "
                    "every match the geometry disproves.",
        epilog="Both layers must be in one projected CRS. Nothing is written "
               "without --apply.",
    )
    ap.add_argument("left", nargs="?", metavar="LEFT",
                    help="the first layer as a CSV")
    ap.add_argument("right", nargs="?", metavar="RIGHT",
                    help="the second layer as a CSV")
    ap.add_argument("--id-field", dest="id_field", default=DEFAULT_ID_FIELD,
                    help="the shared identifier column on both sides "
                         "(default %s)" % DEFAULT_ID_FIELD)
    ap.add_argument("--x-field", dest="x_field", default=DEFAULT_X_FIELD,
                    help="the X column on both sides (default %s)"
                         % DEFAULT_X_FIELD)
    ap.add_argument("--y-field", dest="y_field", default=DEFAULT_Y_FIELD,
                    help="the Y column on both sides (default %s)"
                         % DEFAULT_Y_FIELD)
    ap.add_argument("--status-field", dest="status_field",
                    help="a live-or-retired column to read on both sides. It "
                         "is never filtered on.")
    ap.add_argument("--right-id-field", dest="right_id_field",
                    help="the identifier column on the right side, when it is "
                         "not the one --id-field names")
    ap.add_argument("--right-x-field", dest="right_x_field",
                    help="the X column on the right side")
    ap.add_argument("--right-y-field", dest="right_y_field",
                    help="the Y column on the right side")
    ap.add_argument("--right-status-field", dest="right_status_field",
                    help="the status column on the right side")
    ap.add_argument("--confirm-distance", dest="confirm_distance", type=float,
                    default=DEFAULT_CONFIRM_DISTANCE,
                    help="separation at or under which a match is CONFIRMED, "
                         "in the CRS's own units (default %.0f)"
                         % DEFAULT_CONFIRM_DISTANCE)
    ap.add_argument("--reuse-distance", dest="reuse_distance", type=float,
                    default=DEFAULT_REUSE_DISTANCE,
                    help="separation above which a match is REUSED "
                         "(default %.0f)" % DEFAULT_REUSE_DISTANCE)
    ap.add_argument("--units", default=DEFAULT_UNITS,
                    help="label printed after every distance. Nothing is "
                         "converted. (default %s)" % DEFAULT_UNITS)
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="how many records to list under each heading "
                         "(default %d)" % DEFAULT_SAMPLE)
    ap.add_argument("--out", help="path for the classification CSV")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _load(path, side, args):
    """One side of the comparison, read and reduced. Raises on a bad column."""
    raw_rows, header = read_csv(path)
    fields = side_fields(args, side)
    absent = missing_columns(fields, header)
    if absent:
        raise ValueError(
            "%s: columns not in the CSV: %s. It has: %s"
            % (path, ", ".join(absent),
               ", ".join(header) if header else "(no columns at all)"))
    return extract_rows(raw_rows, side, fields)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.left or not args.right:
        print("error: two CSV layers are required. Use --self-test to verify "
              "the tool without them.", file=sys.stderr)
        return 64
    if args.sample < 0:
        print("error: --sample cannot be negative.", file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out.", file=sys.stderr)
        return 64

    try:
        left = _load(args.left, "left", args)
        right = _load(args.right, "right", args)
    except ValueError as exc:
        # A column name the operator got wrong is a usage error, and a file
        # that could not be read is not. Only the exit code tells them apart.
        print("error: %s" % exc, file=sys.stderr)
        return 64
    except (IOError, OSError, csv.Error) as exc:
        # csv.Error too. A field over csv's 131072 character limit, or a NUL
        # in the file, is an unreadable input, not a join that failed.
        print("error: %s" % exc, file=sys.stderr)
        return 2

    print("idreuse: %s %d row(s), %s %d row(s)"
          % (args.left, len(left), args.right, len(right)))

    try:
        report = reconcile(left, right, args.confirm_distance,
                           args.reuse_distance, args.units,
                           args.status_field)
        lines = describe(report, args.sample)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    for line in lines:
        print(line)

    if args.out:
        if args.apply:
            if report.refused:
                print("\nRefused. %s was not written, because nothing was "
                      "classified." % args.out)
            else:
                # A traceback here exits 1, and 1 is the code a scheduled job
                # reads as "a match was disproved". A write that failed is a
                # different fact and gets its own exit code.
                try:
                    write_classified(args.out, report)
                except (IOError, OSError, csv.Error) as exc:
                    print("error: could not write %s: %s" % (args.out, exc),
                          file=sys.stderr)
                    return 2
                print("\nwrote %s" % args.out)
        else:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)

    return gate(report)


if __name__ == "__main__":
    sys.exit(main())
