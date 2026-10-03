# Native text selection in a batch

These cases were defined before implementing `select_text` batch support. The
public contract is the existing native selection contract, with the same app
queue, observation, task, cancellation, and human-input gates as other steps.

## Failure cases and acceptance evidence

1. A model cannot discover the new step, or the parser rejects a documented
   selection field. `batch_text_schema` requires `select_text`, `text`,
   `element_index`, `prefix`, `suffix`, and all three `selection_type` modes in the
   public tool schema. Flat steps and legacy nested `args` must both work.
2. Selection followed by insertion replaces too much, uses grapheme offsets in
   place of AX UTF-16 offsets, or returns its observation before insertion.
   `batch_text_unicode` replaces one contextual occurrence after emoji and a
   combining character, then checks the complete text, exact UTF-16 caret,
   per-step semantic routes/readback verification, and the final batch `expect`.
3. Caret-before/caret-after becomes a selection, or typing duplicates an identical
   replacement. `batch_text_carets` checks insertion on both sides of a match and
   replacement with identical text without duplication, including `observe=none`.
4. A malformed later selection (empty/missing text, missing/invalid index,
   unsupported mode, non-scalar context) is discovered after an earlier mutation
   has already run. `batch_text_validation` places a sentinel mutation first and
   requires the entire call to be rejected before any text or selection changes.
5. A missing or ambiguous match moves the selection anyway and lets typing land
   at the previous caret. `batch_text_refusals` requires the selection step to
   fail, typing to be skipped by default, and both text and selected range to
   remain unchanged. Prefix/suffix disambiguation is demonstrated by case 2.
   `batch_text_overlap` also rejects overlapping ASCII and emoji matches, then
   selects exactly one using its prefix; counting only non-overlapping matches
   would incorrectly permit an ambiguous edit.
6. An obsolete observation/index bypasses freshness because selection runs inside
   a batch. `batch_text_stale` requires a stale observation ID and a nonexistent
   element to refuse selection and leave the document unchanged. Existing
   `native_stale_coordinates`/`batch_layout_guard` cover layout-sensitive pixel
   steps: a semantic text selection does not make subsequent stale pixels safe.
7. Task pause, takeover, or cancellation arrives during an earlier wait but the
   following selection/insertion still runs. `batch_text_controls` queues the
   selection after a wait, changes the task state while the operation is in
   flight, and checks that neither selection nor text changes. Resuming requires
   a fresh observation under the existing task contract.
8. The AX setter accepts a selection but readback does not match it. The shared
   selection path must report `verified=false, uncertain=true`, so the default
   batch stops before insertion. `batch_text_unconfirmed` uses a deliberately
   faulty AX editor that acknowledges range writes but retains its original
   selection, and checks that the queued insertion is skipped.
9. Selection skips app approval, session ownership, target freshness, or recent
   human-input checks. It must execute through the common per-step admission
   checks and the same `mutationElement`/`NativeSafety` path as standalone
   selection. Existing native/task suites exercise those shared gates; actual
   simultaneous physical typing remains separate attended evidence and must not
   be claimed from synthetic input.

## Execution

`SC_E2E_APP=build/SecondCursor.app python3 e2e/text_batch.py` uses disposable
TextEdit documents and a disposable runtime. The runner records cursor,
foreground-app, and Space invariants. `--off-space` uses the existing suite's
explicit off-Space fixture contract. No test edits or saves the user's documents.

For model validation, run the existing native notes benchmark with
`gpt-6.1-sol` and reasoning `high`. The final document must preserve the original
line and append exactly `\nTotal: 42`; record whether the model actually chooses
selection plus insertion in one batch, tool count, and measured runtime. A
model choosing separate calls does not invalidate API correctness, but cannot
support a claim of fewer model turns.
