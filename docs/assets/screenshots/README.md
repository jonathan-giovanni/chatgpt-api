# Screenshot Evidence

The current Console captures come from the local Docker stack. Before
committing them, the account header was cropped out and identifiers, expiry
dates, Project IDs, and attachment batch IDs were replaced with examples. They
do not show account usage, account secrets, or private session data.

| File | Console view | What it shows |
| --- | --- | --- |
| `console-test-lab-voice.png` | `#test-lab` | Optional Project and conversation UUID, model, local audio source, and Arbor voice in the same workflow. |
| `console-project-routing.png` | `#projects` | Friendly Project mapping and routing; example names and a hidden Project ID. |
| `console-sip-attachments.png` | `#test-lab` | A synthetic two-file SIP attachment batch and the generated Docker gateway command. |
| `console-chrome-connector.png` | `#accounts` | Extension pairing status in the Console with an example account label and hidden date. |
| `oss-game-setup.png` | Character game | Separate example client calling the local API. |
| `character-game-redesign.png` | Character game | Example route selection and game layout. |

To recapture, run `docker compose up -d --build` and open the relevant local
page. Remove account names, counts, usage, reset/expiry dates, Project IDs,
tokens, cookies, captures, and local paths **before** adding any new image to
Git. The API's default development key is an example only; never screenshot a
real key. Review the resulting PNG visually, including its background and
header, before staging it.
