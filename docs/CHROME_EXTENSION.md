# Chrome account connector

This connector uses a normal Chrome profile, so the first ChatGPT login stays in
Chrome. It does not automate a login form or bypass CAPTCHA. The extension asks
only for a bridge IP when the bridge is on another computer. The API port is
always `8000`.

## Setup

1. Start the API and console with `docker compose up -d --build chatgpt-api bridge-console`.
2. In Chrome, open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select `extensions/chrome-bridge` from this repository.
3. In that same Chrome profile, open `https://chatgpt.com` and sign in normally.
4. Open the extension and choose **Conectar ahora**. It tests `127.0.0.1`, then `localhost`. If the bridge is on a different LAN computer, type only that computer's private IP. The extension then tries that IP on port `8000`. Pairing and the first renewal complete inside the extension; no Accounts approval is needed.
5. The pulsing icon and popup show the matched account, expiry, last renewal, and status: green ready, yellow expiring, red attention, blue connecting, gray paused. Press the pulse or switch to pause/resume automatic renewal. **Renovar ahora** runs a manual refresh.
6. The background worker checks every 12 hours. With automatic renewal enabled, it renews within two days of expiry or after expiry. After three failed attempts it shows a red status for manual intervention. A successful automatic refresh leaves a check in the popup and toolbar badge; it does not send a notification.

For LAN use, set `CHATGPT_BIND_HOST=0.0.0.0` and set a strong
`CHATGPT_API_KEY` in your private `.env`, then restart Docker. Permit TCP 8000
only on the trusted LAN. The extension requests Chrome's host permission for the
specific IP on connection. First pairing, including localhost, asks ChatGPT to
verify the browser cookies and account identity. Arbitrary
LAN subnet discovery is not available to ordinary Manifest V3 extensions;
enter the bridge IP if localhost fails.

The connector only refreshes an **existing** account capture. First registration
still uses the manual Accounts flow. It preserves the existing request body and
updates the Authorization token and ChatGPT cookies. A different ChatGPT user,
account, or email is rejected. Captures remain encrypted on disk. The LAN
payload is encrypted with an ephemeral AES key protected by the bridge's pinned
RSA key; pairing tokens are encrypted to the extension's key. On first LAN use,
an IP address alone cannot authenticate the server against a network attacker;
connect only on a trusted LAN. Once linked, the extension blocks a changed
server fingerprint. Revoking the client in Accounts invalidates its pairing.

The status is an expiry estimate from the token and a check of the browser
session. A token with a future expiry can still be revoked upstream; the popup
cannot promise validity without a live ChatGPT request. If `/api/auth/session`
stops returning an access token, sign in manually and resolve any challenge in
the normal Chrome tab.

| Route | Format | Auth | Purpose |
| --- | --- | --- | --- |
| `GET /v1/chatgpt/extension/discover` | JSON | none | Detect bridge and obtain public key fingerprint |
| `POST /v1/chatgpt/extension/activate` | encrypted JSON | matching ChatGPT session | Link an existing account and perform the first refresh |
| `POST /v1/chatgpt/extension/health` | encrypted JSON | paired client | Read the assigned capture's expiry status |
| `POST /v1/chatgpt/extension/sync` | encrypted JSON | paired client | Update the existing encrypted capture |
| `POST /v1/chatgpt/extension/report` | encrypted JSON | paired client | Report that Chrome needs manual sign-in or has a network problem |
| `GET /v1/chatgpt/admin/extension` | JSON | API bearer key | Read account and connector status for the dashboard |
| `POST /v1/chatgpt/admin/extension/revoke` | JSON | API bearer key | Revoke a paired client |

The extension routes are intended for the bundled extension. Never publish the
bridge directly on the public Internet.
