# Honest limits — pkg-wave1-external-record-v1

- Scope cap: byte-equality is against the on-disk extract of record, not upstream PDF text layers; re-derivation from PDFs was not performed.
- Sandbox limit: process-level sandbox: cwd containment, environment scrubbing, timeout; not kernel isolation.
- C7 scope ground truth: source.user_spans[].in_scope is producer-authored and has no independent ground truth. A producer that marks every span out-of-scope (with reasons) leaves the in-scope set empty, so C7 returns PASS with zero byte-identity work. This semantic hole cannot be closed by a byte-level instrument; it is disclosed here, not treated as coverage.
- C8 probe scope: the seeded-constraint probe measures emission round-trip integrity (the package re-emits byte-identically under the seeded constraints), not classification. It cannot establish that the probe classifies or diagnoses eviction behavior; this limitation is recorded here.
