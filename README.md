# ReAbleton

**REAPER ⇄ Ableton Live project converter.** The command-line tool and Python package are called `rpp-to-als`.

Convert a REAPER project (`.rpp`) into an Ableton Live Set (`.als`), and back again. Main uses: moving a song between DAWs without rebuilding it from scratch, sharing a sketch with an Ableton friend, or finding out exactly which of your plugins won't survive the trip.

Neither DAW has to be running. The tool reads and writes the project files directly.

## How it works

Both formats are text underneath: a `.rpp` is plain text, and a `.als` is gzipped XML. rpp-to-als parses one and writes the other, building every Ableton track from Live's own default templates (read from your install) so Live accepts the result as one of its own.

In short: you give it a `.rpp`, it gives you an `.als`. Give it an `.als` and you get a `.rpp`.

## Two ways to use it

**GUI (recommended for most people):** run `rpp-to-als-gui`, pick a `.rpp` or an `.als`, and hit **Export**. The direction follows the file you picked, and the options relabel themselves to match. When it's done, **FX report...** shows what made it across and what didn't.

**CLI:** the same exports from the command line. Useful for batch jobs, or if you'd rather type than click.

Both need Python 3.10+ for now. A standalone Windows `.exe` can be built with `packaging/build.sh`, see below.

## What converts

### REAPER -> Ableton

- Tracks with name, volume and pan. Tracks that receive sends become Return Tracks, with the send levels. Folder tracks are flattened.
- MIDI items become Arrangement clips, with playrate, loop and source offset handled. The same clips are also placed in Session view.
- Audio items become clips, straight, looped or time-stretched (warped).
- Item pitch adjust, MIDI CC, and track volume/pan automation.
- Tempo, time signature, and project markers (they become Locators). `--scenes` also turns each marker region into a Session-view Scene.
- Instruments and effects:

| REAPER | Ableton |
|--------|---------|
| ReaSamplOmatic5000 (RS5k) drum kit | Drum Rack, one pad per RS5k |
| ReaSynth | A plain sine Simpler as a stand-in (ReaSynth's settings aren't carried over) |
| Surge XT | Surge XT with the same patch |
| JS effects | ReaJS (from ReaPlugs) running the same script |
| ReaDelay | ReaDelay (ReaPlugs VST) |
| Waves plugins (VST3) | The same Waves plugin with the same settings |

### Ableton -> REAPER

- Tracks with name, volume, pan and mute. Group Tracks become folders.
- Arrangement MIDI and audio clips, including warped and looped ones.
- Locators become markers. `--scenes` lays Session-view Scenes out after the Arrangement, one marker per Scene.
- Drum Racks become a chain of RS5k instances, one per pad.
- `--copy-media` copies all audio and drum samples into a `mediaFiles` folder next to the project.

### The FX report

After every export you get a list of every FX and device on every track, marked converted or NOT converted. The GUI shows it in a window with a **Copy as text** button. Treat it as a checklist for whatever you still have to redo by hand.

## Requirements

- Windows. macOS might work; nobody has checked.
- Python 3.10+ and the packages in `requirements.txt` (`pip install -r requirements.txt`), or `pip install .` to also get the `rpp-to-als` and `rpp-to-als-gui` commands.
- **Ableton Live 12, any edition, installed on the same machine.** The tool needs Live's own default track templates and doesn't ship copies of them. It finds the standard install location on its own; if yours lives elsewhere, set `ABLETON_CORE_LIBRARY` to your `Core Library` folder. It will tell you loudly if it can't find it.
- For converted plugins to load, the same plugins must be installed on the Ableton side: Surge XT, ReaPlugs (for ReaJS and ReaDelay), Waves. No plugin, no sound. That part is between you and your plugin folder.

To build the standalone `.exe`: `pip install .[build]`, then `bash packaging/build.sh`. You get `dist/rpp-to-als-cli.exe` and `dist/rpp-to-als-gui.exe`.

## CLI usage

```
rpp-to-als export-project song.rpp "Song Project/song.als" [options]
rpp-to-als export-rpp song.als song-reaper/song.rpp [options]
rpp-to-als export song.rpp drums kit.adg        # one RS5k track -> .adg Drum Rack preset
rpp-to-als list-tracks song.rpp
```

(Without installing, use `python -m rpp_to_als.cli` instead of `rpp-to-als`.)

`export-project` options (REAPER -> Ableton):

| Flag | Default | Description |
|------|---------|-------------|
| `--copy-samples` | off | Copy sample and audio files into the Live project's `Samples/Imported` folder |
| `--scenes` | off | Turn each marker region into a Session-view Scene |
| `--export-standalone-adg` | off | Also write each Drum Rack as its own `.adg` preset |
| `--test` | off | Write to the test folder instead of an output path you give |

`export-rpp` options (Ableton -> REAPER):

| Flag | Default | Description |
|------|---------|-------------|
| `--copy-media [FOLDER]` | off (`mediaFiles` when given) | Copy all audio and drum samples into a subfolder next to the `.rpp` |
| `--scenes` | off | Lay Session-view Scenes out after the Arrangement, one marker per Scene |
| `--force` | off | Overwrite an existing `.rpp` |

