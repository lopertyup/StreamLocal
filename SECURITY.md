# Security Policy

Do not publish real user tokens, cookies, authorization headers, API keys,
local logs, or runtime state files.

If you find a committed secret, rotate or revoke it before reporting the issue.
Then open a private report with the affected file path and commit reference.

Before publishing a fork or release, run a local search for:

```text
token
secret
password
api_key
authorization
cookie
local user paths
```
