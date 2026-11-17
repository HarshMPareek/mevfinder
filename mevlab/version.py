"""Version of the classifier rules. Every saved row carries it; change it whenever a rule changes what is counted."""

from __future__ import annotations

# 0.4.0: V4 hook cuts paid by the PoolManager netted out of the leg (C-CLS-30: fee events + PM -> non-party
#        transfers); zero-amount hook swaps priced by the path when it closes, still flagged (C-CLS-31)
# 0.3.0: proxy-WETH mint/burn = wrap (C-CLS-21), probe excludes token events (C-CLS-23);
# 0.2.0: Algebra, DODO, LB and the two unnamed Base families decoded
CLASSIFIER_VERSION = "0.4.2-base-port.6"