The test folder is `RPP_TO_ALS_TEST_DIR`, or `Documents\rpp-to-als test exports` if that isn't set. Tip: add that folder to Ableton's browser Places and new exports show up there live.

## Observations

- The GUI is fully accessible to screen readers (NVDA, JAWS). All controls have accessible names and are reachable with the keyboard. The rules it follows are in `ACCESSIBILITY.md`.
- Drum kits: only RS5k instances that each play a single note are converted. Multi-note zones are skipped, with a warning.
- Choke groups don't survive the trip back to REAPER, because RS5k doesn't have them. Your open hi-hat will ring out like it's 1985.
- Not converted yet: folder tracks (REAPER -> Ableton), send-level automation, per-plugin parameter automation, tempo changes (Ableton -> REAPER uses the starting tempo), and track colors.
- Without `--force`, `export-rpp` refuses to overwrite an existing `.rpp`, in case it's a real project you care about.
- Some tests need real project files that aren't in this repo, and skip when they're missing. Code comments that mention PLAN.md, AUTOMATION.md or CLAUDE.md point to design notes that aren't published.

## License

GPL-3.0, see `LICENSE`.

Some templates in `rpp_to_als/templates/` are derived from Ableton Live's own files: the drum pad, the sine Simpler, and the track skeletons. They're included only so Live can load what this tool writes. You need your own licensed copy of Ableton Live 12. Not affiliated with Ableton or Cockos.

