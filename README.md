Descripción del proyecto: Chatbot de WhatsApp con interfaz Flask
Estoy desarrollando una aplicación web en Flask conectada a la API de WhatsApp Cloud que automatiza la atención al cliente mediante respuestas preconfiguradas y mensajes interactivos como botones y listas desplegables. Este chatbot está orientado a gestionar cotizaciones, preguntas frecuentes y derivar al asesor humano si se requiere.

📦 Estructura modular actual
El proyecto está dividido en carpetas y archivos para mayor claridad y mantenibilidad:

bash
Copiar
Editar
/ (raíz)
│
├── app.py                         # Archivo principal que inicia Flask y registra blueprints
├── config.py                      # Configuración de tokens y constantes del sistema
├── .env                           # Variables de entorno sensibles (token, phone ID, etc.)
│
├── /routes/                       # Blueprints con rutas
│   ├── auth_routes.py             # Login, logout, sesión
│   ├── chat_routes.py             # Vista principal del chat, mensajes, listado de chats
│   ├── configuracion.py           # Gestión de reglas y botones del chatbot
│   └── webhook.py                 # Endpoint que recibe mensajes de WhatsApp y responde
│
├── /services/                     # Lógica de negocio reutilizable
│   ├── db.py                      # Conexión y funciones sobre la base de datos SQLite
│   ├── whatsapp_api.py            # Funciones para enviar mensajes con texto, botones y listas
│   └── utils.py                   # (Reservado para funciones auxiliares si es necesario)
│
├── /templates/                    # Archivos HTML (Jinja2)
│   ├── index.html                 # Vista del chat entre clientes y asesores
│   ├── login.html                 # Formulario de inicio de sesión
│   ├── configuracion.html         # Administración de reglas del chatbot
│   └── botones.html               # Administración de botones predefinidos
│
├── /static/                       # Archivos CSS/JS si los hay
│   └── style.css                  # Estilos generales
│
├── requirements.txt               # Librerías necesarias para correr el proyecto

🔄 Funcionalidades implementadas
Gestión de usuarios y autenticación (admin)

Recepción y procesamiento de mensajes entrantes de WhatsApp vía webhook

Flujo automático basado en reglas configurables (con pasos, respuestas, tipo de mensaje y opciones)

Las reglas de un mismo paso se evalúan en orden ascendente por `id` (o columna de prioridad) para mantener un criterio consistente.

El procesamiento de listas de pasos (`step1,step2`) se realiza únicamente en memoria mediante la función `advance_steps`.

Envío de mensajes por parte del asesor desde la interfaz web

Interfaz tipo WhatsApp Web con:

Lista de clientes

Ventana de chat

Botones personalizables predefinidos

Recarga automática de mensajes

Importación de reglas y botones desde archivos .xlsx

Soporte para mensajes interactivos: texto, botones y listas desplegables

Ejemplo de `opciones` para una lista con textos personalizados y paso destino:

```json
{
  "header": "Menú principal",
  "button": "Ver opciones",
  "footer": "Selecciona una opción",
  "sections": [
    {
      "title": "Rápido",
      "rows": [
        {"id": "express", "title": "Express", "description": "1 día", "step": "cotizacion"}
      ]
    }
  ]
}
```

Cada fila puede incluir un campo opcional `step` que indica el paso destino al seleccionar esa opción.

Detección de inactividad para cerrar sesión automática del cliente

🔧 Tecnologías utilizadas
Python 3 y Flask

WhatsApp Cloud API (v17+)

MySQL como base de datos principal (SQLite opcional para desarrollo)

HTML + Jinja2 + JavaScript en el frontend

openpyxl para cargar reglas desde archivos Excel

dotenv para manejar tokens y credenciales

ThreadPoolExecutor para procesar transcripciones de audio en segundo plano (sin necesidad de Redis)
ffmpeg (binario del sistema) para normalizar los audios antes de la transcripción (instalar manualmente)
Vosk para transcribir audios en español (puedes apuntar al modelo descargado con `VOSK_MODEL_PATH`)

## Requisitos

Para ejecutar la aplicación necesitas tener instalado **ffmpeg** en el sistema.

