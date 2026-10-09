# idreuse

Reconcile two asset layers on a shared ID and refuse every match the geometry disproves. Refuses
the ID itself when it is not unique, and refuses a status column that holds one value on every
row.

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

```
$ python idreuse.py --self-test
idreuse self-test: no file, no network, no credentials
--------------------------------------------------------------------
PASS  a pair 51 units apart is CONFIRMED, which is the ordinary case
PASS  a pair 300 units apart is DRIFT, neither confirmed nor rejected
PASS  a pair 248940 units apart is REUSED, never a match  <-- pinned defect
PASS  the refusal says in words that one key covers two objects
PASS  two rows on the same point are CONFIRMED
PASS  exactly the confirm distance is CONFIRMED, the threshold is inclusive
PASS  a thousandth past the confirm distance is DRIFT
PASS  exactly the reuse distance is DRIFT, the threshold is inclusive
PASS  a thousandth past the reuse distance is REUSED
...
PASS  a status column holding one value on every row is refused as a signal  <-- pinned defect
PASS  and the refusal names the column and the single value it holds  <-- pinned defect
PASS  and says plainly that nothing was filtered
PASS  and fails the gate  <-- pinned defect
PASS  and DRIFT does not fail the gate: it is not auto-rejected  <-- pinned defect
PASS  and is counted apart from REUSED, so the two never pool  <-- pinned defect
PASS  a pair with no coordinate on one side is UNVERIFIED  <-- pinned defect
PASS  two blank anchors are two unverified rows, never a match on ''  <-- pinned defect
PASS  the classes sum to the input row count  <-- pinned defect
PASS  a repeated anchor refuses the run  <-- pinned defect
PASS  and not one pair is built, because the join never ran  <-- pinned defect
PASS  a status column that is 1 on every row is refused, and the run continues  <-- pinned defect
PASS  a negative confirm distance refuses a run with no rows in it  <-- pinned defect
PASS  and prints no count of anything else  <-- pinned defect
PASS  a refused run writes no class for any row  <-- pinned defect
PASS  a NaN coordinate cell cannot reach the separation  <-- pinned defect
...
PASS  OPERABLE is 1 on all ten rows, so it is refused as a signal  <-- pinned defect
PASS  the run continues and still fails on the match it disproved  <-- pinned defect
PASS  a repeated anchor exits 3, not the 1 a disproved match exits  <-- pinned defect
PASS  and nothing else is reported at all  <-- pinned defect
PASS  a UTF-8 BOM is stripped from the first column name  <-- pinned defect
PASS  a BOM file reconciles the same as one without  <-- pinned defect
PASS  --out without --apply writes nothing at all  <-- pinned defect
PASS  a refused run writes nothing, because it classified nothing  <-- pinned defect
PASS  a CSV field over the csv module's own limit exits 2, not 1  <-- pinned defect
PASS  an empty CSV has no columns, which is a usage error naming them
PASS  a layer with no rows classifies the other side and passes
PASS  main with no argv reads the arguments after the program name
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  and raises() accepts the ValueError it is looking for
--------------------------------------------------------------------
265 assertions, 0 failed
```

The full run prints all 265 assertions. The three `...` lines above are where this block is cut.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database. Both layers are CSVs, which is what every asset layer can be exported as.

The current run prints 265 assertions on Windows (3.13.2). The Ubuntu (3.12.3) run has not been
repeated since the allow_abbrev change. The code uses no syntax newer than Python 3.6, but 3.12 is
the oldest interpreter it has been run on.

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
python idreuse.py fire.csv utilities.csv --out classified.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `LEFT` | none | The first layer as a CSV. Required unless `--self-test`. |
| `RIGHT` | none | The second layer as a CSV. Required unless `--self-test`. |
| `--id-field` | `ASSET_ID` | The shared identifier column, on both sides. |
| `--x-field` | `X` | The X column, on both sides. |
| `--y-field` | `Y` | The Y column, on both sides. |
| `--status-field` | off | A live-or-retired column to read on both sides. Never filtered on. |
| `--right-id-field` | left's | The identifier column on the right side, when it differs. |
| `--right-x-field` | left's | The X column on the right side. |
| `--right-y-field` | left's | The Y column on the right side. |
| `--right-status-field` | left's | The status column on the right side. |
| `--confirm-distance` | `150` | At or under this separation a match is CONFIRMED. |
| `--reuse-distance` | `500` | Above this separation a match is REUSED. |
| `--units` | `ft` | Label printed after every distance. Nothing is converted. |
| `--sample` | `10` | How many records to list under each heading. `0` lists none. |
| `--out` | off | Write the classification to this CSV. Needs `--apply`. |
| `--apply` | off | Actually write. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. Takes no other flag. |

## What it checks

One thing, and it refuses two more before it gets there.

**The check.** Every key that is on both sides pairs one row with one row. The separation between
the two points decides the pair:

| Class | Separation | What it means |
|---|---|---|
| `CONFIRMED` | at or under `--confirm-distance` | Two crews captured one object. |
| `DRIFT` | between the two distances | Neither confirmed nor disproved. A human looks at it. |
| `REUSED` | above `--reuse-distance` | One key, two objects. The join is wrong here. |
| `UNMATCHED` | no pair | The key is on one side only. |
| `UNVERIFIED` | no separation | No coordinate, or no ID, so the geometry proves nothing. |

