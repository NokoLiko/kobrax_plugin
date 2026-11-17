# Capturar comandos con Anycubic Slicer Next

Guía para grabar los comandos que Anycubic Slicer Next le manda a la impresora en modo LAN, y
compartirlos para sumarlos al plugin. Sirve para la Kobra X y para otros modelos con el mismo
protocolo (Kobra 3, Kobra 3 Max, Kobra S1, S1 Max). Ver
[Otras impresoras Anycubic](README.md#otras-impresoras-anycubic).

`tools/sniff.py` se conecta al broker MQTT de la impresora y **solo escucha**: nunca publica nada.
Antes de mostrar o guardar los mensajes, tapa credenciales, tokens, URLs con token y el id del
equipo.

## Qué hace falta

- La impresora en **LAN Mode**, sin ningún trabajo en curso.
- La PC en la misma red que la impresora.
- [uv](https://docs.astral.sh/uv/) y una copia de este repo:
  ```sh
  git clone https://github.com/NokoLiko/kobrax_plugin
  cd kobrax_plugin
  uv sync
  ```
- Anycubic Slicer Next con la impresora agregada por IP en modo LAN.
- **OrcaSlicer cerrado**, así en la captura solo aparecen los comandos de Slicer Next.

## Cómo grabar

En una terminal:

```sh
uv run python tools/sniff.py <ip-de-la-impresora> --commands-only -o captura.jsonl
```

Para cada acción de la lista:

1. Anotá el número de la acción y la hora.
2. Hacé **una sola acción** en Slicer Next.
3. Esperá unos segundos a que aparezca el comando en la terminal antes de pasar a la siguiente.

Si algo no existe en tu Slicer Next, salteá ese número y anotalo. Al terminar, cortá la captura
con `Ctrl+C`.

Sin `--commands-only` también se graban los reportes de la impresora, que sirven para ver qué
campos cambian después de cada acción. El archivo queda bastante más grande.

## Lista de acciones

Van de menos a más riesgo. Las fases 3 y 4 mueven o calientan la impresora: hacelas **al lado de
la impresora**, con la mano cerca del botón de apagado.

### Fase 1: solo lectura (no mueve ni calienta)

| # | Acción en Slicer Next | Qué buscamos |
|---|---|---|
| 1 | Conectar a la impresora y abrir la pestaña de dispositivo | Qué consultas manda al conectar |
| 2 | Abrir la lista de archivos de la impresora | El `file/listLocal` correcto (hoy responde 10112) |
| 3 | Cambiar de carpeta o de almacenamiento (interno/USB), si se puede | Parámetros de ruta |
| 4 | Ver la información de un archivo de la lista (miniatura, detalles) | Otro comando de archivos |
| 5 | Abrir la vista de los slots de filamento | Consulta de `multiColorBox` (o del ACE Pro) |
| 6 | Abrir la cámara | Si pide el stream con algún comando |
| 7 | Abrir la configuración o calibración de la impresora, sin ejecutar nada | Comandos de consulta extra |

### Fase 2: cambian ajustes, sin mover ejes ni calentar

| # | Acción | Qué buscamos |
|---|---|---|
| 8 | Prender y apagar la luz | Comando de luz |
| 9 | Pasar por cada modo de velocidad y anotar el nombre que muestra cada uno | Número → nombre (estándar, silencioso, sport) |
| 10 | Ventilador de pieza a 50 % y después a 0 | `fan/setSpeed` |
| 11 | Ventilador auxiliar a 50 % y después a 0 | Formato de `print/update` |
| 12 | Ventilador de caja a 50 % y después a 0 | Formato de `print/update` |
| 13 | Editar un slot: cambiar material y color, y después volverlo como estaba | `multiColorBox/setInfo` |

### Fase 3: mueven o calientan

| # | Acción | Qué buscamos |
|---|---|---|
| 14 | Home de todos los ejes | Código de home |
| 15 | Home de X solo, después de Y solo, si existen | Códigos por eje |
| 16 | Mover X +10 mm, Y +10 mm y Z +10 mm, uno por vez | Numeración de ejes en `axis/move` |
| 17 | Apagar motores, si el botón existe | Comando de M84 |
| 18 | Offset de Z: subirlo un paso (+0.05) y volverlo a como estaba | Formato del offset. Es el más delicado: en positivo la boquilla se aleja de la cama |
| 19 | Precalentar boquilla y cama, y después apagar | Comandos de temperatura |
| 20 | Cargar filamento en un slot | `feedFilament` (calienta la boquilla) |
| 21 | Descargar ese filamento | `feedFilament` en reversa |

### Fase 4: imprimir (una pieza chica, de 5 a 10 minutos)

| # | Acción | Qué buscamos |
|---|---|---|
| 22 | Enviar e imprimir con nivelado, calibración de flujo y timelapse **activados**, con 2 slots si se puede | `print/start` completo, multicolor y con opciones |
| 23 | Pausar | Comando de pausa |
| 24 | Reanudar | Comando de reanudar |
| 25 | Cambiar velocidad o ventilador durante la impresión | Si cambia el formato mientras imprime |
| 26 | Detener, o dejarla terminar | Comando de detener, o el estado de fin |
| 27 | Solo subir un archivo, sin imprimir | La opción de "solo subir" |

## Cómo compartirla

1. **Revisá el archivo antes de mandarlo.** `sniff.py` tapa lo sensible, pero mirá que no quede
   nada tuyo, como nombres de archivos o de la red.
2. Abrí un [issue](https://github.com/NokoLiko/kobrax_plugin/issues/new) con:
   - el modelo de la impresora y la versión de firmware (en la pantalla: Ajustes → Acerca de);
   - la versión de Anycubic Slicer Next;
   - `captura.jsonl` adjunto (si GitHub no acepta la extensión, renombralo a `captura.txt`);
   - tus notas: número de acción, hora, y qué pasó en la impresora o qué cosas no existían.

Con eso se implementan los comandos y se actualiza [TESTING.md](TESTING.md).