Además, Vosk requiere un modelo de lenguaje en español. Puedes descargar uno ligero
desde https://alphacephei.com/vosk/models (por ejemplo, `vosk-model-small-es-0.42`) y
descomprimirlo en el host o volumen persistente. Luego exporta la ruta mediante:

```bash
export VOSK_MODEL_PATH=/ruta/al/vosk-model-small-es-0.42
```

Si no defines `VOSK_MODEL_PATH`, la librería intentará cargar el modelo por defecto en
español, lo que puede fallar en entornos sin conexión a internet.

### Linux (Ubuntu/Debian)

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
```

### macOS (Homebrew)

```bash
brew install ffmpeg
```

### Docker

Si usas Docker, asegúrate de añadir ffmpeg en la imagen:

```dockerfile
RUN apt-get update && apt-get install -y ffmpeg
```

✅ Estado actual
La app ya está funcionando con:

Flujo conversacional basado en reglas almacenadas en base de datos

Administración visual de botones y reglas

Sistema de login y logout

División completa en módulos con Blueprints y servicios

## HTTPS y URLs públicas detrás de un proxy

Cuando la app corre detrás de Nginx/otro proxy TLS, asegúrate de reenviar el
esquema original con `X-Forwarded-Proto` (por ejemplo `https`) para que Flask
genere URLs externas correctas. El ejemplo de `deploy/linux/nginx/nginx.conf`
ya envía ese encabezado. Si necesitas forzar un esquema específico, define la
variable de entorno `PREFERRED_URL_SCHEME=https`.


### Renovación automática de certificados (Let's Encrypt)

El stack de `deploy/linux/docker-compose.yml` incluye un servicio `certbot` que
renueva certificados cada 12 horas usando el challenge `webroot` y comparte la
ruta `/.well-known/acme-challenge/` con Nginx.

* Para la primera emisión (si aún no existe el certificado) ejecuta:

  ```bash
  docker compose -f deploy/linux/docker-compose.yml run --rm certbot certonly \
    --webroot -w /var/www/certbot \
    -d whapco.site -d app.whapco.site \
    --email TU_EMAIL --agree-tos --no-eff-email
  ```

* Luego levanta/reinicia Nginx:

  ```bash
  docker compose -f deploy/linux/docker-compose.yml up -d nginx
  ```

* El script `deploy/linux/deploy.sh` ahora intenta una renovación en cada
  despliegue y recarga Nginx al completarse.

## Diagnóstico rápido para Embedded Signup

Si el cuadro de login de Facebook no aparece en `/configuracion/signup`, sigue
estos pasos desde el contenedor web:

1. Ejecuta el chequeo básico de entorno:

   ```bash
   python scripts/check_embedded_signup.py
   ```

   Esto valida la presencia de `FACEBOOK_APP_ID`, `WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID` (o `SIGNUP_FACEBOOK`),
   `WHATSAPP_EMBEDDED_SIGNUP_REDIRECT_URI`, la resolución DNS hacia Facebook y
   que el esquema preferido sea HTTPS.

2. Carga `/configuracion/signup` y revisa la sección **Diagnóstico rápido** en la
   tarjeta de Embedded Signup. Marca en tiempo real si falta alguna variable de
   entorno, si el tenant actual no se resolvió o si el SDK de Facebook no
   respondió dentro del tiempo esperado.

3. Abre la consola del navegador (F12) para ver errores de red de `sdk.js` o
   bloqueos de terceros. Si el SDK no carga, verifica conectividad saliente o
   políticas de contenido (CSP/proxy).

4. Define `WHATSAPP_EMBEDDED_SIGNUP_REDIRECT_URI` en el entorno global con la
   URL exacta aprobada en Meta para OAuth (debe coincidir al intercambiar el
   `code` por token en `/oauth/access_token`).

## Comandos globales

El bot cuenta con comandos globales que se ejecutan antes del flujo principal.
Para agregar un nuevo comando:

1. Edita `services/global_commands.py`.
2. Crea una función que reciba el número del usuario y realice la acción deseada.
3. Registra la función en el diccionario `GLOBAL_COMMANDS` usando la palabra clave normalizada con `normalize_text`.

La función `handle_global_command` es llamada desde `routes/webhook.py` y detiene el
procesamiento normal cuando un comando es reconocido.

## Ubicación de la base de datos

La aplicación almacena los datos en un servidor MySQL. Los antiguos archivos de SQLite (`database.db` y `chat_support.db`) se crean en la raíz del proyecto y están excluidos del repositorio.

Si se utilizan para pruebas locales, realiza copias de seguridad en un almacenamiento externo y evita versionarlos.

### Respaldos automáticos de bases de datos

* Define la carpeta donde se guardarán las copias en el `.env` usando la variable `BACKUP_ROOT` (por ejemplo: `\\Svrkiryapp\001 agestion\2025 BACK UP\AGESTION\LEADS`). Si no se define, se usará por defecto la carpeta padre del proyecto.
* El script `scripts/backup_databases.py` genera un volcado independiente por cada base (control y tenants) en una ruta con jerarquía `<BACKUP_ROOT>/<db_name>/<AAAA-MM-DD>/<db_name>_YYYYMMDD_HHMMSS.sql`.
* El despliegue en Linux y Windows ejecuta automáticamente un respaldo antes de actualizar (scripts `deploy/linux/deploy.sh` y `deploy/windows/start_whatsapp_api.ps1`).
* En Windows tienes dos modos de despliegue:
  * `deploy/windows/start_whatsapp_api.ps1`: despliegue completo limpiando contenedores, imágenes y volúmenes del stack.
  * `deploy/windows/start_whatsapp_api_fast.ps1`: despliegue rápido sin limpieza total de Docker (ideal para actualizaciones frecuentes).
* Para un respaldo manual ejecuta: `python scripts/backup_databases.py --env-file .env`.
* Para programar una copia diaria a la medianoche agrega una entrada cron similar a: `0 0 * * * cd /opt/whapco && /usr/bin/python3 scripts/backup_databases.py --env-file /opt/whapco/.env --tag cron >> /var/log/whapco-backup.log 2>&1`.

### Arquitectura multiempresa

La aplicación funciona como una sola instancia multi-tenant. El esquema principal definido por `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` y `DB_NAME` se usa como registro central de empresas (tabla `tenants`). Cada fila describe la base de datos exclusiva de una empresa (host, usuario y nombre de base dedicados), garantizando que los datos de cada tenant estén completamente aislados a nivel de esquema.

#### Base de control vs. bases de cada empresa

* El `.env` solo define la **base de control** del sistema (la indicada en `DB_NAME`). Esta base se usa para autenticar al usuario, identificar a qué empresa pertenece y obtener la configuración necesaria para operar.
* Cada empresa tiene una **base de datos propia**, creada de forma dinámica desde la interfaz web cuando se registra el tenant. El nombre de esa base no está en el `.env`, queda asociado de manera permanente al tenant y no se reutiliza para otras empresas.
* Desde el momento en que se crea el tenant, todos sus usuarios, clientes, chats, mensajes y reglas se guardan únicamente en su base dedicada. No hay mezcla de datos entre empresas.
* La base configurada en `.env` actúa solo como punto de entrada y ruteo: guarda el catálogo de tenants y permite dirigir cada petición a la base que corresponda.

* Cada petición HTTP debe indicar a qué empresa pertenece usando el encabezado definido en `TENANT_HEADER` (por defecto `X-Tenant-ID`) o el parámetro de query `tenant`. Si no se indica y existe `DEFAULT_TENANT`, se usará dicho valor.
* Durante el arranque se asegura la existencia de la tabla `tenants` en el registro central y se registra la empresa por defecto (`DEFAULT_TENANT` y `DEFAULT_TENANT_NAME`) apuntando a la base configurada por las variables `DB_*`.
* La inicialización automática (`INIT_DB_ON_START=1`) crea el esquema completo solo en la base de datos de la empresa por defecto. Para nuevos tenants debes registrar su fila en `tenants` y ejecutar el inicializador (`services.tenants.ensure_tenant_schema`) apuntando a su configuración para poblar las tablas aisladas.
* Compatibilidad hacia atrás: si no defines `DEFAULT_TENANT` y no envías encabezado de tenant, la app sigue funcionando en modo single-tenant exactamente con la base configurada en `DB_*`; no se migran datos a otro lugar ni se pierde la información existente. La tabla `tenants` se crea en tu base actual, pero ningún request la utilizará hasta que definas un tenant.

#### Cómo crear nuevas empresas (tenants)

El registro de empresas se hace siempre en la base de datos central definida por `DB_*`, en la tabla `tenants`. Cada fila apunta a una base exclusiva para esa empresa. Puedes crear tenants de dos formas:

1) **CLI de administración (recomendado)**

```bash
python scripts/create_tenant.py <tenant_key> <db_name> \
  --name "Nombre Comercial" \
  --db-host 127.0.0.1 --db-port 3306 \
  --db-user root --db-password secret \
  --metadata '{"branding": "acme", "plan": "pro"}' \
  --init-schema
