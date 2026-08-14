# Vulture whitelist. Read by the global pre-commit hook.
#
# Everything here is called by a framework, not by our own code, so static
# analysis cannot see the caller. Do not add a name here to silence a real
# dead-code finding. Delete the code instead.

# FastAPI route handlers. Invoked by the @app.get / @app.post decorators.
landing  # unused function (main.py)
x402_discovery  # unused function (main.py)
health  # unused function (main.py)
buy  # unused function (main.py)
research  # unused function (main.py)

# Facilitator protocol methods. Invoked by x402ResourceServer, not directly.
_.verify  # unused method (main.py, LocalBaseFacilitator)
_.settle  # unused method (main.py, LocalBaseFacilitator)

# stripe.api_key is module-level config on the stripe SDK, set before each call.
_.api_key  # unused attribute (main.py)
