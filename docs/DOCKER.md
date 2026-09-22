# Docker

The Compose stack runs three services:

- `chatgpt-api`: main Bridge API on `http://127.0.0.1:8000`
- `bridge-console`: operator console on `http://127.0.0.1:8080`
- `character-game`: roleplay game use-case on `http://127.0.0.1:3000`

Images do not bundle your ChatGPT account captures. Mount them from the host.

Compose publishes these ports on `127.0.0.1` by default. Set
`CHATGPT_BIND_HOST=0.0.0.0` explicitly for LAN access, together with a strong
`CHATGPT_API_KEY` and the appropriate `CHATGPT_PUBLIC_BASE_URL`.
For the prepared Windows development environment, see [LOCAL_SETUP.md](LOCAL_SETUP.md).

## Files

Expected host layout:

```text
secrets/accounts/main-free/chatgpt-request.txt
secrets/accounts/image-pro/chatgpt-request.txt
secrets/accounts/research-pro/chatgpt-request.txt
outputs/
```

Use any subset and any ASCII alias you want. `main-free`, `image-pro`,
`research-pro`, and `team_plus` are examples, not required names. Account names
are local aliases, not automatic plan selectors.

Do not commit `secrets/`. The captures contain live browser credentials.

## Build

```sh
docker build -t chatgpt-api:local .
```

The Compose stack uses production-style images:

- API builds a Python wheel in a build stage, then installs that wheel into the
  runtime image.
- Console builds static Vite assets, then serves only the compiled `dist/`
  through nginx.
- Character game builds the SvelteKit node output, then runs `build/index.js`
  with production dependencies.

Local dev ports such as Vite `5173`/`5174` are not used by Docker defaults.

## Run With Compose

```sh
cp .env.example .env
docker compose up --build
```

Then check:

```sh
curl -H 'Authorization: Bearer local-dev-key' http://127.0.0.1:8000/health
curl -H 'Authorization: Bearer local-dev-key' http://127.0.0.1:8000/v1/models
open http://127.0.0.1:8080
open http://127.0.0.1:3000
```

`/health` and `/v1/models` only prove the Docker services are online. Real
chat, image, vision, and research calls still need at least one valid account
capture mounted or added through the console/CLI.

## Run With Docker

```sh
docker run --rm \
  -p 8000:8000 \
  --env-file .env \
  -v "$PWD/secrets/accounts:/data/secrets/accounts" \
  -v "$PWD/outputs:/data/outputs" \
  chatgpt-api:local
```

Use read-write account mounts if you want the console or CLI to add, update, or
delete captures from inside Docker. Use `:ro` only for locked production
deployments where account captures are managed outside the container.

## Important Environment

`CHATGPT_API_KEY`
: Bearer token required by local clients. Default example is `local-dev-key`.

`BRIDGE_CONSOLE_PORT`
: Host port for the operator console. Default `8080`. The container serves
  prebuilt static files through nginx on internal port `80`.

`CHARACTER_GAME_PORT`
: Host port for the character game use-case. Default `3000`. The container runs
  the SvelteKit node build output on internal port `3000`.

`CHATGPT_CONSOLE_URL`
: Console URL reported by API logs, `/admin`, and admin status. Docker default
  is `http://127.0.0.1:8080`.

`CHATGPT_CONSOLE_COMMAND`
: Operator command reported by the API for launching the console. Docker default
  is `docker compose up -d bridge-console`; local development can override it
  with `bun --cwd apps/bridge-console dev`.

`CHATGPT_ACCOUNTS`
: Optional comma-separated account names to route, for example
  `main-free,image-pro,research-pro`. Leave it blank to auto-discover every saved
  capture under `secrets/accounts/*`. These names are local aliases from
  `secrets/accounts/<name>/`, not automatic plan selectors.

`CHATGPT_ACCOUNTS_DIR`
: Mounted account capture directory. In Docker this is `/data/secrets/accounts`.

`CHATGPT_PUBLIC_BASE_URL`
: Public `/v1` base URL embedded in artifact download links. For LAN use, set
  this to the machine address, for example `http://192.168.1.203:8000/v1`.

`CHATGAME_OPENAI_BASE_URL`
: Server-side API URL used by the character-game container. Docker compose keeps
  this as the internal service URL `http://chatgpt-api:8000/v1`.

`CHATGAME_PUBLIC_OPENAI_BASE_URL`
: Browser-facing API URL shown in the game UI and persisted in route settings.
  Docker compose defaults it from `CHATGPT_PUBLIC_BASE_URL`, so LAN users should
  set only `CHATGPT_PUBLIC_BASE_URL=http://<LAN-IP>:8000/v1` in most cases.

`CHATGPT_CHAT_CONCURRENCY`
: Local chat throttles. Recommended defaults are `free=1,go=2,plus=3,pro=4`.

`CHATGPT_UPLOAD_CONCURRENCY`
: Local source-image upload throttles for OCR, describe, chat-with-image, image
  edit, and composite requests. These routes also use ChatGPT `file_upload`
  quota when reported. Recommended defaults are `free=1,go=1,plus=1,pro=1`.

`CHATGPT_IMAGE_CONCURRENCY`
: Local image throttles. Recommended defaults are `free=1,go=1,plus=2,pro=3`.

`CHATGPT_RESEARCH_CONCURRENCY`
: Local Deep Research throttles. Recommended defaults are
  `free=1,go=1,plus=2,pro=2`.

The Docker image does not migrate account files. Existing captures under the
mounted `./secrets/accounts` directory keep working after rebuilds. Existing
outputs and artifact metadata keep working when `./outputs` stays mounted.
Newer routing only changes account ordering by reported usage; it does not
change the capture file format.

## CLI Checks

From the host:

```sh
python3 -m chatgpt_api doctor --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api admin capacity --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api admin models --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api api health --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api api chat --message "Reply with exactly: docker bridge ok" --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
python3 -m chatgpt_api api chat --message "Reply with exactly: pinned route ok" --accounts main-free,image-pro --account-strategy random --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

From inside the container:

```sh
docker compose exec chatgpt-api python3 -m chatgpt_api doctor --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
docker compose exec chatgpt-api python3 -m chatgpt_api api health --base-url http://127.0.0.1:8000/v1 --api-key local-dev-key
```

## Account Management

Add or update an account through the console at `http://127.0.0.1:8080`, or
paste a capture through the running Docker API:

```sh
docker compose exec -it chatgpt-api chatgpt-api admin account add \
  --account main-free \
  --paste \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

Paste the full copied Network request or cURL capture, then finish with a line
containing only `END_CAPTURE`. The command writes the capture into the mounted
`/data/secrets/accounts/<account>/` directory only after validation passes.

The command inspects the capture first and refuses to save if required or
recommended checks fail. By default it also runs a live account probe after
saving.

Delete a local account:

```sh
docker compose exec chatgpt-api chatgpt-api admin account delete \
  --account old-free \
  --base-url http://127.0.0.1:8000/v1 \
  --api-key local-dev-key
```

## Artifact Downloads

Generated images and Deep Research reports are stored under `/data/outputs`.
Clients should use `download_url` values returned by the API:

```text
GET/HEAD /v1/chatgpt/files/{file_id}/{filename}
```

Use local `path` values only for scripts running on the same machine or inside
the same container volume.

Image edit outputs use the same image artifact store as normal image generation.
Vision/OCR does not create a file; it returns text in the JSON response. If
clients connect over LAN, set `CHATGPT_PUBLIC_BASE_URL` to the reachable
machine URL so `download_url` does not point to `127.0.0.1` on the client
device.
