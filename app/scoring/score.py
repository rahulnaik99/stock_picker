"""OBSOLETE — do not use. This file is a leftover from an early version of the policy engine.

The real deterministic scoring is in `app/scoring/policy.py` (compute_policy, finalize, rank).
This file had stale AGENT_KEYS (missing 'trader', 'indicator' misspelled as 'indicators') and
imported `app.service.config.load_weights` which is not the production code path.
It is kept only so imports don't silently break; nothing should import from here.
"""
raise ImportError(
    "app.scoring.score is obsolete. Import from app.scoring.policy instead."
)
