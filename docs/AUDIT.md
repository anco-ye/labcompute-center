# Public release review — 2026-10-26

This review covers the isolated public source export. It does not certify a production deployment or change running computations.

## Scope and findings

Reviewed authentication and CSRF boundaries, source editing CAS and deployment activation, package/ZIP validation, dependency ordering, scheduler claim and lease correspondence, resource admission, Windows broker/JobObject and Linux cgroup interfaces, native completion receipts, resumable result delivery and Range requests, and frontend refresh behavior. Scientific computation has no wall-clock termination limit; cancellation and memory, commit, disk and license controls remain distinct protections.

The public distribution fixes an incomplete node application bundle, replaces an embedded operator SSH key with explicit onboarding inputs, retires an instance-specific configuration patch behind a fail-closed guard, and adds private configuration initialization. Enrollment produces an offline package and disabled registration record without modifying an existing controller. Direct-download proxy selection is independent of a particular lab domain. Tests and frozen-input helpers are self-contained.

Source publication and resident activation remain different states. A successful test or queued deployment is not proof that a busy production component has activated the change. A native exit receipt and delivery status are checked separately; exit zero is not scientific acceptance.

## Verification

- Clean Python environment: 1088 tests passed, with one skipped test.
- Frontend: 13 test groups passed, including Chromium refresh/disclosure/scroll coverage.
- Locked core Python dependencies: 11 packages audited; no known vulnerabilities reported at review time.
- npm dependency audit: four packages audited; no known vulnerabilities reported at review time.
- Public-file and nested-archive scan: no detected private-key, known credential, private operator path or instance-secret matches in the release candidate.

Some Python test fixtures emit ResourceWarnings; the test run passes. Dependency audits cover their declared package sets, not scientific runtimes, licenses or operating-system dependencies.

## Publication boundary

Private configuration, authentication databases, certificates and keys, operational receipts, node inventories, real task records, research inputs, results, caches and private automation state are excluded. Public examples use synthetic configuration. Preparation archives are rebuilt from the public sources. No third-party license grant is inferred from publication.

Windows installation and PowerShell flows were reviewed but were not natively installed on new machines in this review. Production Windows/Linux execution and COMSOL license availability were not revalidated as part of publication. Existing operational backlog and recovery cases remain operational concerns, independent of this source release.
