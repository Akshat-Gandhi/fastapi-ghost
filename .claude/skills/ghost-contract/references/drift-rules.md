# Drift classification

Drift = difference between the current state and the previous contract/spec (`spec.prev.json`, old `contract.json`), recorded in `drift.json`. Each item gets one class.

## breaking (existing clients may fail)
- Operation removed, or method/path changed.
- New required parameter or required body field.
- Parameter/field type narrowed or made incompatible; enum values removed.
- A previously declared success status removed or changed.
- Auth requirement added or changed.
- Error body shape changed for an existing status (e.g. `detail` string became object).
- A status that used to be expected now appears under `unexpected` (e.g. new 5xx).

## additive (existing clients keep working)
- New operation, new optional parameter, new optional response field.
- New enum value in a response; new declared error status.
- Newly observed or declared status that was previously "observed but undeclared".

## cosmetic (no behaviour change)
- summary, description, tags, examples, title changes.
- Reordered fields or paths; `operationId` unchanged.
- Whitespace/format-only changes.

## Rules of thumb
- When unsure between breaking and additive, choose breaking and say why in the item's note.
- A changed `spec_hash` with no semantic change is cosmetic.
- Do not classify what was not seen; use `"unknown"`.
- Never modify `pinned` contract fields to resolve drift; report the conflict instead.
