"""Transient, deterministic preprocessing of extracted document text for AI
inference only - see docs/contract-text-optimization.md.

Extracted text is the persistent source of truth (it lives with the
consumer, e.g. HubDix' file_processing.extracted_text - DocPipe never
stores it). Everything in this package works on a per-request copy and
only ever decides *which verbatim parts* of that text an AI call gets to
see. It never summarizes, paraphrases, interprets, converts, or adds
document content.
"""