Every input row from both files lands in exactly one class, and the five counts add up to the
number of rows that went in. `UNMATCHED` is counted apart from `REUSED` on purpose: "this key is
not over there" and "this key is over there on the wrong object" are different failures, and a
tool that pooled them would report one number for two problems.

```
$ python idreuse.py fire.csv utilities.csv --right-id-field HYD_NUM --right-x-field POINT_X --right-y-field POINT_Y
idreuse: fire.csv 7 row(s), utilities.csv 5 row(s)
rows classified: 12 of 12
  CONFIRMED        2
  DRIFT            2
  REUSED           2
  UNMATCHED        3
  UNVERIFIED       3
pairs: 4 on a shared key, at confirm 150.0 ft and reuse 500.0 ft

matches the geometry disproves, worst first:
  H-1043  248940.0 ft apart, left line 4, right line 4

matches in the drift band, for a human to look at:
  H-1042  300.0 ft apart, left line 3, right line 3

VERDICT: 1 match(es) disproved by the geometry. Do not publish this join.
```

Four keys were on both sides. Three of those four matches are fine or arguable. The fourth is
`H-1043`, whose two rows are 248,940 feet apart, and a join is the only tool in the room that
calls that a match.

**Refusal 1: an ID that is not unique.** Checked before anything else, and it stops the run.

```
$ python idreuse.py fire-dupe.csv utilities.csv --right-id-field HYD_NUM --right-x-field POINT_X --right-y-field POINT_Y
idreuse: fire-dupe.csv 3 row(s), utilities.csv 5 row(s)
REFUSED   the anchor is not unique, so nothing was compared
      left key 'H-1041' is on 2 rows: line 2, 4
      Fix the anchor or name a different column. A key two rows claim cannot say which row the other layer meant.

VERDICT: REFUSED
```

Nothing else is printed and `--out --apply` writes nothing, because every class would have been
computed under a key that cannot say which of its two rows the other layer meant. A tool that
reported them would be publishing an answer it already knows is unsound.

**Refusal 2: a status column with one value.** This is the defence from the story.

```
$ python idreuse.py fire.csv utilities.csv --right-id-field HYD_NUM --right-x-field POINT_X --right-y-field POINT_Y --status-field OPERABLE --right-status-field ACTIVEFLAG
idreuse: fire.csv 7 row(s), utilities.csv 5 row(s)
STATUS    status column 'OPERABLE' holds the single value '1' on all 12 row(s). It cannot separate a live record from a retired one, so it is refused as a status signal and nothing is filtered on it.
rows classified: 12 of 12
...
```

The column is named, the single value is named, and the run continues. It continues because the
geometry check does not need the flag, and stopping would hide the recycled ID behind a data
problem the operator can fix later.

When the column does carry two values, a pair whose sides disagree is reported beside the
distance that already condemned it:

```
pairs whose two sides disagree on the status column:
  H-1043  the two sides disagree on OPERABLE: '1' on the left, '0' on the right
```

Nothing is ever filtered on the status column, in either case. Dropping rows from an audit is how
an audit misses things.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No match was disproved by the geometry. |
| 1 | At least one match was disproved. Do not publish the join. |
| 2 | A file could not be read, or `--out` could not be written. |
| 3 | Refused. The identifier is on more than one row, so nothing was compared. |
| 64 | Usage error, including a column name that is not in the CSV. |

Exit 3 is deliberately not exit 1. A scheduled job that treats "one match is wrong" as a finding
to file must not treat "this identifier cannot be joined on at all" the same way.

`UNMATCHED` never fails the gate. Half of any two-layer comparison is legitimately one-sided, and
a tool that exited 1 on it would be ignored within a week.

## Limits

- It measures a straight line between two points and nothing else. No projection, no datum, no
  geodesic. Both layers must arrive in one projected CRS, and `--units` only labels the number.
- Points only. A hydrant is a point. A line or a polygon has no single coordinate to compare, and
  centroid distance between two parcels would be a different tool with a different threshold.
- `150` and `500` are the defaults, not a standard. They came from one measured pair of hydrant
  layers where roughly four fifths of all ID-matched pairs fell inside 150 feet. Measure your own
  pair, then set the two flags. Do not inherit these numbers because they are printed here.
- It compares identifiers verbatim. Trailing whitespace is trimmed and nothing else is: no case
  folding, no punctuation stripping, no leading-zero padding. If the two sides spell the
  identifier differently, settle that first with [nalmatch](https://github.com/uhsear/nalmatch)
  and come back.
- CSV only. No geodatabase, no shapefile, no SDE connection. Reading a geodatabase would need
  `arcpy`, which would stop the tool running anywhere else.
- It reads both files into memory. A few thousand rows a side runs in well under a second; a
  statewide layer is not what this is for.
- There is no `--fix`. The tool names the matches the geometry disproves. Deciding which of the
  two objects keeps the number is an edit somebody signs for.
- `DRIFT` is not a verdict. It is the band where the geometry has no opinion, and it exists so
  that the tool never quietly promotes a doubtful pair into `CONFIRMED` or condemns it.
- A status column is read and reported, never filtered on, and a column holding one value is
  refused outright.
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