Built with [Claude Code](https://claude.com/claude-code).

---

# ReAbleton (Español — América Latina)

**Convertidor de proyectos entre REAPER y Ableton Live.** La herramienta de línea de comandos y el paquete de Python se llaman `rpp-to-als`.

Convierte un proyecto de REAPER (`.rpp`) en un Live Set de Ableton (`.als`), y de vuelta. Usos principales: pasar una canción de un DAW a otro sin rehacerla desde cero, compartir un boceto con un amigo que usa Ableton, o descubrir exactamente cuáles de tus plugins no van a sobrevivir el viaje.

No hace falta tener abierto ninguno de los dos DAW. La herramienta lee y escribe los archivos de proyecto directamente.

## Cómo funciona

Los dos formatos son texto por dentro: un `.rpp` es texto plano, y un `.als` es XML comprimido con gzip. rpp-to-als lee uno y escribe el otro, y arma cada pista de Ableton a partir de las plantillas por defecto de Live (leídas de tu instalación), para que Live acepte el resultado como si fuera suyo.

En pocas palabras: le das un `.rpp`, te devuelve un `.als`. Le das un `.als` y te devuelve un `.rpp`.

## Dos formas de usarlo

**Interfaz gráfica (recomendada para la mayoría):** ejecuta `rpp-to-als-gui`, elige un `.rpp` o un `.als` y presiona **Export**. La dirección depende del archivo que elegiste, y las opciones cambian de nombre para coincidir. Al terminar, **FX report...** muestra qué pasó y qué no.

**Línea de comandos:** las mismas exportaciones desde la terminal. Útil para procesar por lotes, o si prefieres escribir antes que hacer clic.

Por ahora las dos formas necesitan Python 3.10+. Se puede generar un `.exe` independiente para Windows con `packaging/build.sh`; los detalles están más abajo.

## Qué se convierte

### REAPER -> Ableton

- Pistas con nombre, volumen y paneo. Las pistas que reciben envíos se convierten en Return Tracks, con los niveles de envío. Las carpetas se aplanan.
- Los ítems MIDI se convierten en clips del Arrangement, respetando playrate, loop y desplazamiento de inicio. Los mismos clips también se colocan en la vista Session.
- Los ítems de audio se convierten en clips: normales, en loop o estirados en el tiempo (warp).
- Ajuste de tono por ítem, MIDI CC y automatización de volumen/paneo de pista.
- Tempo, compás y marcadores del proyecto (se convierten en Locators). `--scenes` además convierte cada región entre marcadores en una Scene de la vista Session.
- Instrumentos y efectos:

| REAPER | Ableton |
|--------|---------|
| Kit de batería con ReaSamplOmatic5000 (RS5k) | Drum Rack, un pad por cada RS5k |
| ReaSynth | Un Simpler con una onda senoidal simple como reemplazo (no se copian los ajustes de ReaSynth) |
| Surge XT | Surge XT con el mismo patch |
| Efectos JS | ReaJS (de ReaPlugs) ejecutando el mismo script |
| ReaDelay | ReaDelay (VST de ReaPlugs) |
| Plugins de Waves (VST3) | El mismo plugin de Waves con los mismos ajustes |

### Ableton -> REAPER

- Pistas con nombre, volumen, paneo y mute. Las Group Tracks se convierten en carpetas.
- Clips MIDI y de audio del Arrangement, incluidos los que tienen warp o loop.
- Los Locators se convierten en marcadores. `--scenes` coloca las Scenes de la vista Session después del Arrangement, con un marcador por Scene.
- Los Drum Racks se convierten en una cadena de instancias de RS5k, una por pad.
- `--copy-media` copia todo el audio y las muestras de batería a una carpeta `mediaFiles` junto al proyecto.

### El reporte de FX

Después de cada exportación obtienes una lista de todos los FX y dispositivos de cada pista, marcados como convertidos o NO convertidos. La interfaz gráfica la muestra en una ventana con un botón **Copy as text**. Úsala como lista de pendientes para lo que todavía tengas que rehacer a mano.

## Requisitos

- Windows. Tal vez funcione en macOS; nadie lo ha probado.
- Python 3.10+ y los paquetes de `requirements.txt` (`pip install -r requirements.txt`), o `pip install .` para tener además los comandos `rpp-to-als` y `rpp-to-als-gui`.
- **Ableton Live 12, cualquier edición, instalado en la misma computadora.** La herramienta necesita las plantillas de pista por defecto de Live y no incluye copias propias. Encuentra sola la ubicación de instalación estándar; si la tuya está en otro lado, define `ABLETON_CORE_LIBRARY` con la ruta de tu carpeta `Core Library`. Si no la encuentra, te lo va a decir claramente.
- Para que los plugins convertidos carguen, esos mismos plugins tienen que estar instalados del lado de Ableton: Surge XT, ReaPlugs (para ReaJS y ReaDelay), Waves. Sin plugin no hay sonido. Eso queda entre tú y tu carpeta de plugins.

Para generar el `.exe` independiente: `pip install .[build]` y luego `bash packaging/build.sh`. Obtienes `dist/rpp-to-als-cli.exe` y `dist/rpp-to-als-gui.exe`.

## Uso desde línea de comandos

```
rpp-to-als export-project song.rpp "Song Project/song.als" [opciones]
rpp-to-als export-rpp song.als song-reaper/song.rpp [opciones]
rpp-to-als export song.rpp drums kit.adg        # una pista de RS5k -> preset .adg de Drum Rack
rpp-to-als list-tracks song.rpp
```

(Sin instalar, usa `python -m rpp_to_als.cli` en lugar de `rpp-to-als`.)

Opciones de `export-project` (REAPER -> Ableton):

| Parámetro | Por defecto | Descripción |
|-----------|-------------|-------------|
| `--copy-samples` | desactivado | Copia las muestras y el audio a la carpeta `Samples/Imported` del proyecto de Live |
| `--scenes` | desactivado | Convierte cada región entre marcadores en una Scene de la vista Session |
| `--export-standalone-adg` | desactivado | Además guarda cada Drum Rack como su propio preset `.adg` |
| `--test` | desactivado | Escribe en la carpeta de prueba en vez de una ruta de salida que indiques |

Opciones de `export-rpp` (Ableton -> REAPER):

| Parámetro | Por defecto | Descripción |
|-----------|-------------|-------------|
| `--copy-media [CARPETA]` | desactivado (`mediaFiles` si se indica) | Copia todo el audio y las muestras de batería a una subcarpeta junto al `.rpp` |
| `--scenes` | desactivado | Coloca las Scenes de la vista Session después del Arrangement, un marcador por Scene |
| `--force` | desactivado | Sobrescribe un `.rpp` existente |

La carpeta de prueba es `RPP_TO_ALS_TEST_DIR`, o `Documentos\rpp-to-als test exports` si no está definida. Consejo: agrega esa carpeta a los Places del navegador de Ableton y las exportaciones nuevas aparecen ahí al instante.

## Observaciones

- La interfaz gráfica es completamente accesible para lectores de pantalla (NVDA, JAWS). Todos los controles tienen nombres accesibles y se pueden alcanzar con el teclado. Las reglas que sigue están en `ACCESSIBILITY.md`.
- Kits de batería: solo se convierten las instancias de RS5k que tocan una sola nota. Las zonas de varias notas se omiten, con una advertencia.
- Los grupos de choke no sobreviven el viaje de vuelta a REAPER, porque RS5k no los tiene. Tu hi-hat abierto va a sonar hasta el final, como si fuera 1985.
- Todavía no se convierten: carpetas (REAPER -> Ableton), automatización de nivel de envío, automatización de parámetros de plugins, cambios de tempo (Ableton -> REAPER usa el tempo inicial) ni colores de pista.
- Sin `--force`, `export-rpp` se niega a sobrescribir un `.rpp` existente, por si es un proyecto real que te importa.
- Algunas pruebas necesitan proyectos reales que no están en este repositorio, y se omiten si no los encuentran. Los comentarios del código que mencionan PLAN.md, AUTOMATION.md o CLAUDE.md apuntan a notas de diseño que no están publicadas.

## Licencia

GPL-3.0, ver `LICENSE`.

Algunas plantillas en `rpp_to_als/templates/` se derivan de archivos del propio Ableton Live: el pad de batería, el Simpler senoidal y los esqueletos de pista. Se incluyen solo para que Live pueda cargar lo que esta herramienta escribe. Necesitas tu propia copia con licencia de Ableton Live 12. Sin afiliación con Ableton ni con Cockos.

Hecho con [Claude Code](https://claude.com/claude-code).
