# Synthetic workflow evaluation fixtures

`inputs.json` contains two explicitly synthetic source snapshots. They are not
field observations or expert-reviewed accuracy cases. `labels.json` is a separate
scoring input, bound to the exact input-file bytes and each canonical case hash.
Do not pass labels to the execution process, provider requests, or database import.

The first case exercises a bearing analysis and the human-review boundary. The
second lacks usable gearbox samples and guidance and expects abstention at the
unchanged production diagnosis gate. A model that continues or guesses fails that
expectation; the fixture does not lower the confidence threshold or fabricate
downstream reviews. Five review expectations describe the five production review
types, not five independent model calls.

Source timestamps remain unchanged. Execution records its own wall-clock time;
approval and execution validity are not assessed by these pre-review snapshots.
Source artifacts, when supplied, must remain under the explicit artifact root and
match their size and SHA-256. Field snapshots additionally require a provenance
summary and deidentification statement. Expert metadata is only a declaration;
the loader never promotes it to verified expert qualification.

Imports create source facts, reference agent catalog rows, and a queued mission.
The importer never seeds WT-023 business data, generated evidence, decisions,
approvals, agent executions, or embeddings. Each case requires a fresh owned
business database; SQLite tests verify import behavior only. Real PostgreSQL,
embedding-provider retrieval, expert labels, and field accuracy need their own
execution evidence.
