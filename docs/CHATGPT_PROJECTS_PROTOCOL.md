# Routing de ChatGPT Projects

El bridge permite elegir un Project al iniciar una conversación OpenAI-compatible.
La selección es opcional. Si se omite, la conversación se crea fuera de
Projects.

## Evidencia observada

La captura real de una conversación de **INCIDENCIAS** contiene:

```json
{
  "conversation_mode": {
    "kind": "gizmo_interaction",
    "gizmo_id": "<redacted-project-id>"
  }
}
```

El id real, los mensajes, cookies, bearer y demás identificadores se mantienen
fuera del repositorio. La ruta normal usa explícitamente:

```json
{"conversation_mode":{"kind":"primary_assistant"}}
```

El transport no copia `conversation_mode` de la captura base. Esto es esencial
cuando la captura se obtuvo dentro de un Project: sin selección, el chat sigue
siendo normal; con selección, el mapping local aporta el `gizmo_id`.

## Mappings locales

Los mappings viven en la tabla SQLite `project_mappings` y relacionan:

- nombre visible y alias normalizado;
- id real del Project;
- cuenta local propietaria;
- estado y fecha de verificación.

El listado administrativo enmascara el id. Se puede crear o actualizar desde
`http://127.0.0.1:8080/#projects` o mediante:

```http
POST /v1/chatgpt/admin/projects/save
Authorization: Bearer <bridge-key>
Content-Type: application/json

{
  "name": "INVESTIGACION",
  "alias": "investigacion",
  "project_id": "<local-project-id>",
  "account": "info-gpt-work"
}
```

Los nombres no distinguen mayúsculas ni acentos. `INVESTIGACIÓN`,
`INVESTIGACION` e `investigacion` resuelven al mismo alias normalizado.

## Iniciar un chat

Usa el nombre o alias en `chatgpt_project`:

```json
{
  "model": "auto",
  "chatgpt_project": "INVESTIGACION",
  "messages": [{"role":"user","content":"Resume el estado actual."}]
}
```

También se acepta `X-ChatGPT-Project` cuando el body no especifica selección.
La precedencia es body, metadata, header. El mapping fuerza su cuenta; una
cuenta explícita diferente produce HTTP 400. No se permite combinar un Project
con una lista de cuentas porque el id pertenece a una sola cuenta.

Para crear fuera de Projects, omite el campo o selecciona **Outside Projects**
en Test Lab. Los chats de Project desactivan temporary chat, ya que deben quedar
asociados al Project.

## Límites y validación

El protocolo web no es oficial y puede cambiar. Las pruebas unitarias fijan la
forma observada, la resolución de alias, la precedencia, el aislamiento del
chat normal y el enmascarado. Las pruebas live deben usar mensajes inocuos,
comprobar INCIDENCIAS, INVESTIGACION y chat normal, y no registrar contenido.

La captura disponible demuestra INCIDENCIAS. INVESTIGACION fue identificado en
la misma cuenta mediante su URL de Project y se guarda sólo como mapping local.
Una verificación live de ambos Projects sigue siendo necesaria después de cada
cambio relevante de ChatGPT Web.
