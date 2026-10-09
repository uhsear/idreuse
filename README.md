# idreuse

Reconcile two asset layers on a shared ID and refuse every match the geometry disproves. Refuses
the ID itself when it is not unique, and refuses a status or district column that holds one value
on every row. Optionally proposes the real partner of a recycled ID, and writes a crosswalk of the
links worth keeping.

Two departments track the same fire hydrants. The fire side recycles a hydrant number when a
hydrant is retired, so the number is not a permanent name for an object. The utility side joins
on that number. Most rows match, the join reports no error, and some of those matches now link a
live hydrant in one district to a retired record in another district entirely.

Nobody catches it, because there is nothing to catch. The join did what it was told. The two
rows agree on the only field the join compared, and the only evidence that the pair is wrong is
the distance between the two points, which no join computes.

The defence everybody reaches for first is the operable flag: compare only the live records. It
removes nothing, because the flag is set on every row on both sides. The filter runs, the count
does not change, and the run looks defended.

The distance alone does not finish the job either. A recycled number that moved 350 feet looks
like ordinary capture drift, and only the district each side sits in says it moved. And once a
match is disproved, the hydrant it should have matched is still out there under another number,
usually a few feet away.

```
$ python idreuse.py --self-test
idreuse self-test: no file, no network, no credentials
--------------------------------------------------------------------
PASS  a pair 51 units apart is CONFIRMED, which is the ordinary case
PASS  a pair 300 units apart is DRIFT, neither confirmed nor rejected
PASS  a pair 248940 units apart is REUSED, never a match  <-- pinned defect
...
PASS  a drift pair whose sides are in different districts is REUSED  <-- pinned defect
...
PASS  the recycled number is proposed against its real partner, not the object its key points at  <-- pinned defect
PASS  at the 13 units that separate them
PASS  the far row the key pointed at stays REUSED
PASS  and the join on the key still fails the gate, because it was still wrong  <-- pinned defect
...
PASS  two rows near one point yield one candidate, the mutual one  <-- pinned defect
...
PASS  and never a REUSED, UNVERIFIED or UNMATCHED row  <-- pinned defect
...
PASS  a unique prefix of --apply is refused, not read as --apply  <-- pinned defect
...
PASS  --ap through main exits 64, not the 2 an unreadable file exits  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  and raises() accepts the ValueError it is looking for
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
371 assertions, 0 failed
```

The full run prints all 371 assertions. Each `...` line above is where this block is cut.

## What already exists

