#!/usr/bin/env python
"""Reconcile two asset layers on a shared ID and refuse every match the geometry disproves.

Refuses the ID itself when it is not unique, refuses a status or district
column that cannot carry a signal, and refuses to call a run clean when it
compared nothing.

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
    python idreuse.py fire.csv utilities.csv --district-field DISTRICT
    python idreuse.py fire.csv utilities.csv --rematch-distance 50
    python idreuse.py fire.csv utilities.csv --crosswalk links.csv --apply

A district column is a third signal: a drift pair whose two sides sit in
different districts is escalated to REUSED. --rematch-distance proposes a
CANDIDATE partner for a row the key could not pair, but only when each row is
the other's single nearest. Nothing is written without --apply.

Exit codes: 0 at least one pair was measured and no match was disproved, 1 at
least one match was disproved, 2 a file could not be read or written, 3 the
anchor was refused, 4 nothing was compared (rows were read and no pair on a
shared key had a measurable separation), 64 usage error, 70 an unexpected
crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import importlib.util
import io
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

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

# The sixth class, which only --rematch-distance can produce. A row the key
# could not pair, or paired wrongly, whose geometry proposes a partner instead.
# It is a proposal for a human, never a match, so it never passes or fails the
# gate. The partition still holds: every row is in exactly one of all six.
CANDIDATE = "CANDIDATE"
ALL_CLASSES = CLASSES + (CANDIDATE,)

# A line break inside a quoted cell, as csv itself counts them.
_BREAK = re.compile(r"\r\n|\r|\n")

# The columns --out writes. This is a new file, not a copy of either input,
# because there are two inputs and one of them has no row for half the keys.
OUT_COLUMNS = ("side", "csv_line", "key", "class", "distance", "x", "y",
               "reason")

# The columns --crosswalk writes: one row per link worth keeping. A REUSED,
# UNVERIFIED or UNMATCHED row has no link, so it has no crosswalk row.
CROSSWALK_COLUMNS = ("left_key", "right_key", "tier", "distance", "left_line",
                     "right_line", "left_district", "right_district", "reason")


class Row(object):
    """One input row, reduced to the four values this tool reasons about."""

    def __init__(self, index, side, key, x, y, status=None, district=None,
                 line=None):
        self.index = index
        self.side = side
        self.key = key
        self.x = x
        self.y = y
        self.status = status
        self.district = district
        # The line this row starts on in its CSV, which is what the operator
        # has open. A blank line or a quoted newline moves it away from
        # index + 2, so the reader passes the real one. Without it, index 0 is
        # the first data row on line 2, below the header.
        self.line = self.index + 2 if line is None else line
        self.verdict = None
        self.reason = ""

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
        self.district_note = None

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
                 confirm, reuse, candidates=(), district_note=None,
                 rematch=None):
        self.candidates = list(candidates)
        self.district_note = district_note
        self.rematch = rematch
        self.left = left
        self.right = right
        self.pairs = pairs
        self.repeats = repeats
        self.status_note = status_note
        self.units = units
        self.confirm = confirm
        self.reuse = reuse
        self.counts = dict((name, 0) for name in ALL_CLASSES)
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

    @property
    def measured(self):
        """Pairs on a shared key whose separation could be measured. A run
        with rows and none of these compared nothing, whatever it printed."""
        return [p for p in self.pairs if p.distance is not None]

    @property
    def compared_nothing(self):
        """Rows were read, nothing was refused or disproved, and no pair was
        measured. The join may be wrong and this run could not tell."""
        return bool(self.total) and not self.refused and not self.measured

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
    return _signal(values, column, "status",
                   "a live record from a retired one",
                   "nothing is filtered on it",
                   "a pair whose two sides disagree is reported")


def district_signal(values, column):
    """(usable, message) for a column offered as the district each row is in.

    The same proof the status column gets. A district column that holds one
    value on every row cannot say that two rows sit in different districts,
    so escalating on it would escalate nothing and look like a defence. A
    blank is a hole, not a value, so it is not counted: a column that is blank
    on most rows and "7" on the rest would otherwise read as holding two.
    """
    filled = [v for v in values if v is not None and str(v).strip()]
    if values and not filled:
        return False, ("district column %r is blank on all %d row(s), so it "
                       "is refused as a district signal and no pair is "
                       "escalated on it." % (column, len(values)))
    return _signal(filled, column, "district",
                   "one district from another",
                   "no pair is escalated on it",
                   "a drift pair whose two sides disagree is escalated to "
                   "REUSED")


def _signal(values, column, kind, separates, refused, usable):
    """Prove a column carries a signal before anything reads it as one."""
    seen = []
    for value in values:
        text = "" if value is None else str(value).strip()
        if text not in seen:
            seen.append(text)
    if not seen:
        return False, ("%s column %r has no rows to read, so it carries no "
                       "signal" % (kind, column))
    if len(seen) == 1:
        return False, ("%s column %r holds the single value %r on all %d "
                       "row(s). It cannot separate %s, so it is refused as a "
                       "%s signal and %s."
                       % (kind, column, seen[0], len(values), separates, kind,
                          refused))
    return True, ("%s column %r holds %d distinct value(s), so %s"
                  % (kind, column, len(seen), usable))


def _few(values):
    """Up to five values, sorted, for a message."""
    shown = sorted(values)
    text = ", ".join(repr(v) for v in shown[:5])
    return text + (", ..." if len(shown) > 5 else "")


def district_vocabulary(left, right, right_by_key, column, confirm):
    """Message when the two sides do not use one district coding, else None.

    district_signal proves the column can tell districts apart. It cannot
    prove the two layers name them the same way, and two departments rarely
    do: "7", "07", "D7" and "Station 7" are four different strings and one
    district. Escalating on that would condemn every drift pair. So the
    column must also share a value across the sides, agree on at least one
    key-matched pair, and agree on most of the pairs the geometry already
    confirms, because one object cannot sit in two districts.
    """
    mine = set(r.district for r in left if r.district)
    theirs = set(r.district for r in right if r.district)
    head = "district column %r is refused as a district signal: " % column
    tail = " No pair is escalated on it."
    if not mine or not theirs:
        return head + "the %s side has no district value." % (
            "left" if not mine else "right") + tail
    if not mine & theirs:
        return head + ("the two sides share no value. Left holds %s and "
                       "right holds %s, so they are not coded the same way."
                       % (_few(mine), _few(theirs))) + tail
    agree = confirmed_agree = confirmed_differ = 0
    for row in left:
        other = right_by_key.get(row.key) if row.key else None
        if other is None or not row.district or not other.district:
            continue
        same = row.district == other.district
        agree += same
        distance = separation(row.x, row.y, other.x, other.y)
        if distance is not None and distance <= confirm:
            if same:
                confirmed_agree += 1
            else:
                confirmed_differ += 1
    if not agree:
        return head + ("no pair on a shared key has the same district on "
                       "both sides.") + tail
    if confirmed_differ > confirmed_agree:
        return head + ("%d of the %d pairs the geometry confirms are in "
                       "different districts on the two sides, so the codes "
                       "do not match." % (confirmed_differ,
                                          confirmed_differ + confirmed_agree)
                       ) + tail
    return None


def status_disagreement(left, right, column):
    """Message when two paired rows disagree on the status column, else None."""
    if left.status is None or right.status is None:
        return None
    if left.status == right.status:
        return None
    return ("the two sides disagree on %s: %r on the left, %r on the right"
            % (column, left.status, right.status))


def district_disagreement(left, right, column):
    """Message when two paired rows sit in different districts, else None.

    A blank district is a hole, not a district. Escalating on it would turn a
    missing cell into evidence that a key was recycled.
    """
    if not left.district or not right.district:
        return None
    if left.district == right.district:
        return None
    return ("the two sides are in different districts on %s: %r on the "
            "left, %r on the right" % (column, left.district, right.district))


def nearest(row, pool):
    """(other, distance) for the single nearest row in pool, else (None, None).

    A tie at the nearest distance answers None. Two rows the same distance
    away are two claims on one point, and picking either would make the
    answer depend on file order, which a rerun on sorted data would change.
    """
    best, best_distance, tied = None, None, False
    # shortcut: every row against every pooled row, O(n*m). The pool is only
    # the rows the key failed. 3,000 a side with every row pooled took 9 to
    # 15 s. Upgrade to a grid index if a pool that size becomes routine.
    for other in pool:
        distance = separation(row.x, row.y, other.x, other.y)
        if distance is None:
            continue
        if best_distance is None or distance < best_distance:
            best, best_distance, tied = other, distance, False
        elif distance == best_distance:
            tied = True
    if best is None or tied:
        return None, None
    return best, best_distance


def rematch(left, right, limit, units=DEFAULT_UNITS):
    """CANDIDATE pairs among the rows the key did not pair, or paired wrongly.

    The pool on each side is every UNMATCHED row and every row of a REUSED
    pair. A left row and a right row become a candidate only when each is the
    other's single nearest row in the pool and they are at most limit apart.
    Mutual nearest is what keeps it one to one: a right row has at most one
    nearest left row, so two left rows can never both claim it. A plain
    nearest-neighbour join lets every row in a dense block claim the same one.
    """
    left_pool = [r for r in left if r.verdict in (UNMATCHED, REUSED)]
    right_pool = [r for r in right if r.verdict in (UNMATCHED, REUSED)]
    found = []
    for row in left_pool:
        other, distance = nearest(row, right_pool)
        if other is None or distance > limit:
            continue
        back, _ = nearest(other, left_pool)
        if back is not row:
            continue
        reason = ("%.1f %s between left line %d (key %r) and right line %d "
                  "(key %r); each is the other's nearest within %.1f. A "
                  "proposal for a human, not a match."
                  % (distance, units, row.line, row.key, other.line,
                     other.key, limit))
        found.append(Pair(row.key, row, other, distance, CANDIDATE, reason))
    # Verdicts change only after the search, so no row leaves a pool while
    # another row is still being measured against it.
    for pair in found:
        pair.left.verdict = pair.right.verdict = CANDIDATE
        pair.left.reason = pair.right.reason = pair.reason
    return found


def reconcile(left, right, confirm=DEFAULT_CONFIRM_DISTANCE,
              reuse=DEFAULT_REUSE_DISTANCE, units=DEFAULT_UNITS,
              status_column=None, district_column=None, rematch_distance=None):
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
    if rematch_distance is not None:
        if not _is_finite(rematch_distance) or rematch_distance < 0:
            raise ValueError("the rematch distance must be a real number of "
                             "zero or more, got %r" % (rematch_distance,))
        # A candidate must sit at least as close as a confirmed match. That
        # also means a REUSED pair can never re-propose itself: its separation
        # is past the reuse distance, so it is past this one too.
        if rematch_distance > confirm:
            raise ValueError("the rematch distance %r is above the confirm "
                             "distance %r" % (rematch_distance, confirm))

    right_by_key = dict((r.key, r) for r in right if r.key)

    district_note = None
    if district_column:
        usable, district_note = district_signal(
            [r.district for r in list(left) + list(right)], district_column)
        if usable:
            refusal = district_vocabulary(left, right, right_by_key,
                                          district_column, confirm)
            if refusal:
                usable, district_note = False, refusal
        if not usable:
            district_column = None

    status_note = None
    if status_column:
        usable, message = status_signal(
            [r.status for r in list(left) + list(right)], status_column)
        status_note = message
        if not usable:
            # Refused as a signal. The rows keep their values; nothing reads
            # them again.
            status_column = None

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
        moved = None
        if district_column:
            moved = district_disagreement(row, other, district_column)
        if moved and verdict == DRIFT:
            # The geometry had no opinion and the district has one. A recycled
            # number usually lands in another district, so this is the pair
            # the drift band would otherwise hand a human as benign.
            verdict = REUSED
            reason = "%s; and %s. One key, two objects." % (reason, moved)
        row.verdict = other.verdict = verdict
        row.reason = other.reason = reason
        pair = Pair(row.key, row, other, distance, verdict, reason, note)
        pair.district_note = moved
        pairs.append(pair)

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

    candidates = []
    if rematch_distance is not None:
        candidates = rematch(left, right, rematch_distance, units)

    return Report(left, right, pairs, [], status_note, units, confirm, reuse,
                  candidates, district_note, rematch_distance)


def gate(report):
    """Exit code for a report, which is the whole point of running this."""
    if report.refused:
        return 3
    if report.disproved:
        return 1
    return 4 if report.compared_nothing else 0


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
    if report.district_note:
        out.append("DISTRICT  %s" % report.district_note)
    out.append("rows classified: %d of %d" % (report.classified, report.total))
    # CANDIDATE is printed only when the rematch ran. A zero would read as
    # "looked and found none" on a run that never looked.
    shown = ALL_CLASSES if report.rematch is not None else CLASSES
    for name in shown:
        out.append("  %-11s %6d" % (name, report.counts[name]))
    out.append("pairs: %d on a shared key, at confirm %.1f %s and reuse "
               "%.1f %s" % (len(report.pairs), report.confirm, report.units,
                            report.reuse, report.units))
    if report.rematch is not None:
        out.append("rematch: %d candidate pair(s), each the other's nearest "
                   "within %.1f %s" % (len(report.candidates), report.rematch,
                                       report.units))

    worst = sorted([p for p in report.pairs if p.distance is not None],
                   key=lambda p: (-p.distance, p.key))
    disproved = [p for p in worst if p.verdict == REUSED]
    if disproved:
        out.append("")
        out.append("matches the geometry disproves, worst first:")
        for pair in disproved[:sample]:
            line = ("  %s  %.1f %s apart, left line %d, right line %d"
                    % (pair.key, pair.distance, report.units,
                       pair.left.line, pair.right.line))
            if pair.district_note:
                line += ", districts %r and %r" % (pair.left.district,
                                                   pair.right.district)
            out.append(line)
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

    _section(out, "pairs whose two sides are in different districts:",
             ["  %s  %s" % (p.key, p.district_note)
              for p in report.pairs if p.district_note], sample)
    _section(out, "rematch candidates, for a human to confirm or reject:",
             ["  %s -> %s  %.1f %s apart, left line %d, right line %d"
              % (p.left.key, p.right.key, p.distance, report.units,
                 p.left.line, p.right.line) for p in report.candidates],
             sample)

    out.append("")
    if report.disproved:
        out.append("VERDICT: %d match(es) disproved by the geometry. Do not "
                   "publish this join." % len(report.disproved))
    elif report.compared_nothing:
        out.append("VERDICT: NOTHING COMPARED. No pair on a shared key had a "
                   "separation that could be measured, so the geometry "
                   "checked nothing. This is not a clean join. Look for an ID "
                   "spelled differently on the two sides, or for coordinates "
                   "this tool cannot read.")
    else:
        out.append("VERDICT: no match was disproved by the geometry.")
    return out


def _section(out, heading, lines, sample):
    """Append a heading and up to sample lines, counting the rest."""
    if not lines:
        return
    out.append("")
    out.append(heading)
    out.extend(lines[:sample])
    if len(lines) > sample:
        out.append("  ...and %d more" % (len(lines) - sample))


def crosswalk_rows(report):
    """One --crosswalk record per link worth keeping, pairs before candidates.

    CONFIRMED and DRIFT pairs from the key, then every rematch CANDIDATE. The
    tier column says which is which, so a DRIFT link or a CANDIDATE is never
    read as settled. REUSED, UNVERIFIED and UNMATCHED rows have no link.
    """
    links = [p for p in report.pairs if p.verdict in (CONFIRMED, DRIFT)]
    rows = []
    for pair in links + list(report.candidates):
        rows.append({
            "left_key": pair.left.key,
            "right_key": pair.right.key,
            "tier": pair.verdict,
            "distance": "%.3f" % pair.distance,
            "left_line": pair.left.line,
            "right_line": pair.right.line,
            "left_district": pair.left.district or "",
            "right_district": pair.right.district or "",
            "reason": pair.reason,
        })
    return rows


def classified_rows(report):
    """Every input row as an --out record, left side first, in file order."""
    rows = []
    by_row = {}
    # Candidates last, so a row of a REUSED pair that rematched carries the
    # distance to its candidate, which is what its class now rests on.
    for pair in list(report.pairs) + list(report.candidates):
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
    """Run the assertions in a temp directory that is removed even when one
    of them raises, so a crash never leaves idreuse-selftest-* behind."""
    tmp = tempfile.mkdtemp(prefix="idreuse-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(tmp):
    """Assertions over the decision core. No network, no credentials. The
    only files are the ones written under tmp."""
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

    print("idreuse self-test: temporary files only, no network, no "
          "credentials")
    print("-" * 68)

    # ---- the three separations the thresholds were set from
    check(verdict(51.0) == CONFIRMED,
          "a pair 51 units apart is CONFIRMED, which is the ordinary case")
    check(verdict(300.0) == DRIFT,
          "a pair 300 units apart is DRIFT, neither confirmed nor rejected")
    check(verdict(250000.0) == REUSED,
          "a pair 250000 units apart is REUSED, never a match"
          "  <-- pinned defect")
    check(classify_separation(250000.0)[1].endswith("One key, two objects."),
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
          "a pair 250000 units apart classifies both rows REUSED")
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
    check(gate(rep) == 4,
          "a run whose only pair is UNVERIFIED exits 4, not 0: it compared "
          "nothing  <-- pinned defect")
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

    # ---- v2: the district column, a third signal beside the key and the
    # geometry. A recycled number usually lands in another district.
    def dlayers(left_spec, right_spec, **kw):
        left = [Row(i, "left", k, x, y, None, d)
                for i, (k, x, y, d) in enumerate(left_spec)]
        right = [Row(i, "right", k, x, y, None, d)
                 for i, (k, x, y, d) in enumerate(right_spec)]
        return reconcile(left, right, **kw)

    usable, message = district_signal(["7", "7", "7"], "DISTRICT")
    check(usable is False and "refused as a district signal" in message,
          "a district column holding one value on every row is refused as a "
          "signal  <-- pinned defect")
    check("no pair is escalated on it" in message and "'7'" in message,
          "and the refusal names the value and says nothing was escalated")
    check(district_signal(["7", "8"], "DISTRICT")[0] is True,
          "a district column with two values carries a signal")
    check("escalated to REUSED" in district_signal(["7", "8"], "DISTRICT")[1],
          "and says what the signal will be used for")
    check(district_signal([], "DISTRICT")[1].startswith("district column"),
          "an empty district column names itself as a district column")
    check(district_disagreement(Row(0, "left", "H-1", 0, 0, None, "7"),
                                Row(0, "right", "H-1", 0, 0, None, "8"),
                                "DISTRICT") is not None,
          "two sides in different districts disagree")
    check(district_disagreement(Row(0, "left", "H-1", 0, 0, None, "7"),
                                Row(0, "right", "H-1", 0, 0, None, "7"),
                                "DISTRICT") is None,
          "two sides in one district do not")
    check(district_disagreement(Row(0, "left", "H-1", 0, 0, None, ""),
                                Row(0, "right", "H-1", 0, 0, None, "8"),
                                "DISTRICT") is None,
          "a blank district is a hole, not evidence of a move"
          "  <-- pinned defect")
    check(district_disagreement(Row(0, "left", "H-1", 0, 0, None, "7"),
                                Row(0, "right", "H-1", 0, 0, None, None),
                                "DISTRICT") is None,
          "and so is a district that was never read")

    spread = [("A", 0.0, 0.0, "7"), ("B", 0.0, 0.0, "7"),
              ("C", 0.0, 0.0, "7"), ("D", 0.0, 0.0, "7"),
              ("G", 0.0, 0.0, "7")]
    # G is the pair that agrees: C alone, confirmed across a line, would
    # make most confirmed pairs disagree and refuse the column.
    moved = [("A", 0.0, 300.0, "8"), ("B", 0.0, 300.0, "7"),
             ("C", 0.0, 51.0, "8"), ("D", 0.0, 9000.0, "8"),
             ("G", 0.0, 10.0, "7")]
    rep = dlayers(spread, moved, district_column="DISTRICT")
    by_key = dict((p.key, p) for p in rep.pairs)
    check(by_key["A"].verdict == REUSED,
          "a drift pair whose sides are in different districts is REUSED"
          "  <-- pinned defect")
    check("different districts" in by_key["A"].reason
          and by_key["A"].reason.endswith("One key, two objects."),
          "and its reason names the district move and says one key, two "
          "objects")
    check(by_key["B"].verdict == DRIFT,
          "a drift pair inside one district stays DRIFT")
    check(by_key["C"].verdict == CONFIRMED,
          "a confirmed pair across a district line stays CONFIRMED: a point "
          "on a boundary road is one object")
    check(by_key["C"].district_note is not None,
          "but carries the district note, so a human can see it")
    check(by_key["D"].verdict == REUSED and by_key["D"].district_note,
          "a pair the distance already disproved keeps REUSED and the note")
    check(gate(rep) == 1 and len(rep.disproved) == 2,
          "the escalated pair fails the gate with the one the distance "
          "disproved")
    check(rep.classified == rep.total == 10,
          "and every row is still in exactly one class")
    check("DISTRICT" in rep.district_note and "2 distinct" in rep.district_note,
          "the report carries the proof that the district column has a "
          "signal")
    lines = describe(rep)
    check(any(l.startswith("DISTRICT  district column 'DISTRICT' holds 2")
              for l in lines),
          "and prints it on its own line")
    check(any(l.startswith("  A  300.0 ft apart") and
              l.endswith("districts '7' and '8'") for l in lines),
          "an escalated pair is listed as disproved with both districts")
    check(any(l == "pairs whose two sides are in different districts:"
              for l in lines),
          "every district disagreement is listed under its own heading")
    check(sum(1 for l in lines if "different districts on DISTRICT" in l) == 3,
          "and all three pairs that cross a district line are in it")
    rep = dlayers(spread, [("A", 0.0, 300.0, "7"), ("B", 0.0, 300.0, "7"),
                           ("C", 0.0, 51.0, "7"), ("D", 0.0, 9000.0, "7")],
                  district_column="DISTRICT")
    check("refused as a district signal" in rep.district_note,
          "a district column that is 7 on every row is refused, and the run "
          "continues  <-- pinned defect")
    check([p.verdict for p in rep.pairs]
          == [DRIFT, DRIFT, CONFIRMED, REUSED],
          "and escalates nothing, because it cannot tell districts apart")
    rep = dlayers([("A", 0.0, 0.0, "7"), ("B", 0.0, 0.0, "9")],
                  [("A", 0.0, 300.0, "8"), ("B", 0.0, 300.0, "9")])
    check([p.verdict for p in rep.pairs] == [DRIFT, DRIFT]
          and rep.district_note is None,
          "no district column named means no district is ever compared")
    rep = dlayers([("A", None, None, "7"), ("B", 0.0, 0.0, "9")],
                  [("A", 0.0, 0.0, "8"), ("B", 0.0, 0.0, "9")],
                  district_column="DISTRICT")
    check(rep.pairs[0].verdict == UNVERIFIED and rep.pairs[0].district_note,
          "a pair with no coordinate stays UNVERIFIED and is noted, never "
          "condemned on the district alone")

    # ---- v2: the mutual-nearest rematch, for the rows the key failed
    pool = [Row(0, "right", "U-1", 0.0, 10.0), Row(1, "right", "U-2", 0.0, 30.0)]
    origin = Row(0, "left", "H-1", 0.0, 0.0)
    check(nearest(origin, pool) == (pool[0], 10.0),
          "nearest finds the single closest row and its distance")
    check(nearest(origin, []) == (None, None), "an empty pool has no nearest")
    check(nearest(Row(0, "left", "H-1", None, None), pool) == (None, None),
          "a row with no coordinate has no nearest")
    check(nearest(origin, [Row(0, "right", "U-0", None, 0.0)] + pool)[0]
          is pool[0], "a pooled row with no coordinate is skipped")
    tie = [Row(0, "right", "U-1", 0.0, 10.0), Row(1, "right", "U-2", 10.0, 0.0)]
    check(nearest(origin, tie) == (None, None),
          "a tie at the nearest distance answers nobody  <-- pinned defect")
    check(nearest(origin, tie + [Row(2, "right", "U-3", 0.0, 5.0)])[0]
          is not None, "a strictly closer row breaks an earlier tie")

    # The recycled number from the story. H-3's key points at a row 250000
    # units away, and its real partner sits 13 units off under another key.
    story_left = [("H-1", 0.0, 0.0, None), ("H-3", 5000.0, 5000.0, None)]
    story_right = [("H-1", 0.0, 51.0, None), ("H-3", 5000.0, 255000.0, None),
                   ("W-3", 5012.0, 5005.0, None)]
    rep = layers(story_left, story_right)
    check(rep.counts[CANDIDATE] == 0 and rep.candidates == [],
          "the rematch is off by default, so v1 classes are unchanged")
    check(not any(l.startswith("  CANDIDATE") for l in describe(rep)),
          "and a run that never looked prints no CANDIDATE count")
    rep = layers(story_left, story_right, rematch_distance=50.0)
    check(len(rep.candidates) == 1,
          "the rematch proposes one candidate for the recycled number")
    cand = rep.candidates[0]
    check(cand.left.key == "H-3" and cand.right.key == "W-3",
          "the recycled number is proposed against its real partner, not the "
          "object its key points at  <-- pinned defect")
    check(cand.distance == 13.0 and cand.verdict == CANDIDATE,
          "at the 13 units that separate them")
    check(rep.right[1].verdict == REUSED,
          "the far row the key pointed at stays REUSED")
    check(gate(rep) == 1,
          "and the join on the key still fails the gate, because it was "
          "still wrong  <-- pinned defect")
    check(rep.counts[CANDIDATE] == 2 and rep.counts[REUSED] == 1,
          "both rows of the candidate move to CANDIDATE")
    check(sum(rep.counts.values()) == rep.total == rep.classified,
          "the six classes sum to the input row count  <-- pinned defect")
    check("not a match" in cand.reason and "left line 3" in cand.reason
          and "right line 4" in cand.reason,
          "a candidate says it is a proposal and names both lines")
    lines = describe(rep)
    check(any(l == "  CANDIDATE        2" for l in lines),
          "a run that rematched prints the CANDIDATE count")
    check(any(l.startswith("rematch: 1 candidate pair(s), each the other's "
                           "nearest within 50.0 ft") for l in lines),
          "and the rematch distance it used")
    check(any(l == "  H-3 -> W-3  13.0 ft apart, left line 3, right line 4"
              for l in lines),
          "each candidate is listed with both keys, both lines and the "
          "distance")

    check(layers(story_left, story_right, rematch_distance=12.0).candidates
          == [], "a rematch distance under the gap proposes nothing")
    crowd = layers([("A", 0.0, 0.0, None), ("B", 0.0, 20.0, None)],
                   [("Z", 0.0, 8.0, None)], rematch_distance=50.0)
    check(len(crowd.candidates) == 1 and crowd.candidates[0].left.key == "A",
          "two rows near one point yield one candidate, the mutual one"
          "  <-- pinned defect")
    check(crowd.left[1].verdict == UNMATCHED,
          "and the other row stays UNMATCHED rather than claiming it too")
    check(layers([("A", 0.0, 10.0, None), ("B", 10.0, 0.0, None)],
                 [("Z", 0.0, 0.0, None)], rematch_distance=50.0).candidates
          == [], "a point two rows are equally near proposes nothing")
    rep = layers([("A", 0.0, 0.0, None)], [("A", 0.0, 51.0, None),
                                          ("Z", 0.0, 1.0, None)],
                 rematch_distance=50.0)
    check(rep.candidates == [] and rep.right[1].verdict == UNMATCHED,
          "a CONFIRMED row is never offered to the rematch")
    rep = layers([("A", None, None, None), ("", 0.0, 0.0, None)],
                 [("Z", 0.0, 0.0, None)], rematch_distance=50.0)
    check(rep.candidates == [],
          "an UNMATCHED row with no coordinate and an UNVERIFIED row with no "
          "key are never rematched")
    rep = layers(story_left, story_right, rematch_distance=0.0)
    check(rep.candidates == [], "a rematch distance of zero is legal")
    raises(lambda: layers([], [], rematch_distance=-1.0),
           "a negative rematch distance raises")
    raises(lambda: layers([], [], rematch_distance=float("nan")),
           "a NaN rematch distance raises")
    raises(lambda: layers([], [], rematch_distance=151.0),
           "a rematch distance above the confirm distance raises"
           "  <-- pinned defect")
    rep = layers([("H-1", 0.0, 0.0, None), ("H-1", 5.0, 5.0, None)],
                 [("W-1", 0.0, 1.0, None)], rematch_distance=50.0)
    check(rep.refused and rep.candidates == [] and rep.classified == 0,
          "a refused run rematches nothing")
    many = layers([("H-%d" % i, i * 1000.0, 0.0, None) for i in range(12)]
                  + [("Z", 99000.0, 0.0, None)],
                  [("W-%d" % i, i * 1000.0, 5.0, None) for i in range(12)]
                  + [("Z", 99000.0, 5.0, None)], rematch_distance=50.0)
    lines = describe(many, sample=10)
    check(len(many.candidates) == 12
          and sum(1 for l in lines if " -> " in l) == 10
          and "  ...and 2 more" in lines,
          "candidates are sampled and counted like every other list")
    check(gate(many) == 0,
          "a CANDIDATE alone never fails the gate: it is a proposal")
    check(gate(layers([("H-0", 0.0, 0.0, None)], [("W-0", 0.0, 5.0, None)],
                      rematch_distance=50.0)) == 4,
          "but a CANDIDATE with no pair on a shared key compared nothing, "
          "which exits 4")
    check("  ...and 2 more" not in describe(layers(
        [("H-%d" % i, i * 1000.0, 0.0, None) for i in range(10)],
        [("W-%d" % i, i * 1000.0, 5.0, None) for i in range(10)],
        rematch_distance=50.0), sample=10),
          "and ten candidates at a sample of ten print no ...and line")

    # ---- the district escalation feeds the rematch pool
    rep = dlayers([("H-1", 0.0, 0.0, "7"), ("H-6", 0.0, 0.0, "7")],
                  [("H-1", 0.0, 10.0, "7"), ("H-6", 0.0, 350.0, "8"),
                   ("W-6", 3.0, 4.0, "7")],
                  district_column="DISTRICT", rematch_distance=50.0)
    check(rep.pairs[1].verdict == REUSED and len(rep.candidates) == 1
          and rep.candidates[0].right.key == "W-6",
          "an escalated pair's row is rematched to the partner in its own "
          "district")

    # ---- the crosswalk, which only --apply writes
    rep = dlayers(spread + [("E", 0.0, 0.0, "7"), ("F", 9e4, 0.0, "7")],
                  moved + [("E", None, None, "7"), ("W", 9e4, 4.0, "7")],
                  district_column="DISTRICT", rematch_distance=50.0)
    walk = crosswalk_rows(rep)
    check([(r["left_key"], r["right_key"], r["tier"]) for r in walk]
          == [("B", "B", DRIFT), ("C", "C", CONFIRMED), ("G", "G", CONFIRMED),
              ("F", "W", CANDIDATE)],
          "the crosswalk holds CONFIRMED, DRIFT and CANDIDATE links, pairs "
          "first")
    check(not any(r["tier"] in (REUSED, UNVERIFIED, UNMATCHED) for r in walk),
          "and never a REUSED, UNVERIFIED or UNMATCHED row  <-- pinned defect")
    check(walk[1]["left_district"] == "7" and walk[1]["right_district"] == "8",
          "each link carries the district on both sides")
    check(walk[1]["distance"] == "51.000" and walk[1]["left_line"] == 4,
          "and its distance and both CSV lines")
    check(all(r["reason"] for r in walk), "and the reason for its tier")
    check(crosswalk_rows(layers([("A", 0.0, 0.0, None)],
                                [("A", 0.0, 1.0, None)]))[0]["left_district"]
          == "", "a run with no district column writes the district empty")
    out_rows = classified_rows(layers(story_left, story_right,
                                      rematch_distance=50.0))
    check(out_rows[1]["class"] == CANDIDATE and out_rows[1]["distance"]
          == "13.000",
          "an --out row that rematched carries its candidate's distance, not "
          "the 250000 that disproved its key")
    check(out_rows[3]["class"] == REUSED
          and out_rows[3]["distance"] == "250000.000",
          "while the row its key pointed at keeps the disproved distance")

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
    check(a.district_field is None and a.right_district_field is None,
          "--district-field and --right-district-field default to off")
    check(a.rematch_distance is None, "--rematch-distance defaults to off")
    check(a.crosswalk is None, "--crosswalk defaults to nothing written")
    check(_parse(["l.csv", "r.csv", "--district-field", "D"]).district_field
          == "D", "--district-field is read")
    check(_parse(["l.csv", "r.csv", "--right-district-field", "RD"]
                 ).right_district_field == "RD",
          "--right-district-field is read")
    check(_parse(["l.csv", "r.csv", "--rematch-distance", "50"]
                 ).rematch_distance == 50.0, "--rematch-distance is read")
    check(_parse(["l.csv", "r.csv", "--crosswalk", "x.csv"]).crosswalk
          == "x.csv", "--crosswalk is read")
    cw_err = io.StringIO()
    cw_refused = False
    with contextlib.redirect_stderr(cw_err):
        try:
            _parse(["l.csv", "r.csv", "--cross", "x.csv"])
        except SystemExit:
            cw_refused = True
    check(cw_refused and "unrecognized arguments" in cw_err.getvalue(),
          "a unique prefix of --crosswalk is refused too")
    # With abbreviations on, --ap parses as --apply and writes the file.
    ap_err = io.StringIO()
    ap_refused = False
    with contextlib.redirect_stderr(ap_err):
        try:
            _parse(["l.csv", "r.csv", "--ap"])
        except SystemExit:
            ap_refused = True
    check(ap_refused and "unrecognized arguments" in ap_err.getvalue(),
          "a unique prefix of --apply is refused, not read as --apply"
          "  <-- pinned defect")

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
    args = _parse(["l.csv", "r.csv", "--district-field", "DISTRICT"])
    check(side_fields(args, "left")["district"] == "DISTRICT"
          and side_fields(args, "right")["district"] == "DISTRICT",
          "one --district-field names the column on both sides")
    args = _parse(["l.csv", "r.csv", "--district-field", "DISTRICT",
                   "--right-district-field", "DIST"])
    check(side_fields(args, "right")["district"] == "DIST"
          and side_fields(args, "left")["district"] == "DISTRICT",
          "--right-district-field overrides the right side only")
    check(missing_columns(side_fields(args, "right"), ["ASSET_ID", "X", "Y"])
          == ["DIST"], "a named district column that is absent is missing")
    dist_rows = extract_rows([{"DIST": " 8 "}, {}], "right",
                             side_fields(args, "right"))
    check(dist_rows[0].district == "8" and dist_rows[1].district == "",
          "a district is trimmed as it is read, and a missing cell is empty")
    check(extract_rows([{}], "left", side_fields(_parse(["l.csv", "r.csv"]),
                                                 "left"))[0].district is None,
          "no district column means no district value")

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

    # ---- real files on disk. Everything below writes into tmp, which
    # self_test deletes again. No network, no database, no credentials.

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
        "H-3,621000.0,1831000.0,1\n"
        "H-4,,,1\n"
        "U-9,622200.0,1582200.0,1\n")
    fire_csv = tmpfile("fire.csv", fire_text)
    util_csv = tmpfile("utilities.csv", util_text)
    base = [fire_csv, util_csv, "--id-field", "HYDRANT_NO"]

    code, out, err = run_cli(base)
    check(code == 1, "the two hydrant layers fail the gate")
    check("VERDICT: 1 match(es) disproved" in out,
          "because one match is disproved by the geometry")
    check("H-3  250000.0 ft apart, left line 4, right line 4" in out,
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
    check("250000.0 m apart" in out,
          "--units relabels every distance and converts nothing")
    code, out, err = run_cli(base + ["--sample", "0"])
    check("H-3  250000.0" not in out and "VERDICT: 1 match(es)" in out,
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
    live_text = util_text.replace("H-3,621000.0,1831000.0,1",
                                  "H-3,621000.0,1831000.0,0")
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
        "H-3,621000.0,1831000.0,1\n")
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
    check("H-3  250000.0 ft apart" in out,
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
    check(written[2]["key"] == "H-3" and written[2]["distance"] == "250000.000",
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

    # ---- v2 through the CLI. The same layers, with a district column on
    # each side, H-6 drifting 350 ft across a district line, and W-3: the
    # real partner of the recycled H-3, 13 ft from it under another key.
    fire2_text = (
        "HYDRANT_NO,X,Y,OPERABLE,DISTRICT\n"
        "H-1,620100.0,1580200.0,1,7\n"
        "H-2,620500.0,1580600.0,1,7\n"
        "H-3,621000.0,1581000.0,1,7\n"
        "H-4,621400.0,1581400.0,1,7\n"
        "H-5,621800.0,1581800.0,1,7\n"
        "H-6,622600.0,1582600.0,1,7\n")
    util2_text = (
        "HYDRANT_NO,X,Y,OPERABLE,DIST\n"
        "H-1,620100.0,1580251.0,1,7\n"
        "H-2,620500.0,1580900.0,1,7\n"
        "H-3,621000.0,1831000.0,1,9\n"
        "H-4,,,1,7\n"
        "U-9,622200.0,1582200.0,1,7\n"
        "H-6,622600.0,1582950.0,1,8\n"
        "W-3,621012.0,1581005.0,1,7\n")
    fire2 = tmpfile("fire2.csv", fire2_text)
    util2 = tmpfile("utilities2.csv", util2_text)
    base2 = [fire2, util2, "--id-field", "HYDRANT_NO"]
    district2 = ["--district-field", "DISTRICT", "--right-district-field",
                 "DIST"]

    code, out, err = run_cli(base2)
    check(code == 1 and "VERDICT: 1 match(es) disproved" in out
          and "  H-6  350.0 ft apart" in out,
          "without a district column H-6 is only a drift pair, as in v1")
    code, out, err = run_cli(base2 + district2)
    check(code == 1 and "VERDICT: 2 match(es) disproved" in out,
          "with the district column H-6 is disproved too  <-- pinned defect")
    check("  H-6  350.0 ft apart, left line 7, right line 7, districts '7' "
          "and '8'" in out, "and is listed with both of its districts")
    check("DISTRICT  district column 'DISTRICT' holds 3 distinct" in out,
          "the run first proves the district column has a signal")
    code, out, err = run_cli(base2 + ["--district-field", "OPERABLE"])
    check("refused as a district signal" in out
          and "VERDICT: 1 match(es) disproved" in out,
          "a district column that is 1 on every row is refused and escalates "
          "nothing  <-- pinned defect")
    code, out, err = run_cli(base2 + district2 + ["--rematch-distance", "50"])
    check("  H-3 -> W-3  13.0 ft apart, left line 4, right line 8" in out,
          "the rematch proposes W-3 for the recycled H-3  <-- pinned defect")
    check("rows classified: 13 of 13" in out
          and ("  %-11s %6d" % (CANDIDATE, 2)) in out,
          "and the six classes still cover all thirteen rows")
    check(code == 1, "and the key join still fails the gate")
    code, out, err = run_cli(base2 + ["--rematch-distance", "200"])
    check(code == 64 and "above the confirm distance" in err,
          "a rematch distance above the confirm distance is a usage error")
    check(run_cli(base2 + ["--rematch-distance", "-1"])[0] == 64,
          "so is a negative one")
    code, out, err = run_cli(base2 + ["--right-district-field", "DIST"])
    check(code == 64 and "needs --district-field" in err,
          "a district on the right side alone is a usage error"
          "  <-- pinned defect")
    code, out, err = run_cli(base2 + ["--district-field", "WARD"])
    check(code == 64 and "WARD" in err,
          "a district column that is not in the CSV is named in the error")

    walk_path = os.path.join(tmp, "crosswalk.csv")
    code, out, err = run_cli(base2 + district2 + ["--rematch-distance", "50",
                                                  "--crosswalk", walk_path])
    check(not os.path.exists(walk_path) and "was not written" in out,
          "--crosswalk without --apply writes nothing at all"
          "  <-- pinned defect")
    code, out, err = run_cli(base2 + district2 + ["--rematch-distance", "50",
                                                  "--crosswalk", walk_path,
                                                  "--apply"])
    check(code == 1 and "wrote %s" % walk_path in out,
          "--apply writes the crosswalk alone and keeps the gate's exit code")
    walk, walk_header = read_csv(walk_path)
    check(walk_header == list(CROSSWALK_COLUMNS),
          "the crosswalk carries the nine columns it documents")
    check([(r["left_key"], r["right_key"], r["tier"]) for r in walk]
          == [("H-1", "H-1", CONFIRMED), ("H-2", "H-2", DRIFT),
              ("H-3", "W-3", CANDIDATE)],
          "and holds the three links worth keeping, with H-3 against W-3 and "
          "never against the row its key points at  <-- pinned defect")
    check(walk[2]["distance"] == "13.000" and walk[2]["right_line"] == "8",
          "a candidate link carries its distance and its line")
    both_out = os.path.join(tmp, "both-classified.csv")
    both_walk = os.path.join(tmp, "both-crosswalk.csv")
    code, out, err = run_cli(base2 + ["--out", both_out,
                                      "--crosswalk", both_walk])
    check("%s and %s were not written" % (both_out, both_walk) in out,
          "--out and --crosswalk without --apply name both files skipped")
    code, out, err = run_cli(base2 + ["--out", both_out,
                                      "--crosswalk", both_walk, "--apply"])
    check(os.path.exists(both_out) and os.path.exists(both_walk),
          "--apply writes both when both are named")
    check(run_cli(base2 + ["--crosswalk", tmp, "--apply"])[0] == 2,
          "a crosswalk that cannot be written exits 2")
    refused_walk = os.path.join(tmp, "refused-crosswalk.csv")
    code, out, err = run_cli([dupe_csv, util_csv, "--id-field", "HYDRANT_NO",
                              "--crosswalk", refused_walk, "--apply"])
    check(code == 3 and not os.path.exists(refused_walk),
          "a refused run writes no crosswalk  <-- pinned defect")

    # ---- the exits a scheduled job reads
    check(run_cli([])[0] == 64, "a run with no layer is a usage error")
    code, out, err = run_cli(base + ["--out", out_path, "--ap"])
    check(code == 64 and "unrecognized arguments: --ap" in err,
          "--ap through main exits 64, not the 2 an unreadable file exits"
          "  <-- pinned defect")
    check(run_cli(["--help"])[0] == 0, "--help still exits 0")
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
    check(code == 4 and "rows classified: 5 of 5" in out
          and "NOTHING COMPARED" in out,
          "a layer with no rows classifies the other side, but compared "
          "nothing, so it exits 4 and not 0  <-- pinned defect")
    argv_before = sys.argv
    try:
        sys.argv = ["idreuse.py", fire_csv, util_csv, "--id-field",
                    "HYDRANT_NO", "--reuse-distance", "300000"]
        check(run_cli(None)[0] == 0,
              "main with no argv reads the arguments after the program name")
    finally:
        sys.argv = argv_before

    # ---- review round 1. Each block pins a defect that was reproduced first.

    # A run that compared nothing is not a clean run.
    comma_l = tmpfile("comma-l.csv", 'ID,X,Y\nH-1,"620,100.5","1,580,200.5"\n')
    comma_r = tmpfile("comma-r.csv", 'ID,X,Y\nH-1,"630,100.5","1,590,200.5"\n')
    code, out, err = run_cli([comma_l, comma_r, "--id-field", "ID"])
    check(code == 4 and "VERDICT: NOTHING COMPARED" in out
          and "no match was disproved" not in out,
          "coordinates with thousands separators are UNVERIFIED, so the run "
          "exits 4 and does not call itself clean  <-- pinned defect")
    spell_l = tmpfile("spell-l.csv", "ID,X,Y\nH-1,1000,1000\nH-2,2000,2000\n")
    spell_r = tmpfile("spell-r.csv", "ID,X,Y\nH1,1000,1000\nH2,2000,2000\n")
    code, out, err = run_cli([spell_l, spell_r, "--id-field", "ID"])
    check(code == 4 and "pairs: 0 on a shared key" in out
          and "NOTHING COMPARED" in out,
          "an ID spelled H-1 on one side and H1 on the other pairs nothing, "
          "so the run exits 4  <-- pinned defect")
    check(gate(layers([], [])) == 0 and not layers([], []).compared_nothing,
          "two empty layers are not a failed comparison, there was no row")
    check(layers([("H-1", 0.0, 0.0, None)],
                 [("H-1", 0.0, 9000.0, None)]).measured != [],
          "a measured pair is what separates a clean run from one that "
          "compared nothing")
    check(gate(layers([("H-1", 0.0, 0.0, None)],
                      [("H-1", 0.0, 51.0, None), ("H-2", 0.0, 0.0, None)])) == 0,
          "and one measured pair among unmatched rows is still a clean exit 0")

    # The district column must share a vocabulary across the two sides.
    spread7 = [("A", 0.0, 0.0, "7"), ("B", 0.0, 0.0, "8"),
               ("C", 0.0, 0.0, "7")]
    for label, other in (("D7", ("D7", "D8", "D7")), ("07", ("07", "08", "07"))):
        rep = dlayers(spread7, [("A", 0.0, 300.0, other[0]),
                                ("B", 0.0, 300.0, other[1]),
                                ("C", 0.0, 300.0, other[2])],
                      district_column="DISTRICT")
        check("share no value" in rep.district_note
              and [p.verdict for p in rep.pairs] == [DRIFT] * 3
              and gate(rep) == 0,
              "'7' on one side and '%s' on the other share no value, so the "
              "column is refused and nothing is escalated  "
              "<-- pinned defect" % label)
    rep = dlayers(spread7, [("A", 0.0, 300.0, ""), ("B", 0.0, 300.0, ""),
                            ("C", 0.0, 300.0, "")],
                  district_column="DISTRICT")
    check("right side has no district value" in rep.district_note
          and gate(rep) == 0,
          "a right column that is blank on every row is refused by name, "
          "not passed because the left side has two values  "
          "<-- pinned defect")
    check("left side has no district value" in dlayers(
        [("A", 0.0, 0.0, ""), ("B", 0.0, 0.0, "")],
        [("A", 0.0, 300.0, "7"), ("B", 0.0, 300.0, "8")],
        district_column="DISTRICT").district_note,
          "and a blank left column is named as the left side")
    usable, message = district_signal(["", None, " "], "DISTRICT")
    check(usable is False and "blank on all 3 row(s)" in message,
          "a district column that is blank everywhere is refused  "
          "<-- pinned defect")
    check(district_signal(["7", "", ""], "DISTRICT")[0] is False,
          "a blank is a hole, so '7' and blanks hold one value, not two")
    rep = dlayers([("A", 0.0, 0.0, "7"), ("B", 0.0, 0.0, "8")],
                  [("A", 0.0, 300.0, "8"), ("B", 0.0, 300.0, "7")],
                  district_column="DISTRICT")
    check("no pair on a shared key has the same district" in rep.district_note
          and gate(rep) == 0,
          "two columns that share values but never agree on a pair are "
          "refused  <-- pinned defect")
    rep = dlayers([("A", 0.0, 0.0, "7"), ("B", 0.0, 0.0, "8"),
                   ("C", 0.0, 0.0, "7"), ("D", 0.0, 0.0, "7")],
                  [("A", 0.0, 10.0, "8"), ("B", 0.0, 10.0, "7"),
                   ("C", 0.0, 10.0, "7"), ("D", 0.0, 300.0, "8")],
                  district_column="DISTRICT")
    check("2 of the 3 pairs the geometry confirms" in rep.district_note
          and rep.pairs[3].verdict == DRIFT,
          "when most pairs the geometry confirms disagree on district the "
          "codes do not match, and the drift pair is not escalated  "
          "<-- pinned defect")
    rep = dlayers([("A", 0.0, 0.0, "7"), ("B", 0.0, 0.0, "8"),
                   ("C", 0.0, 0.0, "7")],
                  [("A", 0.0, 150.0, "8"), ("B", 0.0, 150.0, "7"),
                   ("C", 0.0, 10.0, "7")],
                  district_column="DISTRICT")
    check("2 of the 3 pairs the geometry confirms" in rep.district_note,
          "a pair exactly at the confirm distance counts as confirmed here "
          "too  <-- pinned defect")
    rep = dlayers([("A", 0.0, 0.0, "7"), ("B", None, None, "8"),
                   ("C", 0.0, 0.0, "7")],
                  [("A", 0.0, 10.0, "7"), ("B", 0.0, 10.0, "7"),
                   ("C", 0.0, 300.0, "8")],
                  district_column="DISTRICT")
    check(rep.district_note is not None
          and "refused" not in rep.district_note
          and rep.pairs[2].verdict == REUSED,
          "a pair with no coordinate is not counted as confirmed, so it "
          "cannot sink the column")
    many_d = [("K%d" % i, 0.0, 0.0, "7") for i in range(21)]
    rep = dlayers(many_d, [("K%d" % i, 0.0, 10.0, "8" if i < 10 else "7")
                           for i in range(21)], district_column="DISTRICT")
    lines = describe(rep, sample=10)
    check(rep.district_note is not None and "refused" not in rep.district_note
          and sum(1 for l in lines if "different districts on DISTRICT" in l)
          == 10 and not any(l.startswith("  ...and") for l in lines),
          "ten district disagreements at a sample of ten print no ...and "
          "line at all, not even a 0 more")
    check("  ...and 1 more" in describe(rep, sample=9),
          "and a sample of nine counts exactly one unlisted")
    refused_d = dlayers(spread7, [("A", 0.0, 300.0, "D7"),
                                  ("B", 0.0, 300.0, "D8"),
                                  ("C", 0.0, 300.0, "D7")],
                        district_column="DISTRICT")
    check(not any(p.district_note for p in refused_d.pairs),
          "a refused district column leaves no pair carrying a district "
          "note")
    check(_few(set("abcde")) == "'a', 'b', 'c', 'd', 'e'"
          and _few(set("abcdef")) == "'a', 'b', 'c', 'd', 'e', ...",
          "a message lists five values and then says there are more")
    check("holds the single value '7'" in dlayers(
        [("A", 0.0, 0.0, "7")], [("A", 0.0, 300.0, "")],
        district_column="DISTRICT").district_note,
          "a column that is '7' on one side and blank on the other is a "
          "single value, and says so rather than naming the blank side")

    # The line a row is on in its own file.
    nl_path = tmpfile("lines.csv",
                      'ID,X,Y,NOTE\n'
                      'A,1,2,"two\nlines"\n'
                      '\n'
                      'B,3,4,x\n'
                      'C,5,6,"a\r\nb\r\nc"\r\n'
                      'D,7,8,x,"e\nf"\n'
                      'E,9,10\n')
    nl_rows, nl_header = read_csv(nl_path)
    check([r.line for r in nl_rows] == [2, 5, 6, 9, 11],
          "a blank line and a quoted newline do not move the reported line "
          "off the file  <-- pinned defect")
    check([r.key for r in extract_rows(nl_rows, "left",
                                       {"key": "ID", "x": "X", "y": "Y",
                                        "status": None})][-1] == "E"
          and extract_rows(nl_rows, "left", {"key": "ID", "x": "X", "y": "Y",
                                             "status": None})[-1].line == 11,
          "and extract_rows carries that line onto the row")
    check(Row(0, "left", "H-1", 0.0, 0.0, line=9).line == 9
          and Row(3, "left", "H-1", 0.0, 0.0).line == 5,
          "a row built with no line falls back to index + 2")
    gap_l = tmpfile("gap-l.csv", 'ID,X,Y,NOTE\nH-1,0,0,"a\nb"\n\n\nH-3,0,0,x\n')
    gap_r = tmpfile("gap-r.csv", "ID,X,Y\nH-3,0,9000\n")
    code, out, err = run_cli([gap_l, gap_r, "--id-field", "ID"])
    check("H-3  9000.0 ft apart, left line 6, right line 2" in out,
          "the report names the line the operator will find the row on  "
          "<-- pinned defect")

    # The two boundaries of the rematch.
    edge = layers([("A", 0.0, 0.0, None)], [("B", 0.0, 50.0, None)],
                  rematch_distance=50.0)
    check(len(edge.candidates) == 1,
          "a candidate exactly at the rematch distance is found, the limit "
          "is inclusive  <-- pinned defect")
    check(layers([("A", 0.0, 0.0, None)], [("B", 0.0, 50.0, None)],
                 rematch_distance=49.999).candidates == [],
          "and one a hair beyond it is not")
    check(layers([("A", 0.0, 0.0, None)], [("B", 0.0, 1.0, None)],
                 rematch_distance=DEFAULT_CONFIRM_DISTANCE).candidates != [],
          "a rematch distance equal to the confirm distance is accepted  "
          "<-- pinned defect")
    raises(lambda: layers([], [], rematch_distance=150.001),
           "and one a hair above it is refused")

    # The CLI hand-offs that no other assertion could see fail.
    real_self_test = globals()["self_test"]
    try:
        globals()["self_test"] = lambda: 7
        handed = run_cli(["--self-test", "ignored.csv"])[0]
    finally:
        globals()["self_test"] = real_self_test
    check(handed == 7,
          "main returns the exit status of --self-test, so a red self-test "
          "fails CI  <-- pinned defect")
    code, out, err = run_cli([fire_csv, "--id-field", "HYDRANT_NO"])
    check(code == 64 and "two CSV layers are required" in err,
          "one layer is a usage error even when its columns exist  "
          "<-- pinned defect")
    code, out, err = run_cli(base)
    plain = out.splitlines()
    check(not any(l.startswith(("STATUS", "DISTRICT")) for l in plain)
          and "Check only" not in out and "districts" not in out
          and "different districts" not in out
          and "disagree on the status column" not in out
          and not any(l.lstrip().startswith("...and") for l in plain),
          "a plain run prints no STATUS, DISTRICT, 'Check only' or ...and "
          "line  <-- pinned defect")
    check(not any(l.lstrip().startswith("...and")
                  for l in describe(layers(
                      [("H-%d" % i, i * 1000.0, 0.0, None) for i in range(10)],
                      [("W-%d" % i, i * 1000.0, 5.0, None) for i in range(10)],
                      rematch_distance=50.0),
                      sample=10)),
          "ten rematch candidates at a sample of ten print no ...and 0 more")
    code, out, err = run_cli(base2 + ["--rematch-distance", "200"])
    check(code == 64 and out == "",
          "a usage error prints nothing to stdout, so it cannot pass for the "
          "start of a good run  <-- pinned defect")
    code, out, err = run_cli(base + ["--right-status-field", "OPERABLE"])
    check(code == 64 and "needs --status-field" in err,
          "a right status column with no left one is a usage error, not "
          "silently ignored  <-- pinned defect")
    check(run_cli(base + ["--proj"])[0] == 64,
          "a prefix of --projected is refused like a prefix of --apply")

    # Degrees are not feet.
    deg_l = tmpfile("deg-l.csv", "ASSET_ID,X,Y\nH-1,-82.10,29.10\n")
    deg_r = tmpfile("deg-r.csv", "ASSET_ID,X,Y\nH-1,-81.50,29.80\n")
    code, out, err = run_cli([deg_l, deg_r])
    check(code == 64 and "look like degrees" in err and out == "",
          "two layers whose coordinates all fit longitude and latitude are "
          "refused, because 60 miles apart reads as CONFIRMED  "
          "<-- pinned defect")
    code, out, err = run_cli([deg_l, deg_r, "--projected"])
    check(code == 0 and "CONFIRMED" in out,
          "and --projected says they are a local grid and runs them")
    check(looks_like_degrees([]) is False
          and looks_like_degrees([Row(0, "left", "a", None, None)]) is False
          and looks_like_degrees([Row(0, "left", "a", 10.0, 20.0)]) is True
          and looks_like_degrees([Row(0, "left", "a", 10.0, 20.0),
                                  Row(1, "left", "b", 620100.0, 20.0)])
          is False
          and looks_like_degrees([Row(0, "left", "a", 10.0, 91.0)]) is False
          and looks_like_degrees([Row(0, "left", "a", -181.0, 5.0)]) is False
          and looks_like_degrees([Row(0, "left", "a", 180.0, -90.0)]) is True
          and looks_like_degrees([Row(0, "left", "a", None, 20.0),
                                  Row(1, "left", "b", 10.0, None)]) is False,
          "the degrees check reads the box inclusively, skips an unreadable "
          "row, and needs a coordinate to judge")

    # An output may not be an input, or the other output.
    before = [open(p, "rb").read() for p in (fire_csv, util_csv)]
    for flag, target in (("--out", fire_csv), ("--crosswalk", util_csv),
                         ("--out", os.path.join(tmp, ".", "fire.csv"))):
        code, out, err = run_cli(base + [flag, target, "--apply"])
        check(code == 64 and "is also an input layer" in err,
              "%s naming an input layer is refused  <-- pinned defect" % flag)
    check(run_cli(base + ["--out", fire_csv])[0] == 64,
          "and is refused on a dry run too, before --apply is added")
    check(before == [open(p, "rb").read() for p in (fire_csv, util_csv)],
          "and neither input layer was touched")
    same_out = os.path.join(tmp, "same.csv")
    code, out, err = run_cli(base + ["--out", same_out, "--crosswalk",
                                     same_out, "--apply"])
    check(code == 64 and "name the same file" in err
          and not os.path.exists(same_out),
          "--out and --crosswalk naming one file are refused and nothing is "
          "written  <-- pinned defect")

    # A character the console cannot encode must not turn a verdict into a
    # crash. The reconfigure is proved in process, and the crash itself in a
    # child whose stdout is piped through ascii.
    calls = []

    class Reconfigurable(io.StringIO):
        def reconfigure(self, **kw):
            calls.append(kw)

    with contextlib.redirect_stdout(Reconfigurable()), \
            contextlib.redirect_stderr(Reconfigurable()):
        main(["--help"])
    check(calls == [{"errors": "replace"}] * 2,
          "main reconfigures stdout and stderr to replace what they cannot "
          "encode")
    uni_l = tmpfile("uni-l.csv", "ASSET_ID,X,Y\nH-\u0141 1,100000,100000\n")
    uni_r = tmpfile("uni-r.csv", "ASSET_ID,X,Y\nH-\u0141 1,109000,100000\n")
    child = subprocess.run(
        [sys.executable, os.path.abspath(__file__), uni_l, uni_r],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, PYTHONIOENCODING="ascii"))
    check(child.returncode == 1 and b"H-? 1" in child.stdout
          and b"Traceback" not in child.stderr
          and b"VERDICT: 1 match(es) disproved" in child.stdout,
          "a key the console cannot encode prints as ? and the verdict still "
          "arrives, instead of a traceback and exit 1  <-- pinned defect")

    # A crash is not a finding.
    real_reconcile = globals()["reconcile"]

    def boom(*args, **kw):
        raise RuntimeError("probe")

    try:
        globals()["reconcile"] = boom
        code, out, err = run_cli(base)
    finally:
        globals()["reconcile"] = real_reconcile
    check(code == 70 and "unexpected RuntimeError: probe" in err
          and "Traceback" in err and "VERDICT" not in out,
          "an unexpected exception exits 70, not the 1 that means a match "
          "was disproved  <-- pinned defect")

    # The temp directory goes even when the body raises.
    seen = []

    def exploding(path):
        seen.append(path)
        raise RuntimeError("probe")

    real_body = globals()["_self_test"]
    try:
        globals()["_self_test"] = exploding
        try:
            self_test()
        except RuntimeError:
            seen.append("raised")
    finally:
        globals()["_self_test"] = real_body
    check(len(seen) == 2 and seen[1] == "raised"
          and not os.path.exists(seen[0]),
          "a self-test that raises still removes its temp directory  "
          "<-- pinned defect")

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

    def summary(passed_count, failures):
        print("-" * 68)
        total = passed_count + len(failures)
        if failures:
            print("%d assertions, %d failed" % (total, len(failures)))
            for f in failures:
                print("  FAILED: %s" % f)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    # The footer on its red path. A footer that could only print 0 failed
    # would hide every FAIL line above it from anyone who reads the last line.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1"
          "  <-- pinned defect")

    # ---- importing the module runs nothing. Another script may import it
    # for the core, and must not start a reconciliation.
    spec = importlib.util.spec_from_file_location(
        "idreuse_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    # No bytecode cache, so the self-test writes nothing outside its own
    # temporary directory.
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(buf.getvalue() == "" and imported.gate(imported.reconcile([], []))
          == 0, "importing the module prints nothing and exposes the core")

    return summary(passed[0], failed)


# ----------------------------------------------------------------------- cli

class _Record(dict):
    """One CSV row as a dict, plus the line of the file it starts on."""
    line = None


def _breaks(value):
    """How many line breaks one parsed cell carries (a quoted newline)."""
    cells = value if isinstance(value, list) else [value]
    return sum(len(_BREAK.findall(c)) for c in cells if c)


def looks_like_degrees(rows):
    """True when every usable coordinate fits longitude and latitude.

    Degrees exported into X and Y are the commonest wrong input, and they
    fail silently: every pair is a fraction of a degree apart, so every pair
    is CONFIRMED against a threshold of feet. A projected layer in feet or
    metres leaves the +-180 and +-90 box at once; a local grid whose origin
    is at the site can stay inside it, which --projected says is on purpose.
    """
    seen = False
    for row in rows:
        if row.x is None or row.y is None:
            continue
        seen = True
        if abs(row.x) > 180.0 or abs(row.y) > 90.0:
            return False
    return seen


def read_csv(path):
    """Raw rows and the header of a CSV, as dicts with a .line attribute.

    The line is the one the record starts on. DictReader skips a blank line
    without saying so and counts a quoted field with a newline in it as one
    row, so index + 2 drifts away from the file after either. The reader's own
    line_num is the line the record ends on; the breaks inside its cells
    walk it back to the start.
    """
    # utf-8-sig, because Esri's Table To Table and Excel both write a UTF-8
    # BOM. Read as plain UTF-8 the first header cell arrives as a BOM with
    # HYDRANT_NO after it, the anchor column reads as absent on every row, and
    # the run stops for a reason nothing in the output explains.
    with open(path, "r", newline="", encoding="utf-8-sig",
              errors="replace") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for raw in reader:
            record = _Record(raw)
            record.line = reader.line_num - sum(_breaks(v)
                                                for v in raw.values())
            rows.append(record)
        return rows, list(reader.fieldnames or [])


def side_fields(args, side):
    """The column names one side is read with. A district only when named."""
    if side == "right":
        fields = {
            "key": args.right_id_field or args.id_field,
            "x": args.right_x_field or args.x_field,
            "y": args.right_y_field or args.y_field,
            "status": args.right_status_field or args.status_field,
        }
        district = args.right_district_field or args.district_field
    else:
        fields = {"key": args.id_field, "x": args.x_field, "y": args.y_field,
                  "status": args.status_field}
        district = args.district_field
    if district:
        fields["district"] = district
    return fields


def missing_columns(fields, header):
    """Named columns that are not in this header, in the order they are read.

    Every one of them is required. Without the anchor there is no join, and
    without a coordinate there is no geometry to disprove it with, so a
    warning would leave the tool reporting UNVERIFIED on every row and looking
    like a data problem rather than a wrong column name.
    """
    names = [fields["key"], fields["x"], fields["y"]]
    for optional in ("status", "district"):
        if fields.get(optional):
            names.append(fields[optional])
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
        district = None
        if fields.get("district"):
            value = raw.get(fields["district"])
            district = "" if value is None else str(value).strip()
        out.append(Row(index, side,
                       "" if key is None else str(key).strip(),
                       to_number(raw.get(fields["x"])),
                       to_number(raw.get(fields["y"])),
                       status, district, getattr(raw, "line", None)))
    return out


def _write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_classified(path, report):
    """Write one row per input row, with the class and the separation."""
    _write_csv(path, OUT_COLUMNS, classified_rows(report))


def write_crosswalk(path, report):
    """Write one row per link worth keeping, with its tier."""
    _write_csv(path, CROSSWALK_COLUMNS, crosswalk_rows(report))


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="idreuse.py",
        description="Reconcile two asset layers on a shared ID and refuse "
                    "every match the geometry disproves.",
        epilog="Both layers must be in one projected CRS. Nothing is written "
               "without --apply.",
        allow_abbrev=False,
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
    ap.add_argument("--district-field", dest="district_field",
                    help="the district each row is in, on both sides. A drift "
                         "pair whose sides disagree is escalated to REUSED.")
    ap.add_argument("--right-district-field", dest="right_district_field",
                    help="the district column on the right side")
    ap.add_argument("--rematch-distance", dest="rematch_distance", type=float,
                    help="propose a CANDIDATE for an unmatched or REUSED row "
                         "when it and a row on the other side are each the "
                         "other's nearest within this distance. Off unless "
                         "named.")
    ap.add_argument("--projected", action="store_true",
                    help="say that the coordinates are projected, to skip the "
                         "check that refuses values that all fit longitude "
                         "and latitude")
    ap.add_argument("--out", help="path for the classification CSV")
    ap.add_argument("--crosswalk",
                    help="path for the crosswalk CSV: one row per CONFIRMED, "
                         "DRIFT or CANDIDATE link")
    ap.add_argument("--apply", action="store_true",
                    help="write --out and --crosswalk. Without this nothing "
                         "is written.")
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


def _tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode.

    A piped stdout on Windows is cp1252, and a key or district read from a
    file that is not UTF-8 arrives as U+FFFD, which cp1252 cannot write. The
    UnicodeEncodeError then exits 1 and the VERDICT line is never printed.
    A character that cannot be shown is printed as ? instead.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
        os.path.realpath(b))


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'a match was disproved'."""
    _tolerate_odd_characters()
    try:
        return _run(argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about the data." % (type(exc).__name__, exc),
              file=sys.stderr)
        traceback.print_exc()
        return 70


