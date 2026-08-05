"""Interpretation: the stage between the diff and the state.

DELIBERATELY EMPTY OF RE-EXPORTS. Convenience imports here -- `from .propose
import judge_materiality` -- would mean that touching anything in this package,
including action.py, imports the proposer and everything under it. The proposer
keeps the model SDK behind a function-level import so that nothing pays for it
at import time, and a re-export line here would quietly undo half of that by
putting the module one `import app.interpretation` away from every caller.

Import the module you want.
"""
