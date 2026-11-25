"""PDF4Fill form support (Phase 2.1a: inspect only).

Runs in-process with pikepdf (structure, AcroForm, security) and pdfminer.six
(text with coordinates). Stirling is deliberately not used here - see
docs/architecture/pdf4fill-capability-audit.md section 2.1 for why its form
endpoints do not meet the contract (rotation ignored, out-of-page widgets
dropped silently).
"""
