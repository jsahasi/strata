"""The seam between the chat screen and the answering engine.

WHY THIS FILE EXISTS, and it is not a flattering reason. app/web/views/chat.py
looks up its engine by name at call time -- `app.chat.engine.answer` -- with a
docstring saying the seam closes "the moment app/chat/engine.py lands". The
engine landed as app/chat/agent.py, publishing `run_turn`. Two agents building
either side of one seam named it two different ways, and because the view treats
a missing engine as a legitimate state rather than an error, the product did the
worst possible thing: it answered every question with a polite, honest, entirely
unnecessary "the part of me that reads the record is not wired in".

Nothing failed. No test went red. The fallback announced itself exactly as
designed, and the design was right -- it is only that the thing it announced was
not true. The same shape as a router nobody mounted and a stylesheet nobody
loaded, both of which this repository has already produced today.

So this module is a name, not a layer. It resolves the four things run_turn
needs that the view does not have, and gets out of the way.

WHAT IT SUPPLIES:
  transport  -- from the environment, so a deployment with a key uses the model
                and one without it still serves every screen and says so.
  toolset    -- derived from app/chat/tools.py REGISTRY by agent.default_toolset.
  dispatch   -- app/chat/tools.py dispatch, which injects identity the model
                never sees.
  company_name -- resolved the way every screen resolves it, from app/web/deps,
                because run_turn's own docstring says asking a second module the
                same question is how two answers appear.

WHAT IT DROPS. The view passes `history`. run_turn does not take it and does not
want it: it screens, then asks, then returns. Passing prior turns back into the
model is a real feature and a real decision -- it changes what the model can be
talked into across a conversation -- and inventing it inside a shim would be
smuggling in a behaviour nobody chose. The parameter is accepted and ignored,
and this sentence is why.
"""

from __future__ import annotations

from typing import Any

from app.chat import agent as _agent
from app.chat import tools as _tools

__all__ = ["answer"]


def answer(
    session,
    *,
    company_id: str,
    actor,
    message: str,
    surface: str = "",
    project_id: str | None = None,
    history: Any = None,
    **_ignored: Any,
) -> dict:
    """One turn, in the shape app/web/views/chat.py reads.

    Extra keyword arguments are swallowed on purpose. This is a seam between two
    modules that already disagreed once about its name; a shim that raises on an
    argument it does not recognise would turn the next small disagreement into a
    500 on a screen that is meant to degrade politely.
    """
    from app.web.deps import company_name as _company_name

    turn = _agent.run_turn(
        session,
        company_id=company_id,
        actor=actor,
        company_name=_company_name(company_id),
        message=message,
        surface=surface,
        project_id=project_id,
        toolset=_agent.default_toolset(),
        dispatch=_tools.dispatch,
        transport=_agent.transport_from_environment(),
    )
    return turn.as_dict()