def _run(argv):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file
        # could not be read. A mistyped --ap is a usage error, so it gets 64.
        return 64 if exc.code == 2 else exc.code

    if args.self_test:
        return self_test()

    if not args.left or not args.right:
        print("error: two CSV layers are required. Use --self-test to verify "
              "the tool without them.", file=sys.stderr)
        return 64
    if args.sample < 0:
        print("error: --sample cannot be negative.", file=sys.stderr)
        return 64
    if args.apply and not (args.out or args.crosswalk):
        print("error: --apply needs --out or --crosswalk.", file=sys.stderr)
        return 64
    if args.right_status_field and not args.status_field:
        # The right column would be required to exist and then never read.
        print("error: --right-status-field needs --status-field for the left "
              "side.", file=sys.stderr)
        return 64
    if args.right_district_field and not args.district_field:
        # Only the right side would carry a district, so no pair could ever
        # disagree on one, and the run would look defended by a column it
        # never compared.
        print("error: --right-district-field needs --district-field for the "
              "left side.", file=sys.stderr)
        return 64

    outputs = [(flag, path) for flag, path in (("--out", args.out),
                                               ("--crosswalk", args.crosswalk))
               if path]
    for flag, path in outputs:
        # Checked on a dry run too: the run that would clobber a layer should
        # say so before --apply is added, not after. The input is read in full
        # before anything is written, and then it is gone.
        for layer in (args.left, args.right):
            if _same_file(path, layer):
                print("error: %s %s is also an input layer. Name a new file."
                      % (flag, path), file=sys.stderr)
                return 64
    if len(outputs) == 2 and _same_file(outputs[0][1], outputs[1][1]):
        print("error: --out and --crosswalk name the same file, so the second "
              "would replace the first.", file=sys.stderr)
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

    if not args.projected and looks_like_degrees(left + right):
        print("error: every coordinate fits longitude -180..180 and latitude "
              "-90..90, so these look like degrees. Distances in degrees mean "
              "nothing against a threshold in feet. Project both layers, or "
              "pass --projected if this is a local grid.", file=sys.stderr)
        return 64

    try:
        report = reconcile(left, right, args.confirm_distance,
                           args.reuse_distance, args.units,
                           args.status_field, args.district_field,
                           args.rematch_distance)
        lines = describe(report, args.sample)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64

    # After reconcile, so a usage error leaves stdout empty rather than
    # looking like the start of a run that went well.
    print("idreuse: %s %d row(s), %s %d row(s)"
          % (args.left, len(left), args.right, len(right)))
    for line in lines:
        print(line)

    targets = [(path, writer) for path, writer in
               ((args.out, write_classified), (args.crosswalk, write_crosswalk))
               if path]
    named = " and ".join(path for path, _ in targets)
    was = "were" if len(targets) > 1 else "was"
    if targets:
        if args.apply:
            if report.refused:
                print("\nRefused. %s %s not written, because nothing was "
                      "classified." % (named, was))
            else:
                for path, writer in targets:
                    # A traceback here exits 1, and 1 is the code a scheduled
                    # job reads as "a match was disproved". A write that
                    # failed is a different fact and gets its own exit code.
                    try:
                        writer(path, report)
                    except (IOError, OSError, csv.Error) as exc:
                        print("error: could not write %s: %s" % (path, exc),
                              file=sys.stderr)
                        return 2
                    print("\nwrote %s" % path)
        else:
            print("\nCheck only. %s %s not written. Re-run with --apply."
                  % (named, was))

    return gate(report)


if __name__ == "__main__":
    sys.exit(main())
