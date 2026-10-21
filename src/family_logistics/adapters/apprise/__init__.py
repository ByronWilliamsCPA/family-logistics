"""Apprise notification adapter.

TODO(M3): POST /notify/{tag} to the Apprise API with the configured tags. Never
claim delivery unless Apprise confirms submission; log provider failures. In
shadow mode route only the Sunday report to Morgan. Routing to people lives in
Apprise configuration, not here.
"""
