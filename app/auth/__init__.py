"""Authentication and authorisation. Who is here, and what they may do.

Nothing is re-exported from this package. A caller imports from the module that
owns the name -- app.auth.sessions for logging in, out and resolving a token,
app.auth.policy for the permission and approval checks -- so there is one import
path to each of them, for the same reason app/state/identity.py does not
re-export the audit action codes. Two paths to one function is how half the
callers end up on the copy that was not updated.

app/state/identity.py, the layer below, answers what permissions a user holds.
app/auth/policy.py answers whether a specific act is allowed, which is a
different question: its approval check refuses people who hold the permission.
app/auth/sessions.py answers the question before both of them -- who is holding
this request -- and it is the only module here that writes to the audit chain on
its own account, because a login is a decision and a refused login is evidence.
"""
