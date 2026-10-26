# Security and local account information

Do not attach `.state`, Cookie files, authentication tokens, browser profiles, or
unredacted API logs to public issues or pull requests.

The app stores local account data under `.state` next to the executable. Windows
uses DPAPI when available. When encryption is unavailable, the app keeps login
information in the current process instead of saving plaintext credentials.

The bundled API listens on `127.0.0.1:3000`. Keep it local. A remote API must use
HTTPS and is responsible for handling the account cookies sent to it.

For bug reports, describe the action and sanitized error code. Remove account
identifiers, token values and private playlist details before sharing logs.
