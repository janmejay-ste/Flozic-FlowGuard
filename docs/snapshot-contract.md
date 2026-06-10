# Snapshot Contract (v3 schema)

Cross-language contract between the Python Playwright stack (this repo)
and the Java scoring engine (`../automate-workflow-test/`). Both sides
read and write JSON files conforming to the v3 schema defined in the
Java `SnapshotSchemaVersion.V3_CONTRIBUTOR_RICH` enum.

This doc is the **specification**. Any deviation in either stack's
output is a contract violation.

## Authoritative source

The Java enum:

```java
// src/test/java/utils/health/schema/SnapshotSchemaVersion.java
V3_CONTRIBUTOR_RICH(3, true)
```

The detector:

```java
// src/test/java/utils/health/schema/SchemaDetector.java
hasContributorItems(root) → returns V3 when scoreContributors.items[] exists
```

When the Java schema definition changes, this doc must change, and the
Python writer (`utils/snapshot_writer.py`) must change. The Java enum
is the source of truth; this doc and the Python writer follow.

## Top-level shape

```json
{
  "schemaVersion": 3,
  "source": "python",
  "timestamp": 1780000000000,
  "tests": [ /* test records, see below */ ],
  "scoreContributors": {
    "testFailures": { "source": "testFailures", "items": [] },
    "jsErrors":     { "source": "jsErrors",     "items": [] },
    "fallbacks":    { "source": "fallbacks",    "items": [] },
    "totalRaw": 0.0,
    "totalApplied": 0.0,
    "totalSuppressed": 0.0
  }
}
```

### Field-by-field

| Field | Type | Required | Notes |
|---|---|---|---|
| `schemaVersion` | int | yes | Always `3` for this schema. Older snapshots use `2` and are recognised but lossy-migrated by Java's `LossyV2ToV3Migrator`. |
| `source` | string | no | `"python"` when produced by this stack. Java omits this field. Used by a hybrid merger to attribute origin. |
| `timestamp` | long (epoch ms) | yes | When the snapshot was written. |
| `tests` | array | yes | Test records, shape below. |
| `scoreContributors` | object | yes (shape) | MUST have the V3 shape. Python emits empty `items[]` arrays because scoring lives in Java. Java's HealthTracker computes the actual scores. |

### Test record shape

```json
{
  "category": "FULL",
  "login": "Auth",
  "feature": "Sanity Journey",
  "class": "TestAuthenticatedSanityJourney",
  "method": "test_sanity_journey",
  "status": "PASS",
  "duration": 12345,
  "artifacts": "test_sanity_journey_20260605_120000_123",
  "videoPath": "reports/recordings/test_sanity_journey/abc.webm"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `category` | string | yes | SMOKE / SANITY / REGRESSION / FULL — matches Java TestType enum |
| `login` | string | yes | `"Auth"` or `"Guest"` |
| `feature` | string | yes | Free-form feature label from `@test_category(feature=...)` |
| `class` | string | yes | Pytest class name. Field is named `class` for cross-language compat with Java's existing JSON readers. |
| `method` | string | yes | Pytest method name |
| `status` | string | yes | `"PASS"` / `"FAIL"` / `"SKIP"` |
| `duration` | int (ms) | yes | Test wall-clock duration |
| `artifacts` | string | no | Folder name under `reports/failures/` for failed tests |
| `videoPath` | string | no | Relative path to .webm video file when `--record-video` is set |

## Cross-stack merge strategy

Two options for combining Python output with Java output:

### Option A: Single shared snapshot (currently NOT implemented)

Both stacks write to `reports/trend/health_snapshot.json` and append
their test records. Requires:
- Append-merge logic on the Java side (currently the Java HealthTracker
  overwrites)
- File locking to prevent races

Not done yet. Avoid until the simpler Option B proves insufficient.

### Option B: Separate snapshots merged at dashboard time (current default)

- Java writes its canonical `reports/trend/health_snapshot.json`
- Python writes `reports/trend/python-health-snapshot.json`
- The Java DashboardBuilder reads both, with Python's records appended
  to the `tests` array before the dashboard is generated

This is the current Python writer's behaviour. The merge step on the
Java side has NOT been implemented yet — adding it is a small follow-up
in the Java repo, not in this one.

## Schema-evolution rules

When the Java schema definition adds a new version (V4, V5, ...):

1. Java's `SnapshotSchemaVersion` enum gets a new entry
2. A new `SchemaMigrator` permits-list entry is added to handle V3→V4
3. The Python `SCHEMA_VERSION` constant in `utils/snapshot_writer.py` is updated
4. This contract doc is updated to describe the V4 shape
5. The Python writer's `_test_record_to_json` / `write_snapshot`
   are updated to match

The Java compile-time exhaustiveness check catches new versions on its
side automatically. The Python side has no such check — schema drift
will produce silent contract violations unless someone updates the
writer. The mitigation: a small contract test in the Java repo that
parses the Python snapshot and asserts V3 detection (deferred until
the Python stack lands in CI).

## What this contract does NOT promise

Applying the evidence-first discipline:

- This doc does **not** specify the dashboard rendering. That's
  Java DashboardBuilder's territory; the snapshot is its input.
- This doc does **not** specify scoring. Scores are computed by the
  Java HealthTracker from the snapshot fields.
- This doc does **not** cover the snapshot's other top-level fields
  (`scoreContributors.testFailures.items[]`, semantic.clusters[], etc.)
  because Python doesn't populate them in this phase. Those fields are
  the Java side's responsibility.

## References

- Java enum source: `../automate-workflow-test/src/test/java/utils/health/schema/SnapshotSchemaVersion.java`
- Java detector source: `../automate-workflow-test/src/test/java/utils/health/schema/SchemaDetector.java`
- Migration plan: `../automate-workflow-test/docs/python-playwright-migration-plan.md`
- Evidence-first discipline: `../automate-workflow-test/docs/evidence-first-analysis.md`
