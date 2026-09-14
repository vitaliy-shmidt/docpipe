"""Assistant routing foundation (V2.2).

Deliberately separate from system/ai/ (extraction modes): an assistant
*question* and an extraction *mode* are different concepts with different
inputs (free-text question vs. a document's extracted text) and different
trust boundaries (server-supplied, pre-vetted context vs. client-supplied
document text) - see docs/assistant-routing.md.

This package is routing + prompting only. It never queries a database,
never calls out to HubDix, and never generates SQL - see modes.py and
router.py docstrings for the exact boundary.
"""
