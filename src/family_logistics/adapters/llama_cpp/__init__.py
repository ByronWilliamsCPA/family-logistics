"""Narrative adapter for the local inference server's OpenAI-compatible endpoint.

TODO(M5): blocked until the local inference server is reachable from the app. Send only
pseudonymized findings (no addresses, no external children's names); validate
the JSON response schema; on failure deliver the deterministic report with a
``narrative_unavailable`` marker. LLM output never creates, dismisses,
downgrades or modifies a finding.
"""
