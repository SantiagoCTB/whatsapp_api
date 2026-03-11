# SSL / Let's Encrypt (ERR_CERT_DATE_INVALID)

Si en el navegador aparece `net::ERR_CERT_DATE_INVALID` para `app.whapco.site`, normalmente significa que el certificado presentado por Nginx está vencido (o no corresponde al dominio).

## 1) Verificar qué certificado estás sirviendo

```bash
openssl s_client -connect app.whapco.site:443 -servername app.whapco.site </dev/null 2>/dev/null | openssl x509 -noout -dates -issuer -subject
```

Debes revisar especialmente `notAfter`.

## 2) Confirmar DNS

Asegura que `app.whapco.site` y `whapco.site` apuntan al mismo servidor donde corre este `docker compose`.

## 3) Renovar certificado con webroot (recomendado)

> Requiere que exista `/var/www/certbot` en el host y que Nginx esté levantado.

```bash
mkdir -p /var/www/certbot
sudo certbot certonly --webroot -w /var/www/certbot \
  -d whapco.site -d app.whapco.site \
  --email admin@whapco.site --agree-tos --no-eff-email
```

Con esta configuración, Nginx ya sirve `/.well-known/acme-challenge/` desde `/var/www/certbot` por HTTP.

## 4) Recargar Nginx

Después de renovar/emitar:

```bash
docker compose -f deploy/linux/docker-compose.yml exec nginx nginx -s reload
```

## 5) Verificar de nuevo

```bash
openssl s_client -connect app.whapco.site:443 -servername app.whapco.site </dev/null 2>/dev/null | openssl x509 -noout -dates
```

---

## Nota sobre warnings de Docker Compose

Si ves:

- `The "DB_ROOT_PASSWORD" variable is not set...`

es porque ejecutas compose desde `deploy/linux/` y la interpolación de `${VAR}` no está leyendo automáticamente el `.env` raíz.

Opciones:

1. Exportar variables antes de ejecutar compose (`set -a; source /opt/whapco/.env; set +a`).
2. Usar `docker compose --env-file /opt/whapco/.env ...`.

Además, en este repo los `DB_*` de MySQL ahora usan fallback vacío para evitar esos warnings cuando no están exportadas.
