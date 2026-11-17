<img src="assets/icon.png" alt="" width="96" align="right">

# kobrax_plugin

Plugin de **OrcaSlicer** para usar una **Anycubic Kobra X** por la red local (modo LAN), sin la
nube de Anycubic y sin Anycubic Slicer Next.

Con el plugin podés enviar e imprimir desde Orca, ver el estado de la impresora en la pestaña
**Device** y manejarla desde una página propia.

> Proyecto comunitario, sin relación con Anycubic ni con OrcaSlicer. El protocolo de la
> impresora no es público: sale de ingeniería inversa. Probado en una Kobra X con firmware
> **2.0.2.2**, con OrcaSlicer **2.5.1** (flatpak, Linux). Qué está probado y qué no:
> [TESTING.md](TESTING.md).

## Qué hace

**Imprimir**
- Subir un archivo, o subirlo e imprimirlo, desde el botón de enviar de Orca.
- Asignar los colores del proyecto a los 4 slots de filamento de la impresora.
- Casillas de nivelado automático, calibración de flujo y timelapse en el diálogo de envío.
- Pausar, reanudar y detener.

**Pestaña Device de Orca**
- Estado, progreso, capa, tiempo restante y etapa (calentando, nivelando…).
- Temperaturas de boquilla y cama, con control.
- Ventiladores de pieza, auxiliar y de caja, con control.
- Modo de velocidad, luz, home y movimiento de X/Y/Z.
- Cámara.
- Slots de filamento: ver, sincronizar con el proyecto, editar material y color, cargar y
  descargar.
- Avisos con los errores de la impresora.

