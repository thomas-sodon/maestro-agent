"""Phase-seam collaborators extracted from ``maestro.agent`` (proposal 068).

Each module in this package holds one phase cluster behind a narrow,
separately-testable surface; :class:`~maestro.agent.MaestroAgent` keeps
thin delegating shims so call sites stay unchanged while the bodies live
here. Collaborators import each symbol from its defining module, and tests
patch a name in the namespace of the module that reads it.
"""