```

* `<tenant_key>` es el identificador que enviarán los clientes en el header `X-Tenant-ID`.
* `--init-schema` crea inmediatamente todas las tablas en la base aislada del tenant usando el mismo schema que la empresa por defecto. Omite el flag si prefieres manejar la migración manualmente.

2) **Panel de administración principal (solo super admin)**

La aplicación incluye un panel protegido en `/admin/tenants` visible únicamente para usuarios con el rol `superadmin` (el usuario `admin` lo recibe por defecto). Desde allí puedes:

* Listar todas las empresas registradas en la tabla `tenants` del registro central.
* Crear o actualizar una empresa indicando `tenant_key`, host, usuario y contraseña de su base de datos. Marca la casilla de “Crear/actualizar esquema aislado” si quieres que se generen todas las tablas en la base nueva.
* Gestionar usuarios de cada empresa: crea o actualiza un usuario y marca los roles que debe tener en su base aislada. El formulario sincroniza automáticamente los roles asignados.

Con cualquiera de los métodos, una vez creada la empresa debes enviar el `tenant_key` en cada petición HTTP del panel y en cada webhook de WhatsApp para aislar los datos automáticamente.

### Usuario administrador por defecto

Durante la inicialización de la base de datos (`init_db`) se crean automáticamente los usuarios `admin` y `superadmin` con el hash definido en la variable de entorno `DEFAULT_ADMIN_PASSWORD_HASH`. Si no estableces un valor propio, se utilizará el hash correspondiente a la contraseña `Admin1234` (`scrypt:32768:8:1$JAUhBgIzT6IIoM5Y$6c5c9870fb039e600a045345fbe67029001173247f3143ef19b94cddd919996a7a82742083aeeb6927591fa2a0d0eb6bb3c4e3501a1964d53f39157d31f81bd4`). Ambos reciben el rol `superadmin` (y `admin` en el caso del usuario `admin`) para que puedas acceder al panel central con cualquiera de los dos.

Cuando necesites otro password inicial, genera su hash con `werkzeug.security.generate_password_hash`, asígnalo a `DEFAULT_ADMIN_PASSWORD_HASH` y reinicia el servicio para que `init_db` lo inserte si el usuario no existe todavía.

### Inicialización automática del esquema

La aplicación ejecuta `init_db()` por defecto durante el arranque para crear la base de datos (si no existe) y asegurarse de que todas las tablas, índices y datos semilla estén listos antes de aceptar peticiones. Si prefieres administrar las migraciones manualmente, establece `INIT_DB_ON_START=0` antes de iniciar Flask para desactivar este comportamiento.

## Almacenamiento de medios subidos por el usuario

Todos los archivos de entrada y salida (imágenes, audios, videos y documentos) se guardan siempre en `static/uploads` dentro del proyecto. La ruta se crea automáticamente al arrancar Flask y no puede sobrescribirse mediante variables de entorno para evitar que los ficheros desaparezcan al recrear el contenedor.

Para que las cargas sobrevivan a los reinicios de Docker, monta `static/uploads` como volumen persistente. Un ejemplo mínimo para Linux sería:

```yaml
services:
  whatsapp_api:
    build: .
    volumes:
      - ./static/uploads:/app/static/uploads
```

Los archivos no deben versionarse en Git; usa siempre un volumen o carpeta externa para evitar su borrado accidental.