**Página "Kobra X"**, con el trabajo en curso (miniatura, progreso, tiempos), los parámetros
con los que se laminó, ventiladores, velocidad, luz, slots y archivos de la impresora. Ver
[Página de la Kobra X](#página-de-la-kobra-x).

**Lo que no hace:** extruir a mano, nivelar a pedido desde Orca, ni manejar el ACE Pro.
La Kobra X no tiene un comando conocido para los dos primeros, y el ACE no está probado.

## Requisitos

- **OrcaSlicer 2.5 o posterior** con soporte de plugins de Python. Las versiones 2.2 y
  anteriores no lo tienen.
- **Kobra X con el modo LAN activado:** en la impresora, Settings → Network → **LAN Mode**.
  Mientras el modo LAN está activo, la impresora no usa la nube de Anycubic.
- La PC y la impresora en la **misma red**.
- Conviene darle a la impresora una **IP fija** en el router, porque el plugin se conecta por IP.

Las dependencias de Python (`cryptography`, `paho-mqtt`, `requests`) las instala Orca solo al
instalar el plugin, así que necesita internet la primera vez.

## Instalación

### 1. Conseguir el plugin

**Desde Orca Cloud (lo más fácil):** en el diálogo de Plugins de Orca, **Browse plugins**,
buscá *Kobra X* y suscribite. Necesitás una cuenta de Orca Cloud con la sesión iniciada en Orca.
Orca lo instala y te avisa cuando hay una versión nueva. Si lo instalás así, seguí desde el
[paso 3](#3-activar-los-agentes-de-impresora).

**A mano:** descargá el archivo `kobrax_plugin-<versión>-py3-none-any.whl` de la página de *Releases* del
repositorio, o generalo vos (necesitás [uv](https://docs.astral.sh/uv/)):

```sh
git clone <url-del-repo>
cd kobrax_plugin
uv build            # deja el .whl en dist/
```

### 2. Instalarlo en Orca

1. Abrí el diálogo de **Plugins** y elegí **Install local plugin**.
2. Elegí el archivo `.whl`.
3. Orca muestra un diálogo de permisos. **Aceptalo.** El plugin pide:
   - crear hilos en segundo plano (la conexión con la impresora corre aparte de la interfaz);
   - leer la carpeta temporal donde Orca guarda el proyecto que envía
     (`<temp>/orcaslicer_<usuario>/orcaslicer_model`);
   - abrir sockets locales, incluido el servidor de la página en `127.0.0.1:18930`.

   Si lo rechazás, el plugin no se carga.
4. Activá el plugin en la lista, si no quedó activo.

### 3. Activar los agentes de impresora

En **Preferences → Developer**, activá **Use printer agents instead of print hosts**. Sin esta
opción, Orca no usa el plugin.

### 4. Configurar el perfil de la impresora

En el perfil de la Kobra X (ajustes de la impresora):

1. **Basic information → Advanced → Printer Agent**: elegí **anykubic**.
2. Cargá la **IP de la impresora** como print host, por ejemplo `192.168.0.84`. La IP la ves en
   la pantalla de la Kobra X, en los ajustes de red.

   Con los agentes activados, Orca oculta el botón de conexión (el ícono de wifi) donde
   normalmente se carga la IP. Para cargarla:
   1. desactivá un momento **Use printer agents** (paso 3);
   2. tocá el ícono de wifi junto al perfil y escribí la IP en **Hostname, IP or URL**;
   3. si querés la página en "Device (Web)", escribí también `http://127.0.0.1:18930/` en
      **Device UI** (ver [Página de la Kobra X](#página-de-la-kobra-x));
   4. guardá y volvé a activar **Use printer agents**.
3. **Guardá el perfil.** Si no lo guardás, Orca sigue usando su agente propio y el plugin no
   se conecta.

### 5. Primera conexión

Elegí el perfil de la Kobra X. La primera vez, Orca pregunta si el plugin puede conectarse a la
IP de la impresora: **aceptá**. Esa respuesta queda guardada. En unos segundos, la pestaña
**Device** muestra la impresora con sus temperaturas.

Cada vez que reinstalás o actualizás el plugin, Orca borra los permisos guardados y vuelve a
preguntar.

## Página de la Kobra X

La misma página se puede ver en dos lugares:

- **Pestaña "Kobra X"** de la ventana principal. Está siempre, aunque tengas elegida otra
  impresora.
- **Pestaña "Device (Web)"**, que solo aparece con la Kobra X seleccionada. Requiere cargar
  `http://127.0.0.1:18930/` en el campo **Device UI** del perfil (paso 4).

Muestra el trabajo en curso (miniatura, estado, progreso, capa, tiempo transcurrido y
restante), los parámetros con los que se laminó (altura de capa, relleno, temperaturas,
filamento usado), los tres ventiladores con control, el modo de velocidad, la luz, los slots de
filamento y la lista de archivos de la impresora.

El servidor de la página escucha solo en `127.0.0.1`. Rechaza los pedidos que no traen el
encabezado `X-Kobrax` y los que llegan con otro `Host`, así que una página web abierta en tu
navegador no puede mandarle comandos a la impresora.

## Problemas comunes

| Síntoma | Causa y solución |
|---|---|
| No aparece el ícono de wifi | Es normal con **Use printer agents** activado. Ver el paso 4 |
| "Device (Web)" dice *Connection refused* | Orca intenta abrir una página web en la IP de la impresora, y la Kobra X no tiene. Cargá `http://127.0.0.1:18930/` en **Device UI** o usá la pestaña **Device** |
| La pestaña Device muestra cosas de Bambu Lab y no la impresora | Orca no está usando el plugin. Revisá que el perfil tenga **Printer Agent: anykubic** y que esté **guardado** |
| Pide permiso en cada arranque | Corregido en la v0.6.3: actualizá el plugin. Si antes habías aceptado "FsReadWrite:open", podés quitarlo en el diálogo de plugins porque ya no hace falta. Después de reinstalar el plugin, Orca vuelve a preguntar una vez |
| El primer envío de una sesión falla | Era por un permiso de lectura que se resolvió en la v0.4.1. Si te pasa, aceptá el permiso y volvé a enviar |
| Pide hacer home antes de cada movimiento | La impresora no informa si los ejes tienen home: el plugin lo recuerda después de un home hecho desde Orca. Al reconectar, Orca vuelve a pedirlo |
| "La impresora no confirmó el inicio" | El plugin manda el inicio **una sola vez** para no arrancar dos trabajos. Revisá la pantalla de la impresora antes de volver a enviar |
| No conecta | Revisá que el **LAN Mode** esté activo, que la IP sea la correcta y que la PC esté en la misma red |

**Logs:** los mensajes del plugin empiezan con `[kobrax]` y están en la carpeta `log/` de los
datos de Orca, en los archivos `python_*.log`:

| Sistema | Carpeta de datos de Orca |
|---|---|
| Linux (flatpak) | `~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer` |
| Linux | `~/.config/OrcaSlicer` |
| Windows | `%APPDATA%\OrcaSlicer` |
| macOS | `~/Library/Application Support/OrcaSlicer` |

## Límites conocidos

- Probado en una sola Kobra X (firmware 2.0.2.2), en Linux con OrcaSlicer en flatpak. En
  Windows y macOS no está probado.
- La impresión multicolor no está probada: solo se probó con un filamento.
- Varios controles salen del protocolo documentado por la comunidad y todavía no se probaron en
  el hardware: ventiladores auxiliar y de caja, editar slots, cargar y descargar filamento.
  Ver [TESTING.md](TESTING.md).
- La impresora no se encuentra sola en la red, porque la Kobra X no responde a SSDP. Hay que
  cargar la IP.
- La cámara depende de que el GStreamer de Orca reproduzca el stream HTTP-FLV de la impresora.
- La lista de archivos de la impresora todavía no funciona: el comando devuelve error.
- Los botones "Bed" de Orca mueven el **cabezal** en Z, porque en la Kobra X la cama no sube ni
  baja. La flecha de arriba sube el cabezal.

## Otras impresoras Anycubic

La Kobra 3, la Kobra 3 Max, la Kobra S1 y la S1 Max usan el mismo protocolo LAN que la Kobra X:
handshake por HTTP en el puerto 18910 y MQTT con TLS en el 9883. El plugin lee el modelo de la
impresora (`modelId`) y ajusta los movimientos según el modelo (`models.py`). **Ninguna de
estas impresoras se probó todavía**: lo que sigue es lo que se espera, no lo comprobado.

| Modelo | `modelId` | Z en Orca | Códigos de movimiento |
|---|---|---|---|
| Kobra X | 20030 | Mueve el cabezal (signo invertido) | X=1, Y=2, Z=3, home=5. **Probado** |
| Kobra 3 / 3 V2 | 20024 / 20027 | Mueve el cabezal (signo invertido) | Y=1, X=2, Z=3, home=4. Sin probar |
| Kobra 3 Max | 20026 | Mueve el cabezal (signo invertido) | Y=1, X=2, Z=3, home=4. Sin probar |
| Kobra S1 | 20025 | Mueve la cama (como espera Orca) | Y=1, X=2, Z=3, home=4. Sin probar |
| Kobra S1 Max | 20029 | Mueve la cama (como espera Orca) | Y=1, X=2, Z=3, home=4. Sin probar |

Los `modelId` salen de [Rinkhals](https://github.com/jbatonnet/Rinkhals). Los códigos de home de
la Kobra 3 y la S1 salen de
[ianloic/anycubic-orca-plugin](https://github.com/ianloic/anycubic-orca-plugin). Que X e Y usen la
misma numeración al moverse es una suposición.

**Qué tiene más chances de andar:** conectar, ver el estado y las temperaturas, y subir e imprimir
un archivo.

**Qué puede fallar:**
- **Home y movimiento de ejes:** con códigos equivocados se mueve otro eje. Probalo primero con
  distancias chicas y la mano cerca del botón de apagado.
- **Filamento:** el plugin espera la caja de 4 slots de la Kobra X (`multiColorBox`). Con un
  ACE Pro de la Kobra 3 o la S1, o sin caja, la asignación de colores y los slots pueden no
  aparecer.
- **Ventiladores, luz y modo de velocidad:** se usan los de la Kobra X (pieza, auxiliar y caja;
  luz tipo 3).
- **Inicio de impresión:** el formato de `print/start` está validado solo en la Kobra X.

Si tenés una de estas impresoras y querés ayudar, seguí [CAPTURE.md](CAPTURE.md) y mandanos la
captura en un issue. Con eso se pueden corregir los códigos de ese modelo.

## Seguridad

- El modo LAN de la Kobra X entrega las credenciales MQTT sin autenticación a cualquiera de la
  red local. Usalo solo en una red de confianza.
- El plugin guarda el certificado de cliente solo mientras arma la conexión, en su carpeta de
  datos de Orca, y lo borra enseguida.

## Para desarrollar

```sh
uv sync
uv run pytest
uv build
```

Estructura:

| Archivo | Qué contiene |
|---|---|
| `src/kobrax_plugin/main.py` | El agente de impresora (lo que llama Orca) y el registro del plugin |
| `src/kobrax_plugin/lan.py` | Protocolo: handshake, MQTT con TLS y subida de archivos |
| `src/kobrax_plugin/status.py` | Traducción de los reportes de la Kobra X al formato que espera Orca |
| `src/kobrax_plugin/job.py` | Preparación del trabajo y armado de `print/start` |
| `src/kobrax_plugin/page.py` | Página de la Kobra X y su servidor local |
| `src/kobrax_plugin/errors.py` | Códigos de error de Anycubic |
| `src/kobrax_plugin/models.py` | Diferencias por modelo: numeración de ejes, home y dirección de Z |
| `tools/sniff.py` | Captura del tráfico MQTT, para descubrir comandos |

### Descubrir comandos nuevos

El broker MQTT de la Kobra X deja escuchar todos los topics, incluidos los comandos que mandan
otros clientes. `tools/sniff.py` solo escucha, nunca publica, y oculta credenciales, tokens y
el id del equipo:

```sh
uv run python tools/sniff.py <ip-de-la-impresora> -o captura.jsonl
```

Dejalo corriendo mientras usás Anycubic Slicer Next en modo LAN, o la pantalla de la impresora.
Cada tipo de mensaje nuevo se muestra completo, y los repetidos solo se cuentan.
`--commands-only` muestra únicamente los comandos.

**¿Querés ayudar a completar el plugin?** Seguí [CAPTURE.md](CAPTURE.md): es una lista de acciones para
hacer en Anycubic Slicer Next mientras corre `sniff.py`, con los pasos para mandarnos la captura y sumarla al plugin.

### Publicar una versión

1. Subí la versión en `pyproject.toml` y en `PLUGIN_VERSION` (`src/kobrax_plugin/main.py`).
2. Creá un release en GitHub con el tag `v<versión>` (por ejemplo `v0.6.0`). En el cuerpo del
   release poné los cambios: se usan como changelog en Orca Cloud.
3. El workflow `.github/workflows/publish-orcacloud.yml` verifica que el tag coincida con la
   versión, arma el wheel, lo adjunta al release y lo publica en Orca Cloud.

Antes del primer release automático hay que crear el plugin a mano en
[Orca Cloud](https://cloud.orcaslicer.com) (Plugins → Shared Plugins → **+**) y conectar el
repositorio en **Edit plugin → GitHub publishing**.

## Créditos

- Los formatos de los comandos de slots, carga y descarga, ventiladores auxiliares y modo de
  velocidad se tomaron como **datos del protocolo** documentados en
  [ianloic/anycubic-orca-plugin](https://github.com/ianloic/anycubic-orca-plugin). El código es
  propio y no copia el de ese proyecto, que es AGPL.
- La tabla de códigos de error (`errors.py`) viene de
  [stribor/anycubic_kobrax](https://github.com/stribor/anycubic_kobrax), con licencia MIT. El
  aviso de licencia original está en el archivo.
- La investigación del protocolo LAN de la comunidad: stribor/anycubic_kobrax,
  jaimevictor/kobra-x-local-slicer-haas, eduardobittencourt/kobra-lan-cli y rvanderp3/kobra-connect.

## Licencia

MIT. Ver [LICENSE](LICENSE).
