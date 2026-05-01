# AI-readable PDF text layer

This file is generated automatically. Do not edit by hand.

This directory contains an auxiliary AI-readable text layer for PDF files found across the public repositories of the LUMINA-30 GitHub organization.

## Files

- [`org-pdf-text-layer-bundle.txt`](./org-pdf-text-layer-bundle.txt) — consolidated extracted PDF text.
- [`org-pdf-text-layer-manifest.json`](./org-pdf-text-layer-manifest.json) — machine-readable metadata, Git blob SHA values, SHA256 values, extraction status, and source paths.
- [`index.html`](./index.html) — compact generated launcher page.

## AI usage note

Do not perform fresh PDF text extraction by default for the LUMINA-30 repository set.
When PDF body text is needed, first use the generated organization-wide text bundle above.
Use the manifest to check source repository, PDF path, Git blob SHA, SHA256 hash, extraction status, and generation metadata.

PDF files remain the authoritative versions. Inspect or re-extract from original PDFs only when the bundle is missing the required PDF, the manifest indicates extraction failure or OCR_REQUIRED, visual layout/figures/tables/signatures/exact formatting matter, or the user explicitly asks to verify the PDF itself.

## Garbage-prevention rule

This directory is generated-only. It is rebuilt from the current organization PDF set on every run. Deleted or renamed PDFs are not retained as stale records.
