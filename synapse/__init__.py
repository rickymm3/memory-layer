"""Synapse: the social layer built on the memory core.

Posts, discussions, the explore feed and their workers. Synapse depends on
the core (app/, mcp_server/); the core never imports Synapse. Importing this
package registers Synapse's handlers for the core's commit events.
"""
from synapse.hooks import register as _register

_register()
