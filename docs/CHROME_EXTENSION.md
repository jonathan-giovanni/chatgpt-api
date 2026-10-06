# Chrome account connector

This connector uses a normal Chrome profile, so the first ChatGPT login stays in
Chrome. It does not automate a login form or bypass CAPTCHA. The extension asks
only for a bridge IP when the bridge is on another computer. The API port is
always `8000`.

## Setup

1. Start the API and console with `docker compose up -d --build chatgpt-api bridge-console`.
2. In Chrome, open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select `extensions/chrome-bridge` from this repository.
3. In that same Chrome profile, open `https://chatgpt.com` and sign in normally.
4. Open the extension and choose **Detectar y conectar**. It tests `127.0.0.1`, then `localhost`. If the bridge is on a different LAN computer, type only that computer's private IP. The extension then tries that IP on port `8000`.
5. Open Bridge Console → **Accounts**. Compare the six-digit code and the server fingerprint shown by the extension with the dashboard. Select the existing account alias and approve. Reopen the popup if it was closed; it will finish pairing and refresh the capture.
6. Check the extension badge and popup. **Renovar ahora** runs an immediate refresh. The background worker checks every 12 hours and renews when the capture expires within four days or has not been refreshed for three days. After three failed attempts it shows an error for manual intervention.

For LAN use, set `CHATGPT_BIND_HOST=0.0.0.0` and set a strong
`CHATGPT_API_KEY` in your private `.env`, then restart Docker. Permit TCP 8000
only on the trusted LAN. The extension requests Chrome's host permission for the
specific IP on connection. Arbitrary LAN subnet discovery is not available to
ordinary Manifest V3 extensions; enter the bridge IP if localhost fails.

The connector only refreshes an **existing** account capture. First registration
still uses the manual Accounts flow. It preserves the existing request body and
updates the Authorization token and ChatGPT cookies. A different ChatGPT user,
account, or email is rejected. Captures remain encrypted on disk. The LAN
payload is encrypted with an ephemeral AES key protected by the bridge's pinned
RSA key; pairing tokens are encrypted to the extension's key. Compare the
fingerprint during first pairing to detect a substituted LAN server. Revoking
the client in Accounts invalidates its pairing.

The status is an expiry estimate from the token and a check of the browser
session. A token with a future expiry can still be revoked upstream; the popup
cannot promise validity without a live ChatGPT request. If `/api/auth/session`
stops returning an access token, sign in manually and resolve any challenge in
the normal Chrome tab.

| Route | Format | Auth | Purpose |
| --- | --- | --- | --- |
| `GET /v1/chatgpt/extension/discover` | JSON | none | Detect bridge and obtain public key fingerprint |
| `POST /v1/chatgpt/extension/pair` | JSON | dashboard approval | Start a ten-minute pairing request |
| `GET /v1/chatgpt/extension/pair/status` | JSON | opaque request ID | Receive the encrypted pairing token after approval |
| `POST /v1/chatgpt/extension/health` | encrypted JSON | paired client | Read the assigned capture's expiry status |
| `POST /v1/chatgpt/extension/sync` | encrypted JSON | paired client | Update the existing encrypted capture |
| `GET /v1/chatgpt/admin/extension` | JSON | API bearer key | List pending/paired clients in Accounts |
| `POST /v1/chatgpt/admin/extension/approve` | JSON | API bearer key | Assign a pending client to an account |
| `POST /v1/chatgpt/admin/extension/revoke` | JSON | API bearer key | Revoke a paired client |

The extension routes are intended for the bundled extension. Never publish the
bridge directly on the public Internet.
