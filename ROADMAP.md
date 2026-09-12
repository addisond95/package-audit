# Package Audit Product Roadmap

This is the working roadmap from the current offline audit tool to a product-ready system. Complete each phase and its acceptance gate before moving to the next one.

## Product principles

- Tracking number is the primary package identity. A package is never marked present from a name, address, or unit alone.
- Unit, building address, and resident last name are corroborating evidence. Ambiguous or conflicting evidence creates a review alert; it must never be silently "fixed."
- The future scanner is a **non-agentic, deterministic ML-assisted system**: computer-vision/OCR models produce regions, text, and confidence; fixed matching rules decide confirm, rescan, or alert. It will not be an autonomous agent.
- Never train a model from unreviewed scans. Training labels must be reviewed, versioned, and traceable.
- Minimize resident data. The normal audit must remain offline and work without uploading labels.

## Current baseline: v0.13

- [x] Offline Bluetooth connection between the Android scanner and Mac audit app.
- [x] Fast on-device barcode + QR scanning, with OCR fallback and multiple barcode candidates.
- [x] Exact tracking-number matching.
- [x] Optional destination-unit reading and numeric normalization (`2803` can match `2803S`; `305` cannot match `3005`).
- [x] Tracking-only mode, manual confirmation when a unit cannot be read, and review alerts for unit mismatches.
- [x] Audit alerts, package-error templates, area-completion check, audit-note rules, and unit-based audit-note metrics.
- [x] Local scanner feedback counters and verified unit aliases.
- [x] Package location shown with phone results and retained in scanner alerts.
- [x] Operator-created alerts from a phone label scan plus physical-location selection.
- [x] Consistent `YYYY_MMDD_package_audit` export filenames.

### Important limitation of the current learning loop

The existing feedback loop records aggregate scan outcomes and verified unit aliases. It does **not** retain label images or retrain barcode/OCR/layout models. It will not, on its own, learn damaged labels or carrier-specific visual layouts. The later training-data and model phases below are what enable that.

## Phase 0 — Field validation of the current scanner

- [ ] Run at least three real audits with v0.12 or later.
- [ ] Record non-sensitive metrics: total packages, first-pass tracking reads, rescans, manual tracking entries, unit reads, unit mismatches, false alerts, and average scan-to-result time.
- [ ] Record failures by category: carrier, label condition (glare, wrinkle, tear, low contrast), barcode type, and multiple-label packages. Do not store resident information in this field log.
- [ ] Fix reproducible workflow defects before collecting data for a model.

**Acceptance gate:** the core workflow is dependable enough to use every audit, and its baseline accuracy/latency is measured rather than guessed.

## Phase 1 — Building profile and address verification

The address belongs to the configured property/building, not to each audit row.

- [ ] Add a per-building profile: canonical delivery address, street aliases, city/state/ZIP, building/tower names, and approved address-format variants.
- [ ] Add a destination-address extractor that distinguishes destination from sender/return address where possible.
- [ ] Normalize address components deterministically: street-number, street-name abbreviations, directional prefixes, unit-bearing address lines, ZIP+4, and approved building aliases.
- [ ] Show one of three results: destination matches this building, destination is unreadable/uncertain, or destination conflicts with this building.
- [ ] Make a confident wrong-building result an alert that requires review before ordinary confirmation; an uncertain result must allow tracking-only confirmation.
- [ ] Test against actual USPS, UPS, FedEx, Amazon, courier, handwritten, and multi-label packages from the pilot property.

**Acceptance gate:** address checking catches wrong-building deliveries without regularly blocking correctly addressed packages.

## Phase 2 — Resident last-name corroboration

This phase requires management approval because resident names are personal information.

- [ ] Define the authoritative resident roster, update process, access roles, retention policy, and what happens for households, businesses, guests, and vacant units.
- [ ] Match normalized **last names** only by default. Do not make first-name OCR or full-name storage part of the normal workflow unless there is a separately approved need.
- [ ] Use the last name as supporting evidence alongside exact tracking and unit/building checks.
- [ ] Alert for a confident conflict (for example, label unit and resident roster conflict) but keep ambiguous, abbreviated, or unreadable names reviewable rather than automatically rejecting a package.
- [ ] Do not retain name OCR results in scanner analytics or diagnostic logs by default.
- [ ] Add tests for hyphens, apostrophes, suffixes, household aliases, common surnames, and labels with both sender and recipient names.

**Acceptance gate:** name matching improves error detection without creating privacy risk or a high false-alert rate.

## Phase 3 — Official training-data API and dataset

Build this only after the rules above are proven in field use and data governance is approved.

```text
Phone (explicit diagnostic sample)
          |
          v
Authenticated API -> encrypted object storage (label image/crop)
          |          encrypted review database (metadata + annotations)
          v
Human review -> approved, versioned training dataset -> model training
```

