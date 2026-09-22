# Entorno local de Windows

Preparación del 22 de septiembre de 2026. Esta etapa se limita al entorno y a
la validación del proyecto existente. El bootstrap de GitHub, la modernización
de modelos y Projects descritos en los documentos adjuntos son fases posteriores.

## Arranque

Desde la raíz del checkout, con Docker Desktop iniciado:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
. ./scripts/Enter-LocalEnvironment.ps1
rtk proxy docker compose up -d --build
rtk proxy docker compose ps
```

| Servicio | Dirección |
| --- | --- |
| Bridge Console | http://127.0.0.1:8080 |
| API | http://127.0.0.1:8000/v1 |
| Character Game | http://127.0.0.1:3000 |

La configuración inicial usa la clave de desarrollo del ejemplo,
`local-dev-key`. `.env`, `secrets/`, `.venv/` y `outputs/` están excluidos de Git.
Los tres puertos se publican solamente en `127.0.0.1`. Para habilitar acceso
desde LAN deliberadamente, configura `CHATGPT_BIND_HOST=0.0.0.0`, una clave
propia y `CHATGPT_PUBLIC_BASE_URL`, siguiendo `docs/DOCKER.md`.

```powershell
rtk proxy curl.exe --fail -H 'Authorization: Bearer local-dev-key' http://127.0.0.1:8000/health
rtk proxy curl.exe --fail -H 'Authorization: Bearer local-dev-key' http://127.0.0.1:8000/v1/models
rtk proxy docker compose stop
rtk proxy docker compose start
```

`stop` conserva contenedores y datos. Las cuentas se montan desde
`secrets/accounts/` y la base SQLite y los artefactos desde `outputs/`.

## Herramientas preparadas

- Python 3.12.13 mediante `uv`, con instalación editable `.[dev]` en `.venv`.
- Node 22.23.2 en `%USERPROFILE%/.local/share/node/node-v22.23.2-win-x64`.
  Se verificó el SHA-256 del ZIP con el manifiesto de nodejs.org.
- Bun 1.4.2 instalado mediante npm en el perfil de usuario.
- RTK 0.49.0 en `%USERPROFILE%/.local/bin`.
- Docker Desktop 4.29.0, Docker Engine 26.0.0 y Compose 2.26.1 existentes.
- WSL actualizado de 2.5.7 a 2.7.14 mediante `wsl --update --web-download`.

El script `Enter-LocalEnvironment.ps1` está preparado para las rutas anteriores:
antepone las herramientas al PATH de la terminal actual y no cambia el PATH
global ni carga credenciales. El Node 18 del sistema se conserva.

WSL 2.5.7 impedía importar `docker-desktop-data` con
`WSL_E_NOT_A_LINUX_DISTRO`. Tras actualizar WSL y reiniciar Docker, el motor
arrancó. No se borraron distribuciones, volúmenes ni datos existentes.
Referencia: [incidencia de Docker/WSL](https://github.com/docker/for-win/issues/14802).

## Comprobaciones reproducibles

```powershell
. ./scripts/Enter-LocalEnvironment.ps1
rtk proxy python -m compileall -q chatgpt_api tests
rtk proxy python -m pytest -q
rtk proxy bun --cwd apps/bridge-console install --frozen-lockfile
rtk proxy bun --cwd apps/bridge-console run check
rtk proxy bun --cwd apps/bridge-console run build
rtk proxy bun --cwd apps/character-game install --frozen-lockfile
rtk proxy bun --cwd apps/character-game run check
rtk proxy bun --cwd apps/character-game run test
rtk proxy bun --cwd apps/character-game run build
rtk proxy docker compose config --quiet
rtk proxy docker compose build
```

La primera instalación simultánea de Bun falló al extraer una dependencia de
la consola. La repetición con cachés separadas en `outputs/bun-cache-console`
y `outputs/bun-cache-game` completó ambas instalaciones sin cambiar los lockfiles.
El juego también necesitó Node 22 y Bun en el PATH para sus dependencias nativas.

La construcción Docker inicial del juego falló en
`npm install --omit=dev --ignore-scripts` con
`Cannot read properties of null (reading 'edgesOut')`. Su etapa de dependencias
de producción ahora usa `bun install --production --frozen-lockfile --ignore-scripts`
y conserva `npm rebuild better-sqlite3` bajo Node 22. Así se respeta el lockfile
existente y el módulo SQLite se recompila para el runtime final.

## Límite de esta validación

Resultados del baseline:

| Comprobación | Resultado |
| --- | --- |
| `compileall` en Windows y Linux | PASS |
| Python en Windows, antes de cambios | 189 passed, 1 failed en 14.51 s |
| Python 3.12 en contenedor Linux, código y tests sin cambios | 190 passed en 11.16 s |
| Bridge Console `check` | 0 errores, 0 avisos |
| Bridge Console `build` | PASS |
| Character Game `check` | 0 errores, 0 avisos |
| Character Game `test` | 1 archivo, 6 tests correctos |
| Character Game `build` | PASS |
| `docker compose build` tras corregir la etapa de producción del juego | PASS, las tres imágenes |
| `docker compose up -d --wait --wait-timeout 90` | PASS, tres servicios activos; API healthy |
| API `/health`, `/v1/models`, `/v1/chatgpt/admin/status` | HTTP 200; 5 aliases publicados |
| `/v1/models` sin bearer | HTTP 401 |
| Consola en navegador | ONLINE; navegación a Accounts verificada |
| Base SQLite de administración | Creada en el volumen persistente |
| Consola `/` y `/health` | HTTP 200 |
| Juego `/` y `/api/status` | HTTP 200 |
| Juego en navegador | API online; bearer aceptado |
| `better-sqlite3` en el contenedor final con Node 22 | `SELECT 1 AS ok` devuelve `{ ok: 1 }` |
| `git diff --check` | PASS |

La ejecución Linux copió únicamente código, tests y manifiestos a un directorio
temporal del contenedor, instaló `.[dev]` y ejecutó `compileall` y `pytest -q`.
El checkout se montó en modo de solo lectura; la comprobación de permisos se
ejecutó sobre el sistema de archivos Linux, no sobre el volumen Windows.

Después del baseline se añadió una captura real bajo el alias local
`info-gpt-work` y se verificó sin registrar credenciales ni contenido. La cuenta
responde como Plus y expone el modelo interno observado
`gpt-5-6-thinking`. El alias histórico `free` contiene actualmente una copia de
la misma captura; se conserva hasta depurar el routing y no debe interpretarse
como una segunda cuenta. Las capturas viven bajo `secrets/` y siguen excluidas
de Git.

En Windows nativo, la suite inicial dio **189 passed, 1 failed** (14.51 s).
`test_load_secrets_key_creates_owner_only_key_file` exige permisos POSIX `0600`;
Windows devuelve `0666` mediante `stat`. El test no se omitió ni se alteró.
La protección equivalente mediante ACL de Windows queda pendiente: se recomienda
operar el servicio con los contenedores Linux para esta fase.
