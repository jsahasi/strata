"""One module per screen, each exporting a `router`.

Routers are kept separate and mounted by whoever builds the application, so a
screen can be tested on its own. Nothing shared lives here on purpose: a common
object in this file would be a thing every screen imports and every screen could
break, and the templates directory is already named once in app/web/__init__.py.
"""