- [ ] Use an API; the phone must never connect directly to a database or cloud storage bucket.
- [ ] Upload only explicitly selected diagnostic samples or staff-approved corrections—not every routine scan.
- [ ] Crop to the relevant label area on-device when possible and redact/minimize unrelated information before upload.
- [ ] Require authenticated users/devices, property/tenant isolation, encryption in transit and at rest, audit logs, rate limits, and deletion requests.
- [ ] Define retention: short raw-image retention, longer de-identified annotations only when justified, and a documented purge process.
- [ ] Add a review tool and label schema: carrier, barcode region, tracking-text region, destination-address region, unit region, optional last-name region, image quality, damage type, orientation, and verified outcome.
- [ ] Require a human reviewer to approve each training label; preserve source, reviewer, timestamp, consent/status, and dataset version.
- [ ] Keep a deliberately held-out test set that is never used for training.

**Acceptance gate:** sample collection is secure, opt-in, reviewable, deletable, and produces consistently annotated data.

## Phase 4 — Carrier, layout, and damaged-label model

Build a conventional supervised computer-vision pipeline, not an agentic system:

1. A label/carrier classifier identifies likely carrier and label family.
2. A layout/region model finds barcode, tracking text, destination address, unit, and (when approved) recipient-last-name regions.
3. On-device barcode decoding and OCR run on those selected regions with quality-aware preprocessing.
4. The existing deterministic matching engine compares results against the audit and building profile.
5. Confidence thresholds choose: confirm candidate, ask for confirmation/rescan, or create an alert.

- [ ] Benchmark the current scanner before adding ML, per carrier and label condition.
- [ ] Train an image-quality/damage classifier for blur, glare, skew, wrinkle, occlusion, low contrast, and torn labels.
- [ ] Add safe preprocessing such as perspective correction, crop selection, rotation, and contrast improvement.
- [ ] Never use generative reconstruction to invent unreadable barcode digits or tracking text. An unclear label must remain unclear and require a rescan/manual review.
- [ ] Evaluate carrier/layout models against the held-out test set and real pilot audits.
- [ ] Package the approved model for offline Android use. Model updates must be versioned, signed, and reversible.

**Acceptance gate:** compared with the v0.12 baseline, the model materially improves first-pass reads and/or reduces false alerts without increasing false package confirmations.

## Phase 5 — Controlled training, release, and further testing

- [ ] Split data by property/date/label source so near-duplicate labels do not leak between training and test sets.
- [ ] Track precision, recall, false-confirm rate, false-alert rate, unit/address/name mismatch accuracy, and scan latency overall and by carrier/label condition.
- [ ] Establish release thresholds before training; false confirmations should be treated as the highest-risk failure.
- [ ] Validate every candidate model against the fixed held-out test set and a small real-world pilot before release.
- [ ] Roll out to a small pilot group first, collect only approved diagnostic samples, then expand after review.
- [ ] Support rollback to the last approved scanner/model version from the Mac app and phone app.
- [ ] Maintain a model card for each release: training-data version, known limits, metrics, supported carriers/layouts, privacy review, and rollback version.

**Acceptance gate:** a model release has demonstrated a measurable improvement in live use, meets predefined safety thresholds, and can be rolled back immediately.

## Phase 6 — Product hardening and scale

- [ ] Define roles: auditor, manager/reviewer, property administrator, and support; enforce least-privilege access.
- [ ] Add audit/export backup and restore, immutable change history, and a clear incident-correction workflow.
- [ ] Establish property onboarding: building profile, area list, audit import format, staff training, and acceptance test.
- [ ] Perform a security/privacy review before any multi-property deployment.
- [ ] Plan signed installers, automatic update policy, crash reporting that excludes package/resident data, and support procedures.
- [ ] Decide whether the product remains Mac-plus-phone, evolves into a tablet/phone-only application, or supports both. The matching and model logic should be portable so this is a product decision rather than a rewrite.

## Decisions required before Phase 2 or 3

- [ ] Management approval for resident-last-name processing and a written data-retention policy.
- [ ] Who owns and may access diagnostic label samples and derived training data.
- [ ] Cloud provider or self-hosted environment for the API, object storage, review database, and backups.
- [ ] Pilot property/properties, authorized staff, and a process for resident/privacy requests.
- [ ] The acceptable tradeoff between automatic convenience and a conservative review alert.

## Field-test checklist

Use this after every audit until the metrics are stable:

- [ ] Record app versions and property identifier.
- [ ] Record total packages and total unique units.
- [ ] Record first-pass scans, rescans, manual entries, unmatched scans, and alerts resolved.
- [ ] Record the label/carrier condition for scanner failures without storing names, full addresses, or tracking numbers.
- [ ] Confirm every exported alert is reviewed and the audit-area checklist is complete.
- [ ] Add only reproducible issues to the product backlog, with steps to reproduce and expected behavior.
