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

## Requisitos

Para ejecutar la aplicación necesitas tener instalado **ffmpeg** en el sistema.

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

### Usuario administrador por defecto

Durante la inicialización de la base de datos (`init_db`) se crea automáticamente el usuario `admin` con el hash definido en la variable de entorno `DEFAULT_ADMIN_PASSWORD_HASH`. Si no estableces un valor propio, se utilizará el hash correspondiente a la contraseña `Admin1234` (`scrypt:32768:8:1$JAUhBgIzT6IIoM5Y$6c5c9870fb039e600a045345fbe67029001173247f3143ef19b94cddd919996a7a82742083aeeb6927591fa2a0d0eb6bb3c4e3501a1964d53f39157d31f81bd4`).

Cuando necesites otro password inicial, genera su hash con `werkzeug.security.generate_password_hash`, asígnalo a `DEFAULT_ADMIN_PASSWORD_HASH` y reinicia el servicio para que `init_db` lo inserte si el usuario no existe todavía.

### Inicialización automática del esquema

Si defines la variable `INIT_DB_ON_START=1` antes de levantar Flask, la aplicación ejecutará `init_db()` durante el arranque. Ese proceso crea la base de datos (si aún no existe) y garantiza que todas las tablas, índices y datos semilla necesarios queden listos antes de aceptar peticiones. Esto evita los errores de “base o tabla inexistente” en despliegues nuevos.

## Almacenamiento de medios subidos por el usuario

Los archivos generados por los usuarios se guardan en la ruta indicada por la variable de entorno `MEDIA_ROOT`. Esta ruta debe apuntar a un volumen externo o a un directorio persistente fuera del repositorio. Si no se define, la aplicación usará `static/uploads` dentro del proyecto.

La aplicación crea automáticamente el directorio configurado en `MEDIA_ROOT` durante el arranque. Para despliegues con Docker asigna dicho directorio a un volumen persistente. Por ejemplo, en `docker-compose.windows.yml` se incluye un volumen llamado `media_uploads` que se monta en `C:\app\static\uploads` para que los archivos sobrevivan a recreaciones del contenedor.

Estos archivos no deben versionarse en Git; durante los despliegues, mantén `MEDIA_ROOT` en un volumen persistente o en un almacenamiento externo para evitar su borrado accidental.
