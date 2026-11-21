"""The template for a new flow, as a TOML string (a flow ships as Python data because `ws secret wall` copies only `.py` files)."""

TOML = """\
# Template for a connect flow. Copy this file to <id>.py (shipped flows are Python data, TOML in a string), or the text
# to <id>.toml for a flow outside the package. Change every value, then run
#   ws connect <id> --dry-run --json
# The steps are a closed set: info, link, capture, verify. docs/how-to/add-a-connect-flow.md has the rules.
schema = "ws.connect-flow/1"
id = "example"
title = "Connect Example"
# The name the stable id from the verify is kept under (never a host, address or display name).
unique_key = "account_id"

# Leave this block out if the flow stores no secret. The person adds the registry entry; ws never edits it.
[secret]
name = "EXAMPLE_TOKEN"
hosts = ["api.example.com"]

[[steps]]
id = "get-token"
type = "info"
title = "Make a token"
text = "Say what the person does on the other side, in one or two plain sentences."

[[steps]]
id = "open-page"
type = "link"
title = "Open the page"
text = "One line on what the page is."
url = "https://example.com/settings/tokens"

[[steps]]
id = "token"
type = "capture"
title = "Paste the token"
fields = [{ name = "token", label = "Token", secret = true, pattern = '[A-Za-z0-9_-]{20,}' }]
[steps.revoke]
action = "delete_secret"

# A verify after a secret capture is a real call. {secret} goes only to a host in [secret] hosts. The secret is stored
# only if this passes.
[[steps]]
id = "who-am-i"
type = "verify"
title = "Check the token"
identity = "name"
unique_id = "id"
[steps.request]
url = "https://api.example.com/v1/me?token={secret}"
[steps.revoke]
action = "tell"
text = "Say how to make the token useless on the service's side."
"""
