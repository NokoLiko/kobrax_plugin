# Pruebas en hardware

Estado de cada función en la Kobra X real (fw 2.0.2.2). Actualizar al probar.

Leyenda: ✅ funciona · ❌ falla · ⏳ pendiente · ⚠️ funciona con observaciones

## Conexión y estado

| Función | Estado | Notas |
|---|---|---|
| Conexión (handshake + MQTT) | ✅ | 2026-11-15, dentro de Orca |
| Estado, temperaturas, progreso | ✅ | |
| Versión del firmware en Device | ✅ | desde v0.2.1 |
| Permisos: sin preguntas repetidas | ⏳ | v0.2.2: archivos por ruta en la carpeta del plugin |
| Primer envío sin fallar | ⏳ | v0.4.1: permiso de lectura de `orcaslicer_model/*` |
| Coincidencia con el perfil (`nozzle_diameter`) | ⏳ | v0.5.0: debería desaparecer `update printer preset name failed` del log |

## Impresión

| Función | Estado | Notas |
|---|---|---|
| Subir e imprimir (1 color) | ✅ | 2026-11-15: Snoopy.gcode, `print/start` aceptado |
| Solo subir (sin imprimir) | ⏳ | |
| Casillas del diálogo de envío (nivelado, flujo, timelapse) | ⏳ | v0.4.0. El timelapse con `status: 1` nunca se probó |
| Impresión multicolor (mapeo de slots) | ⏳ | |
| Pausar / reanudar / detener | ⏳ | |
| Estado FINISH al terminar | ⏳ | |
| Sub-etapa (calentando, nivelando) | ⏳ | estados reales de la Kobra X sin confirmar |
| Avisos de error | ⏳ | difícil de provocar a propósito |

## Controles de la pestaña Device

| Función | Estado | Notas |
|---|---|---|
| Home | ✅ | |
| Luz | ✅ | |
| Mover X | ✅ | |
| Mover Y | ✅ | Y+ lleva la cama hacia adelante (normal) |
| Mover Z (botones "Bed") | ⏳ | v0.3.2 invirtió el signo: la flecha de arriba debería subir el cabezal |
| Modo de velocidad | ⏳ | v0.5.0: Anycubic 1 = estándar, 2 = silencioso, 3 = sport. Comparar con la pantalla |
| Ventilador de pieza | ⏳ | `fan/setSpeed` |
| Ventiladores auxiliar y de caja | ⏳ | `print/update` |
| Temperaturas (boquilla, cama) | ⏳ | |
| Cámara | ⏳ | HTTP-FLV en :18088; depende del GStreamer del flatpak |
| Editar material y color de un slot | ⏳ | `multiColorBox/setInfo` |
| Cargar / descargar filamento | ⏳ | `feedFilament`; calienta la boquilla, hacerlo al lado |
| Apagar motores (M84) | ⏳ | |
| Sincronizar filamentos con el perfil | ⏳ | ids de "Generic X @System" |

## Página del plugin (v0.6.0)

| Función | Estado | Notas |
|---|---|---|
| Pestaña "Kobra X" se ve y se actualiza | ⏳ | |
| "Device (Web)" con `print_host_webui = http://127.0.0.1:18930/` | ⏳ | |
| Miniatura y parámetros del trabajo | ⏳ | datos confirmados en vivo (Snoopy); falta verlos en pantalla |
| Lista de archivos | ❌ | `file/listLocal` responde 10112 con todas las variantes probadas; resolver con la captura del Slicer Next |
| Offset de Z | ⏳ | sin implementar hasta confirmar el formato (riesgo de chocar la boquilla) |

## Distribución

| Función | Estado | Notas |
|---|---|---|
| Instalar desde Orca Cloud (Browse plugins) | ✅ | 2026-11-15, v0.6.0; el nombre `kobrax_plugin_any.whl` funciona |
| Publicación automática al crear un release | ✅ | 2026-11-16, v0.6.1: wheel adjunto al release y publicado en Orca Cloud (201) |
| Una sola pregunta de permiso por la IP | ⏳ | v0.6.1 |
| Sin pregunta "FsReadWrite:open" al conectar (netrc/.pyc) | ⏳ | v0.6.2 |

## Fuera de alcance por ahora

- ACE Pro (secado, alimentación automática, humedad): no hay ACE para probar.
