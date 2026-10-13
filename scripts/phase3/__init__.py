"""Phase 3, development, for `run.py` (IDE-147, *Phase 3 — Development*).

Kept out of run.py so the runner stays a dispatcher. One module per concern:

    orchestrator   3.1 planning integration and the 3.2 building loop
    plan           the plan committed to the feature branch and pinned by hash
    shape          the board shape planning creates, and the work list
    journal        the feature journal, a working file uploaded per sub-phase
    finalize       witnesses, the closed outcome set and its routes
    review         the rubber duck's prompt and the filter behind it
    merge          the tech lead: tested-SHA merges, one at a time
    states         todo / doing / done through engineering-catalog, or the adapter
    gitops         git, with every base a SHA
    integration    3.3 integration, the documenter, the hand-off to QA, Bugs back
"""

from . import finalize, gitops, integration, journal, merge, plan, review, shape, \
    states  # noqa: F401
from .orchestrator import Development, InlinePool, ThreadPool  # noqa: F401
