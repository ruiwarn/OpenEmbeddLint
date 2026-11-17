# Evidence-based quality review

Use this after the deterministic runner. Machine diagnostics and semantic review are different evidence sources; never relabel one as the other.

## Required reading scope

1. Every changed implementation and public declaration.
2. Every affected caller, or a recorded reason why the caller set is bounded.
3. Relevant state ownership, error paths and concurrency/interrupt assumptions.
4. Tests and the source of their expected values.
5. Existing project rules that apply to the changed responsibility.

If this scope was not read, keep `maintainability-review` as SKIP. Do not write “no maintainability issue” from metrics alone.

## Readability findings

Look for concrete reading obstacles: nested control flow, mixed units, unclear ownership, long conditional expressions, names that contradict behavior, and constants whose meaning cannot be recovered at the use site.

clang-tidy metrics locate candidates. They do not measure business meaning, conditional-compilation burden or whether a cohesive state machine is better left intact. A metric finding should explain the actual improvement expected from a change. Lizard McCabe CCN counts decision paths; clang-tidy cognitive complexity measures reading burden. Report them separately and never substitute one for the other.

## Maintainability findings

Check for specific, observable coupling:

- Parsing, business decisions and I/O in one function without a coherent reason.
- A module mutating another module's private state or duplicating its rules.
- The same conversion or policy repeated across callers.
- Hidden global inputs or side effects absent from the interface contract.
- Ownership, lifetime, units and valid ranges that callers cannot determine.
- Logic that cannot be tested independently; identify the exact dependency preventing it.

Use this format for every finding:

```text
severity: error | warning | suggestion
kind: review_finding
location: file:line, function/symbol
observed: code or caller relationship actually read
impact: concrete maintenance, correctness or test consequence
recommendation: smallest useful change
premise: verified | needs-manual-confirmation
```

Do not emit “improve SRP”, “improve readability” or a numeric maintainability score without observed evidence. Do not manufacture a new abstraction solely to satisfy a pattern label.

## Correctness and test adequacy

Establish expected behavior independently from requirements, protocol contracts, accepted bug reproductions or existing documented behavior. Expected values copied from the implementation are not independent evidence.

For every acceptance requirement, record:

```text
requirement → test/assertion → execution result → covered paths → remaining manual/target evidence
```

Inspect assertions, not just test names. Distinguish:

- Real C/C++ execution versus source-text checks.
- Host hardware stubs versus target hardware behavior.
- Branch execution versus a meaningful assertion at that branch.
- One preprocessor configuration versus other shipped configurations.
- Fuzz crash-free execution versus a business invariant that was actually checked.

State uncovered error, timeout and boundary branches as evidence gaps. Never infer that all missing tests were enumerated.

## Reporting policy

Deterministic failures use the tool's `PASS/FAIL/ERROR/SKIP` result. Semantic observations use `review_finding` with a premise marker. If execution required for a conclusion is missing, say what remains unknown. No single aggregate quality score and no automatic refactoring driven only by thresholds.
