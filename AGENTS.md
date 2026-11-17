# Telegram account access

The user explicitly permits only reading data from Telegram accounts.
Never send messages, media, reactions, typing actions, read receipts, drafts,
join/leave requests, gifts, or account/profile changes. Never disable this rule
or the read-only RPC allowlist to make a crawl succeed.

Use an existing authenticated session. Do not call Client.start/initialize,
launch new authentication, or start message handlers while collecting data.
Transport acknowledgements are not chat read receipts.

Keep tokens and session data out of source control, logs, and exports.