The join itself is a solved problem. A pandas `merge` with `indicator=True` adds a `_merge`
column that says whether each row came from the left side, the right side or both
([pandas.merge](https://pandas.pydata.org/docs/reference/api/pandas.merge.html)). ArcGIS Add
Join and a SQL `LEFT JOIN` match the rows just as correctly. None of them measures the distance
between the two rows they paired, so none of them can say a match is wrong.

The rematch is not new either. ArcGIS Pro's
[Generate Near Table](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/analysis/generate-near-table.html)
is the tool to reach for first. It runs at every licence level and takes a search radius. By
default it writes the closest near feature for each input feature, as `IN_FID`, `NEAR_FID` and
`NEAR_DIST`, and it can rank several near features with `NEAR_RANK`. It answers the question
from the input side only. Its documentation describes no one-to-one rule and no rule for equal
distances, so nothing stops two input points from naming the same near point. This tool adds
the reverse check: a pair is proposed only when each point is the other's single nearest. Mutual nearest neighbours is an old idea
outside GIS too. [mnnpy](https://github.com/chriscainx/mnnpy) uses it to match cells between
batches of single-cell data.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database. Both layers are CSVs, which is what every asset layer can be exported as.

The self-test prints the same 371 assertions on Windows (Python 3.13.2), on Python 3.9.25, and
on Ubuntu (Python 3.12.3), and the Windows and Ubuntu outputs are identical line for line.
`coverage run --branch idreuse.py --self-test` reports 100 percent of lines and branches.

Both layers must already be in one projected CRS, in feet or in metres. The tool measures a
straight line between two points and converts nothing. Feed it latitude and longitude and it
returns a number in degrees, which no threshold here can read.

```
git clone https://github.com/uhsear/idreuse.git
```

## Usage

Export both layers to CSV with their coordinates in columns, then name the shared ID column.

```
python idreuse.py --self-test
python idreuse.py fire.csv utilities.csv --id-field HYDRANT_NO
python idreuse.py fire.csv utilities.csv --id-field HYDRANT_NO --right-id-field HYD_NUM
python idreuse.py fire.csv utilities.csv --confirm-distance 100 --reuse-distance 800
python idreuse.py fire.csv utilities.csv --status-field OPERABLE
python idreuse.py fire.csv utilities.csv --district-field DISTRICT --right-district-field DIST
python idreuse.py fire.csv utilities.csv --rematch-distance 50
python idreuse.py fire.csv utilities.csv --out classified.csv --crosswalk links.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `LEFT` | none | The first layer as a CSV. Required unless `--self-test`. |
| `RIGHT` | none | The second layer as a CSV. Required unless `--self-test`. |
| `--id-field` | `ASSET_ID` | The shared identifier column, on both sides. |
| `--x-field` | `X` | The X column, on both sides. |
| `--y-field` | `Y` | The Y column, on both sides. |
| `--status-field` | off | A live-or-retired column to read on both sides. Never filtered on. |
| `--district-field` | off | The district each row is in, on both sides. Escalates a cross-district drift pair. |
| `--right-id-field` | left's | The identifier column on the right side, when it differs. |
| `--right-x-field` | left's | The X column on the right side. |
| `--right-y-field` | left's | The Y column on the right side. |
| `--right-status-field` | left's | The status column on the right side. |
| `--right-district-field` | left's | The district column on the right side. Needs `--district-field`. |
| `--confirm-distance` | `150` | At or under this separation a match is CONFIRMED. |
| `--reuse-distance` | `500` | Above this separation a match is REUSED. |
| `--rematch-distance` | off | Propose a CANDIDATE when two leftover rows are each the other's nearest within this. |
| `--units` | `ft` | Label printed after every distance. Nothing is converted. |
| `--sample` | `10` | How many records to list under each heading. `0` lists none. |
| `--out` | off | Write the classification of every row to this CSV. Needs `--apply`. |
| `--crosswalk` | off | Write one row per link worth keeping to this CSV. Needs `--apply`. |
| `--apply` | off | Actually write. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. Takes no other flag. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is refused
with exit 64 instead of being read as `--apply`, and `--cross` is refused instead of being read
as `--crosswalk`.

## What it checks

**The check.** Every key that is on both sides pairs one row with one row. The separation between
the two points decides the pair:

| Class | Separation | What it means |
|---|---|---|
| `CONFIRMED` | at or under `--confirm-distance` | Two crews captured one object. |
| `DRIFT` | between the two distances | Neither confirmed nor disproved. A human looks at it. |
| `REUSED` | above `--reuse-distance` | One key, two objects. The join is wrong here. |
| `UNMATCHED` | no pair | The key is on one side only. |
| `UNVERIFIED` | no separation | No coordinate, or no ID, so the geometry proves nothing. |
| `CANDIDATE` | `--rematch-distance` only | The geometry proposes a partner under another key. |

Every input row from both files lands in exactly one class, and the counts add up to the number
of rows that went in. That holds with the rematch on too: a row that becomes a `CANDIDATE` leaves
`UNMATCHED` or `REUSED`, so it is never counted twice. `UNMATCHED` is counted apart from `REUSED`
on purpose. "This key is not over there" and "this key is over there on the wrong object" are
different failures, and a tool that pooled them would report one number for two problems.

The demo layers below are synthetic. `fire.csv` has six hydrants and `utilities.csv` has seven,
with the right side naming its columns differently.

```
$ python idreuse.py fire.csv utilities.csv --id-field HYDRANT_NO --right-id-field HYD_NUM --right-x-field POINT_X --right-y-field POINT_Y
idreuse: fire.csv 6 row(s), utilities.csv 7 row(s)
rows classified: 13 of 13
  CONFIRMED        2
  DRIFT            4
  REUSED           2
  UNMATCHED        3
  UNVERIFIED       2
pairs: 5 on a shared key, at confirm 150.0 ft and reuse 500.0 ft

matches the geometry disproves, worst first:
  H-3  248940.0 ft apart, left line 4, right line 4

matches in the drift band, for a human to look at:
  H-6  350.0 ft apart, left line 7, right line 7
  H-2  300.0 ft apart, left line 3, right line 3

VERDICT: 1 match(es) disproved by the geometry. Do not publish this join.
```

Five keys were on both sides. `H-3` is the recycled number: its two rows are 248,940 feet apart,
and a join is the only tool in the room that calls that a match. `H-6` looks like drift. The next
section shows that it is not.

**Refusal 1: an ID that is not unique.** Checked before anything else, and it stops the run.

```
$ python idreuse.py fire-dupe.csv utilities.csv --id-field HYDRANT_NO --right-id-field HYD_NUM --right-x-field POINT_X --right-y-field POINT_Y
idreuse: fire-dupe.csv 7 row(s), utilities.csv 7 row(s)
REFUSED   the anchor is not unique, so nothing was compared
      left key 'H-1' is on 2 rows: line 2, 8
      Fix the anchor or name a different column. A key two rows claim cannot say which row the other layer meant.

VERDICT: REFUSED
```

Nothing else is printed, and `--apply` writes neither `--out` nor `--crosswalk`, because every
class would have been computed under a key that cannot say which of its two rows the other layer
meant. A tool that reported them would be publishing an answer it already knows is unsound.

## Prove the signal exists

A column offered as evidence is checked for evidence before it is used. A status or district
column that holds one value on every row of both layers cannot separate anything, so it is
refused as a signal and named, with its single value. The run then continues, because the
geometry check does not need it.

**The status column.** This is the defence from the story.

```
$ python idreuse.py fire.csv utilities.csv --id-field HYDRANT_NO --right-id-field HYD_NUM --right-x-field POINT_X --right-y-field POINT_Y --status-field OPERABLE --right-status-field ACTIVEFLAG
idreuse: fire.csv 6 row(s), utilities.csv 7 row(s)
STATUS    status column 'OPERABLE' holds the single value '1' on all 13 row(s). It cannot separate a live record from a retired one, so it is refused as a status signal and nothing is filtered on it.
rows classified: 13 of 13
...
```

When the column does carry two values, a pair whose sides disagree is reported beside the
distance that already condemned it:

```
pairs whose two sides disagree on the status column:
  H-3  the two sides disagree on OPERABLE: '1' on the left, '0' on the right
```

Nothing is ever filtered on the status column, in either case. Dropping rows from an audit is how
an audit misses things.

**The district column.** The same proof, with the same refusal. Point `--district-field` at the
flag that is `1` everywhere and nothing is escalated:

```
$ python idreuse.py fire.csv utilities.csv --id-field HYDRANT_NO --right-id-field HYD_NUM --right-x-field POINT_X --right-y-field POINT_Y --district-field OPERABLE --right-district-field ACTIVEFLAG
idreuse: fire.csv 6 row(s), utilities.csv 7 row(s)
DISTRICT  district column 'OPERABLE' holds the single value '1' on all 13 row(s). It cannot separate one district from another, so it is refused as a district signal and no pair is escalated on it.
...
VERDICT: 1 match(es) disproved by the geometry. Do not publish this join.
```

## The district signal

A recycled number usually lands in another district. With `--district-field`, a pair in the
drift band whose two sides sit in different districts is escalated to `REUSED` and fails the
gate. The geometry had no opinion about that pair, and the district has one.

```
$ python idreuse.py fire.csv utilities.csv --id-field HYDRANT_NO --right-id-field HYD_NUM --right-x-field POINT_X --right-y-field POINT_Y --district-field DISTRICT --right-district-field DIST
idreuse: fire.csv 6 row(s), utilities.csv 7 row(s)
DISTRICT  district column 'DISTRICT' holds 3 distinct value(s), so a drift pair whose two sides disagree is escalated to REUSED
rows classified: 13 of 13
  CONFIRMED        2
  DRIFT            2
  REUSED           4
  UNMATCHED        3
  UNVERIFIED       2
pairs: 5 on a shared key, at confirm 150.0 ft and reuse 500.0 ft

matches the geometry disproves, worst first:
  H-3  248940.0 ft apart, left line 4, right line 4, districts '7' and '9'
  H-6  350.0 ft apart, left line 7, right line 7, districts '7' and '8'

matches in the drift band, for a human to look at:
  H-2  300.0 ft apart, left line 3, right line 3

pairs whose two sides are in different districts:
  H-3  the two sides are in different districts on DISTRICT: '7' on the left, '9' on the right
  H-6  the two sides are in different districts on DISTRICT: '7' on the left, '8' on the right

VERDICT: 2 match(es) disproved by the geometry. Do not publish this join.
```

The escalation is deliberately narrow:

- Only a `DRIFT` pair is escalated. A `CONFIRMED` pair across a district line stays `CONFIRMED`,
  because a hydrant on a boundary road is one object. It is still listed under the district
  heading so a human can see it.
- A pair with no coordinate stays `UNVERIFIED`. The district alone never condemns a match.
- A blank district on either side is a hole in the data, not a move, so it escalates nothing.
- `--right-district-field` without `--district-field` exits 64. Only one side would carry a
  district, so no pair could ever disagree, and the run would look defended by a column it never
  compared.

## The rematch: mutual nearest

`--rematch-distance` looks at the rows the key failed: every `UNMATCHED` row, and both rows of
every `REUSED` pair. A left row and a right row become a `CANDIDATE` pair only when all of these
hold:

- each is the other's nearest row in that pool,
- neither has a second row at exactly the same nearest distance,
- and they are at most `--rematch-distance` apart.

Mutual nearest is what keeps the result one to one. A right row has at most one nearest left
row, so two left rows can never both claim it. A tie answers nobody, because picking either row
would make the answer depend on file order. The rematch distance may not be above the confirm
distance, so a candidate always sits at least as close as a confirmed match.

```
$ python idreuse.py fire.csv utilities.csv --id-field HYDRANT_NO --right-id-field HYD_NUM --right-x-field POINT_X --right-y-field POINT_Y --district-field DISTRICT --right-district-field DIST --rematch-distance 50
idreuse: fire.csv 6 row(s), utilities.csv 7 row(s)
DISTRICT  district column 'DISTRICT' holds 3 distinct value(s), so a drift pair whose two sides disagree is escalated to REUSED
rows classified: 13 of 13
  CONFIRMED        2
  DRIFT            2
  REUSED           3
  UNMATCHED        2
  UNVERIFIED       2
  CANDIDATE        2
pairs: 5 on a shared key, at confirm 150.0 ft and reuse 500.0 ft
rematch: 1 candidate pair(s), each the other's nearest within 50.0 ft
...
rematch candidates, for a human to confirm or reject:
  H-3 -> W-3  13.0 ft apart, left line 4, right line 8

VERDICT: 2 match(es) disproved by the geometry. Do not publish this join.
```

`W-3` is the hydrant the recycled `H-3` should have matched, 13 feet away under another number.
A candidate is a proposal, never a match. It does not change the verdict: the join on the key
was still wrong, so the run still exits 1. The rematch is off unless the flag is given, and a
run without it prints no `CANDIDATE` count, because a zero would read as "looked and found none".

## The crosswalk

`--crosswalk links.csv --apply` writes one row per link worth keeping: every `CONFIRMED` and
`DRIFT` pair on the key, then every `CANDIDATE`. A `REUSED`, `UNVERIFIED` or `UNMATCHED` row has
no link, so it has no crosswalk row. The `tier` column says which kind each link is, so a drift
link or a candidate is never read as settled.

```
$ python idreuse.py fire.csv utilities.csv ... --rematch-distance 50 --crosswalk links.csv --apply
...
wrote links.csv
$ cat links.csv
left_key,right_key,tier,distance,left_line,right_line,left_district,right_district,reason
H-1,H-1,CONFIRMED,51.000,2,2,7,7,"51.0 ft apart, inside the confirm distance of 150.0"
H-2,H-2,DRIFT,300.000,3,3,7,7,"300.0 ft apart, between the confirm distance of 150.0 and the reuse distance of 500.0"
H-3,W-3,CANDIDATE,13.000,4,8,7,7,"13.0 ft between left line 4 (key 'H-3') and right line 8 (key 'W-3'); each is the other's nearest within 50.0. A proposal for a human, not a match."
```

Without `--apply` the run prints `Check only. links.csv was not written. Re-run with --apply.`
and writes nothing. `--out` works the same way, and one `--apply` writes both when both are named.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No match was disproved by the geometry. |
| 1 | At least one match was disproved, by distance or by district. Do not publish the join. |
| 2 | A file could not be read, or `--out` or `--crosswalk` could not be written. |
| 3 | Refused. The identifier is on more than one row, so nothing was compared. |
| 64 | Usage error, including a column name that is not in the CSV and an unknown or abbreviated flag. |

Exit 3 is deliberately not exit 1. A scheduled job that treats "one match is wrong" as a finding
to file must not treat "this identifier cannot be joined on at all" the same way.

`UNMATCHED`, `DRIFT` and `CANDIDATE` never fail the gate. Half of any two-layer comparison is
legitimately one-sided, and a tool that exited 1 on it would be ignored within a week.

## Limits

- It measures a straight line between two points and nothing else. No projection, no datum, no
  geodesic. Both layers must arrive in one projected CRS, and `--units` only labels the number.
- Points only. A hydrant is a point. A line or a polygon has no single coordinate to compare, and
  centroid distance between two parcels would be a different tool with a different threshold.
- `150` and `500` are the defaults, not a standard. They came from one measured pair of hydrant
  layers where roughly four fifths of all ID-matched pairs fell inside 150 feet. Measure your own
  pair, then set the two flags. Do not inherit these numbers because they are printed here.
- `--rematch-distance` has no default on purpose. Pick it from your own densest block: it must be
  shorter than the distance between two real neighbouring assets, or a tie will answer nobody.
- The rematch compares every leftover row on one side with every leftover row on the other. In
  a measured worst case, 3,000 rows a side with every row in the pool, it took 15 seconds on
  Windows and 9 seconds on Ubuntu. The same layers without the rematch took 0.1 seconds. Only
  the rows the key failed are in the pool, so a real run is usually far smaller. A statewide
  layer would need a spatial index, which this tool does not have.
- The district is read from a column. The tool does not intersect a point with a district
  polygon, so a district column that is stale is believed. Fill it with a spatial join first if
  you do not trust it.
- The crosswalk is keyed on the pair of row keys and their CSV lines. A permanent asset number
  that is a third column, distinct from the join key, is not read, and the crosswalk keeps no
  history between runs.
- It compares identifiers verbatim. Trailing whitespace is trimmed and nothing else is: no case
  folding, no punctuation stripping, no leading-zero padding. If the two sides spell the
  identifier differently, settle that first with [nalmatch](https://github.com/uhsear/nalmatch)
  and come back.
- CSV only. No geodatabase, no shapefile, no SDE connection. Reading a geodatabase would need
  `arcpy`, which would stop the tool running anywhere else.
- It reads both files into memory. A few thousand rows a side runs in well under a second; a
  statewide layer is not what this is for.
- There is no `--fix`. The tool names the matches the geometry disproves and proposes
  candidates. Deciding which of the two objects keeps the number is an edit somebody signs for.
- `DRIFT` is not a verdict. It is the band where the geometry has no opinion, and it exists so
  that the tool never quietly promotes a doubtful pair into `CONFIRMED` or condemns it.
- A status column is read and reported, never filtered on, and a column holding one value is
  refused outright. A district column holding one value is refused the same way.
- Nothing is written without `--apply`, and a refused run writes nothing at all.
- It opens no socket and imports no network module, so there is no credential anywhere in it to
  leak.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [nalmatch](https://github.com/uhsear/nalmatch) - reconcile two tables on an identifier, and
  refuse an identifier rule that would merge two records into one key. Run it first when the two
  sides spell the ID differently; this tool does no normalisation at all.
- [geocodesift](https://github.com/uhsear/geocodesift) - find the records stacked on one
  coordinate in a geocoded batch, and gate the batch on the result. It answers "how many records
  share this point"; this one answers "how far apart are the two rows a join just paired".
- [roadmiles](https://github.com/uhsear/roadmiles) - the same refusal to certify a number nobody
  checked.
- [fcload](https://github.com/uhsear/fcload) - load the reconciled result without corrupting it.
