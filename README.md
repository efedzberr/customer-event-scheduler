# Customer Event Scheduler — API

Backend para el flujo de agendado de juntas con clientes desde Salesforce.

## Endpoints

### `POST /generate-token`

Genera un JWT firmado para el link que se manda al cliente. Lo llama Make.

Headers:
- `Authorization: Bearer <MAKE_API_KEY>`

Body:
```json
{
  "clientId": "003XX0000004TmiXXX",
  "executiveId": "005XX0000001S2zXXX",
  "clientName": "María Rodríguez",
  "executiveName": "Carlos Pérez",
  "proposedStart": "2026-05-12T15:00:00-06:00"
}
```

Respuesta:
```json
{
  "token": "eyJhbGc...",
  "link": "https://your-bolt-app/agendar?token=eyJhbGc...",
  "jti": "550e8400-e29b-41d4-a716-446655440000"
}
```

### `GET /availability?token=<JWT>`

Devuelve los slots libres del ejecutivo para los próximos 7 días. Lo llama Bolt.

Respuesta:
```json
{
  "executive": { "id": "005XX...", "name": "Carlos Pérez" },
  "client":    { "id": "003XX...", "name": "María Rodríguez" },
  "proposedStart": "2026-05-12T15:00:00-06:00",
  "days": [
    {
      "date": "2026-05-06",
      "slots": [
        { "time": "08:30", "datetime": "2026-05-06T08:30:00-06:00" },
        { "time": "10:30", "datetime": "2026-05-06T10:30:00-06:00" }
      ]
    }
  ]
}
```

## Reglas de disponibilidad

- Slots de 1 hora entre 8:30 y 17:30 (timezone America/Mexico_City).
- Lunes a viernes solamente.
- De hoy a +7 días.
- Excluye horarios ya ocupados en el calendario del ejecutivo (objeto `Event` en Salesforce).

## Variables de entorno

Ver `.env.example`. Todas son requeridas excepto `APP_BASE_URL` (tiene default).

## Deploy a Railway

1. Sube este folder a un repositorio de GitHub.
2. En Railway: New Project → Deploy from GitHub repo → selecciona el repo.
3. Railway detecta Python automáticamente. Si no, el `Procfile` le indica cómo arrancar.
4. En Settings → Variables, agrega cada env var del `.env.example` con sus valores reales.
5. Para `SF_PRIVATE_KEY`, pega el contenido completo del archivo `.key` con los saltos de línea reales (Railway lo soporta).
6. Genera el dominio público: Settings → Networking → Generate Domain. Te queda algo como `https://customer-event-scheduler-production.up.railway.app`.

## Verificación rápida

Una vez desplegado, en tu terminal:

```bash
# Health check
curl https://your-railway-url.up.railway.app/

# Debe responder: {"status":"ok","service":"customer-event-scheduler"}
```

Para probar `/generate-token` (sustituye los valores):

```bash
curl -X POST https://your-railway-url.up.railway.app/generate-token \
  -H "Authorization: Bearer <MAKE_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "clientId": "003XX0000004TmiXXX",
    "executiveId": "005XX0000001S2zXXX",
    "clientName": "Test Client",
    "executiveName": "Test Executive",
    "proposedStart": "2026-05-12T15:00:00-06:00"
  }'
```

Para probar `/availability`, usa el token devuelto:

```bash
curl "https://your-railway-url.up.railway.app/availability?token=<TOKEN>"
```
