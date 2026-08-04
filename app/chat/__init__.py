"""Chat: what Clerk sounds like, and what Clerk can actually do.

Two modules, and the split is the point.

``persona.py`` is prompt text and a deterministic pre-screen. It holds no
session, no tools and no database, and it cannot grant authority.

``tools.py`` is the security boundary. Every read it makes is scoped to one
company, every write goes through the same permission gate as the screens, and
the model chooses only WHICH tool and the non-identity arguments. A model that
could pass a company_id is a model that can be talked into passing a different
one, so it never gets to.

Nothing is re-exported here. Importing this package must not drag in a database
session or a web framework, so a caller names the module it wants.
"""
