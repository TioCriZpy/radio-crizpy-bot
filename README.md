# 📻 Radio CriZpy Bot — `web_controller`

Cog personalizado para **Red-DiscordBot** que expone una API REST y WebSocket en tiempo real (FastAPI + Uvicorn embebidos en el event loop de `asyncio`) para controlar el motor de audio **Lavalink** desde el reproductor web **[CriZpy Media Player](https://github.com/TioCriZpy/crizpy-media-player)** con sincronización bidireccional en Discord.

---

## ✨ Características Principales

- **API REST + WebSocket en Vivo:**
  - Progreso de canción cada segundo (`progress`), cambios de pista (`track_change`), actualizaciones de cola (`queue_update`), historial (`history_update`), oyentes en voz (`listeners_update`), registro de actividad (`activity_update`) y notificaciones tipo Toast (`toast`).
- **Sincronización Bidireccional Discord ↔ Web:**
  - Embed enriquecido `🎶 Sonando` con botones interactivos (`⏸️ Pausar`/`▶️ Reanudar`, `⏮️ Reiniciar`, `⏭️ Saltar`, `⏹️ Detener`, `🔀 Aleatorio`, `📋 Ver Cola` efímero).
  - Limpieza automática de botones (`view=None`) al detener la música, terminar la cola o desconectar al bot.
  - Avisos temporales (`delete_after=15.0`) con indicador de origen (`🌐` Web / `💬` Discord) y debounce de 2s en volumen/seek.
- **Playlists Rápidas e Importación de YouTube:**
  - Carga instantánea del primer track (`<1s`) y encolado paralelo en segundo plano (`asyncio.Semaphore(6)`).
  - Importación directa de playlists/mixes de YouTube y guardado de la cola actual como playlist personal.
  - Caché de búsqueda en memoria (TTL 10 min) consultando Lavalink directamente sin forzar conexión al canal de voz.
- **Registro de Actividad (48 horas):**
  - Auditoría de movimientos por servidor (`web`, `discord`, `bot`) consultable vía `GET /api/activity`.
- **Autenticación y Permisos:**
  - Header `x-api-key` + sesiones OAuth2 de Discord (`x-session-token`), soporte multi-servidor automático según el canal de voz donde esté el usuario y roles configurables de DJ / Prioridad.

---

## 🧱 Arquitectura

```text
Discord Voice / Embeds  <──▶  Red-DiscordBot (Audio + Lavalink)
                                      │
                               web_controller Cog
                            (FastAPI + WebSocket :8710)
                                      │
                                      ▼
                     CriZpy Media Player (React + Vite)
```

---

## 📦 Instalación en Red-DiscordBot

### Opción 1: Vía Downloader de RedBot
```text
!repo add radio-crizpy https://github.com/TioCriZpy/radio-crizpy-bot
!cog install radio-crizpy web_controller
!load web_controller
```

### Opción 2: Instalación Manual
1. Instala las dependencias en el entorno virtual de tu RedBot *(respetando `<2` en Pydantic por compatibilidad con `typing-extensions` de RedBot)*:
   ```bash
   pip install -r requirements.txt
   ```
2. Copia la carpeta `web_controller/` dentro de tu directorio de cogs personalizados de RedBot.
3. Carga el cog en Discord:
   ```text
   !load web_controller
   ```

---

## ⚙️ Configuración Inicial (Comandos)

Una vez cargado el cog, usa comandos de dueño (`!wc`) y slash commands:

- `!wc` — Muestra el estado del controlador, puerto, clientes WS conectados y configuración actual.
- `!wc setkey <nueva_api_key>` — Define tu API Key secreta *(cámbiala obligatoriamente antes de exponer el puerto)*.
- `!wc setchannel #canal` — Define el canal de texto donde el bot enviará el embed `🎶 Sonando` y avisos.
- `!wc setport <puerto>` — Cambia el puerto de FastAPI (por defecto `8710`).
- `!wc oauth <client_id> <client_secret> <redirect_uri>` — Configura Discord OAuth2 para el login web.
- `/rol <@rol>` — Configura un rol DJ obligatorio para controlar desde la web.
- `/prioridad <@rol>` — Configura el rol con prioridad de reproducción.
- `/prefix <prefijo>` — Cambia el prefijo del bot en el servidor.

---

## 🔌 Endpoints Principales

Todos los endpoints requieren header `x-api-key` (y `x-session-token` para acciones de usuario):

| Método | Ruta | Descripción |
| :--- | :--- | :--- |
| `GET` | `/api/status`, `/api/now`, `/api/queue` | Estado del bot, canción actual y cola completa |
| `GET` | `/api/search?q=` | Búsqueda en YouTube / detección de playlists |
| `GET` | `/api/history`, `/api/activity`, `/api/listeners` | Historial (7 días), registro (48h) y oyentes en voz |
| `GET` | `/api/playlists`, `/api/playlists/{name}` | Playlists del usuario autenticado |
| `POST` | `/api/play`, `/api/pause`, `/api/skip`, `/api/prev`, `/api/stop` | Controles de reproducción y cola |
| `POST` | `/api/volume`, `/api/seek`, `/api/repeat`, `/api/shuffle` | Volumen (0–150), seek y modos de cola |
| `POST` | `/api/playlists/*`, `DELETE /api/playlists/{name}` | CRUD de playlists, `load`, `save_queue` e `import_yt` |
| `WS` | `/ws?api_key=...&session_token=...` | Eventos en tiempo real |

---

## 📄 Licencia

Distribuido bajo licencia **MIT**. Consulta [LICENSE](LICENSE) para más información.
