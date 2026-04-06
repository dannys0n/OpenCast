# GSI Data Samples

This folder is a small local RAG-style reference for the CS2 GSI payloads we have
actually observed at runtime.

It is meant to answer questions like:

- What top-level payload blocks have we really seen?
- Which fields have shown up non-empty?
- What values have we observed for specific fields?
- Did grenade data arrive as `grenades` or `allgrenades`?

Current important finding:

- Live grenade runtime data has been observed in the top-level `grenades` block.
- We have not yet observed runtime data in `allgrenades`.
- The useful live path so far is `grenades.*.position`.

Files:

- `sample_index.json`
  High-level index of the local sample database.
- `observed_payload_shapes.json`
  Curated view of top-level payload groups and notable runtime notes.
- `observed_field_values.json`
  Seed catalog of observed values and example values for important fields.
- `grenade_samples.json`
  Grenade-specific observed samples from the logs gathered on 2026-04-06.

Notes:

- This is intentionally a seed dataset, not a perfect full schema.
- It should grow as we test more gameplay states.
- When new runtime cases are tested, append the newly observed values instead of
  replacing the old ones.
