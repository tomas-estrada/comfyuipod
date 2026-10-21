# Guía de paso a producción — ComfyUI "Everything-Ready" para Vast.ai

**Versión:** 1.1 · **Repositorio:** ComfyUIPod (rama `main`, estado de código verificado `0e8cafc`)
**Público objetivo:** tú, dentro de una tarde, con un café y una MacBook.

---

## A. Qué tienes ahora

Tienes una imagen Docker lista para producción que convierte cualquier GPU RTX 5090 o RTX 4090 alquilada en Vast.ai en un ComfyUI completo y privado, accesible 100% desde el navegador con usuario y contraseña. La imagen incluye ComfyUI con 10 custom nodes preinstalados, 63.35 GiB de modelos que se descargan automáticamente en dos fases (los ligeros antes de abrir, los pesados en segundo plano mientras trabajas), un portal de bienvenida en `/home`, un panel de estado en vivo en `/status` con métricas de GPU y diagnóstico automático, un supervisor en Rust (`comfyctl`) que verifica la máquina antes de gastar dinero en descargas, y varios perfiles de modelos (`all`, `general`, `realistic`, `anime`, `minimal`) para pagar solo por lo que necesitas. GitHub Actions construye y publica la imagen en GHCR, y el script `comfy` para macOS/Linux te deja alquilar, abrir, monitorizar y destruir instancias con un comando cada uno.

---

## B. Checklist de una tarde

### 1. Crear el repo en GitHub y subir el código — **YA HECHO en este repo**

> El remote `origin` ya existe y el código está empujado en `main` con CI en verde. Haz este paso **solo si empiezas de cero** con otro repo.

**Qué harías (caso desde cero):** crear un repositorio nuevo (vacío, sin README) en GitHub y empujar el código:

```bash
# Sustituye <tu-usuario> por tu usuario de GitHub
git remote add origin https://github.com/<tu-usuario>/comfyuipod.git
git push -u origin main
```

**Cómo sé que va bien:** el push termina sin errores y al refrescar la página del repo en GitHub ves tus archivos (`Dockerfile`, `comfyctl/`, `scripts/`...).

### 2. Esperar el primer workflow verde — **YA HECHO en este repo**

> El workflow ya corre en verde en cada push a `main`.

**Qué hace:** en la pestaña **Actions** del repo, el workflow *"Build, Validate and Publish ComfyUI Vast.ai Image (GHCR)"* valida los scripts, compila la imagen Docker, le hace un smoke test y la publica en GHCR. **Medido en CI: ≈ 10.5 min** el job de build reutilizando la caché `buildcache`; la primera ejecución sin caché tarda bastante más.

**Cómo sé que va bien:** el workflow aparece con un ✔ verde. Si falla, abre el job rojo y lee el paso marcado con ✘: cada nombre de paso indica qué se validaba (ShellCheck, Hadolint, SHAs de nodos, disponibilidad de modelos en HuggingFace, tests de Rust...).

### 3. Hacer público el paquete en GHCR

**Qué haces:** para que Vast.ai descargue la imagen sin credenciales, ve a tu perfil de GitHub → **Packages** → **comfyuipod** → **Package settings** → **Change visibility** → **Public**. El nombre final de la imagen será:

```
ghcr.io/<tu-usuario>/comfyuipod:latest
```

Confirma el nombre exacto tras el primer push (aparece en la página del paquete; GitHub lo pone en minúsculas).

**Cómo sé que va bien:** abres en una ventana de incógnito `https://github.com/<tu-usuario>/comfyuipod/pkgs/container/comfyuipod` y la página del paquete se ve sin iniciar sesión.

### 4. Crear cuenta en Vast.ai y añadir saldo

**Qué haces:** registrarte en [cloud.vast.ai](https://cloud.vast.ai), verificar la cuenta y cargar saldo. Con **$5–10** te sobra para todas las pruebas de esta guía (una sesión de aceptación de una hora cuesta céntimos).

**Cómo sé que va bien:** en *Billing* ves tu saldo disponible y en *Account* puedes copiar tu **API Key**.

### 5. Preparar la Mac (vastai + helper `comfy`)

**Qué haces:** instalas la CLI de Vast.ai, la autenticas e instalas el helper:

```bash
pipx install vastai          # (fallback: pip3 install --user vastai)
vastai set api-key <TU_VASTAI_API_KEY>

mkdir -p ~/.local/bin
cp tools/mac/comfy ~/.local/bin/comfy
chmod +x ~/.local/bin/comfy
```

Añade a tu `~/.zshrc` si no está ya: `export PATH="$HOME/.local/bin:$PATH"`

**Cómo sé que va bien:** `comfy all` lista tus instancias (ninguna aún) sin errores de autenticación.

### 6. Apuntar el helper a tu imagen

```bash
export COMFY_IMAGE=ghcr.io/<tu-usuario>/comfyuipod:latest
```

Hazlo persistente añadiéndolo también a tu `~/.zshrc` (mismo patrón que la línea del `PATH` del paso 5):

```bash
echo 'export COMFY_IMAGE=ghcr.io/<tu-usuario>/comfyuipod:latest' >> ~/.zshrc
```

**Cómo sé que va bien:** `comfy rent --dry-run` muestra la imagen correcta y el resumen de variables de entorno definidas (contraseña, `HF_TOKEN`, reenviadas opcionales) antes de pedir confirmación.

### 7. Primera sesión de aceptación

**Qué haces:**

```bash
comfy rent general   # perfil general: 27.75 GiB de modelos, buena prueba equilibrada
comfy open           # espera al arranque y abre el portal /home
comfy report         # descarga /report.md y lo copia al portapapeles
```

El helper te muestra el **Top 5 de ofertas RTX 5090 (o RTX 4090 si no hay 5090)** con **el precio por hora real antes de confirmar**: ahí ves exactamente lo que vas a pagar. El dry-run muestra además la imagen y el resumen de variables de entorno definidas (enmascaradas).

**Cómo sé que va bien:** primero ves la pantalla de espera (Fase 1 descargando los modelos ligeros, 1-3 min según red, tope 10); el portal `/home` carga con sus tarjetas; `/status` muestra GPU, VRAM y progreso de descargas; y al cabo de unos minutos ComfyUI abre y puedes arrastrar un workflow. Pulsa **R** en ComfyUI para refrescar los desplegables cuando termine la Fase 2 (~4–8 min a 1 Gbps / 500 Mbps en perfil `general`).

> La lista de verificaciones completa de esta primera sesión está en [`docs/auditorias/AUDITORIA_AGENTE_V5.md`](auditorias/AUDITORIA_AGENTE_V5.md) (sección 3). De esa checklist, CI ya cierra en cada push verde: **V1** (build sin errores), **V6** (sin `IMPORT FAILED`), **V7** (`hf`/`hf_xet` correctos) y **V12** (limpieza de `user/`); la publicación del paquete GHCR como **Public** es el paso 3 de esta guía.

### 8. Verificaciones clave de aceptación

1. **Sesión real con descargas:** en `/status`, la Fase 2 avanza y los checkpoints del perfil `general` aparecen en ComfyUI tras pulsar `R`.
2. **FATAL simulado:** alquila una instancia con una GPU pequeña (menos de 20 GB de VRAM, p. ej. una RTX 3060) — **créala a mano con `vastai create instance` (el helper filtra `gpu_ram>=24` y no puede alquilar GPUs pequeñas)**: el arranque estricto debe mostrar el **banner rojo FATAL** en `/home` y `/status`, y la instancia saldrá con código **20** pasados 10 minutos de gracia. Ejecuta `comfy destroy <id>` en cuanto lo veas para no pagar.
3. **`comfy watch`:** muestra el estado en vivo desde la terminal cada 3 segundos.
4. **Secretos redactados:** `comfy report --stdout` no muestra tu `COMFY_PASS` ni tokens: `comfyctl` los redacta automáticamente en `/report.md`.
5. **Variables con espacios (cierra V27 / S-07) — VERIFICADO:** `comfy rent` cita **solo el valor** (`-e COMFYUI_EXTRA_ARGS="--fast --cpu"`), que es el contrato del parser de vast-cli (limpia comillas del valor, nunca de la clave), así que la clave llega intacta y V27 **pasa**. Ciérralo así: alquila con `COMFYUI_EXTRA_ARGS="--fast --cpu"` exportada y comprueba en `/status/api` que `env_masked` muestra `COMFYUI_EXTRA_ARGS` con su valor correcto.

### 9. Al terminar: destruir la instancia

```bash
comfy destroy <id>
```

**Cómo sé que va bien:** `comfy all` ya no la lista, o la muestra destruyéndose. **El disco sigue facturando aunque pares la instancia**: solo `destroy` detiene todos los cargos.

---

## C. Costes aproximados

| Concepto | Coste aproximado | Notas |
|---|---|---|
| RTX 5090 (32 GB) | ~$0.45–0.95/h on-demand | El precio exacto se ve en `comfy rent` antes de confirmar (varía por oferta) |
| Disco (120 GB) | Unos céntimos/hora adicionales | El helper alquila con `--disk 120` (suficiente para `all`: 63 GiB modelos + ~16 GB imagen); usa `--disk 150+` a mano si acumulas mucho output. Incluido en la estimación de coste total/hora que muestra `comfy rent` |
| RTX 4090 (24 GB, fallback) | ~$0.30–0.55/h on-demand | El helper la busca automáticamente si no hay 5090 |
| Sesión de prueba de 1 h | Céntimos a ~$0.95 | Según la oferta elegida |
| Instancia **parada** (no destruida) | El disco **sigue facturando** | Por eso el paso 9 es `destroy`, no "stop" |

Regla de oro: **máquina encendida solo mientras la usas; `comfy destroy` al terminar.**

---

## D. Qué hacer si algo falla

| Síntoma | Qué significa | Qué hacer |
|---|---|---|
| Banner rojo **FATAL** en `/home` / `/status` | El arranque estricto (`strict-check` S1–S10) detectó un host incompatible (GPU, VRAM, RAM, disco...) | Ejecuta `comfy destroy <id>` y alquila otra oferta; el banner indica la causa y la remediación. La instancia sale sola con código 20 tras 10 min |
| Pantalla de espera eterna | La Fase 1 está descargando o algo la bloqueó | Abre `/status` y mira el progreso de descargas y la pestaña de logs; el motor de triaje (17 patrones) suele nombrar la causa exacta |
| Error 401 al abrir el portal | Contraseña incorrecta o no definida | `comfy pass <id>` recupera la contraseña autogenerada desde los logs de arranque |
| ComfyUI no responde pero el portal sí | El supervisor está reiniciando `main.py` (OOM, crash CUDA, etc.) | Mira `/status` (clasifica la salida: 137 OOM, 134/139 crash nativo...) y `/report.md` para el informe completo |
| Faltan modelos en los desplegables | Fase 2 aún en curso | Espera a que termine en `/status` y pulsa **R** en ComfyUI |
| `comfy rent` falla con la imagen | `COMFY_IMAGE` mal exportada o paquete GHCR privado | Revisa el paso 6 y que el paquete sea público (paso 3) |
| Algo raro sin nombre claro | — | `comfy report <id>`: el informe está pensado para pegárselo a un agente IA y que lo diagnostique |

---

## E. Notas de seguridad básicas

- **HTTP (no HTTPS):** el portal y ComfyUI van por HTTP en el puerto de Vast.ai, protegidos solo por Basic Auth: **las credenciales viajan en claro**. Es un riesgo **aceptado y documentado** para sesiones cortas con contraseña autogenerada (24 caracteres hex, única por instancia). Para uso sensible, accede por **túnel SSH** (`vastai ssh <id>` + forward local del 8188, p. ej. `ssh -L 8188:127.0.0.1:8188`) y abre `http://127.0.0.1:8188`. **`ENABLE_WEB_TERMINAL=1` (shell root) actívalo solo a través de un túnel SSH.**
- **No expongas más puertos:** solo el 8188 mapeado está previsto; no publiques otros.
- **Secretos fuera de los logs:** `COMFY_PASS` nunca se imprime si la defines tú, y `comfyctl` redacta secretos automáticamente en `/report.md` y `/status/api`. Aun así, no pegues informes en sitios públicos sin echarles un vistazo.
- **Destruye siempre:** una instancia olvidada es un gasto y una puerta abierta.

---

## F. Checklist final imprimible

- ☑ 1. Repo en GitHub creado y `git push -u origin main` hecho (**ya hecho en este repo**)
- ☑ 2. Workflow "Build, Validate and Publish" en verde en Actions (**ya hecho**; job de build ≈ 10.5 min con `buildcache`)
- ☐ 3. Paquete GHCR `comfyuipod` puesto como **Public**
- ☐ 4. Cuenta Vast.ai creada con $5–10 de saldo
- ☐ 5. `vastai` instalado (pipx) con API key y helper `comfy` en `~/.local/bin`
- ☐ 6. `export COMFY_IMAGE=ghcr.io/<tu-usuario>/comfyuipod:latest` (y añadido a `~/.zshrc`)
- ☐ 7. `comfy rent general` → `comfy open` → pantalla de espera, `/home`, `/status` y ComfyUI OK → `comfy report` (checklist completa en `docs/auditorias/AUDITORIA_AGENTE_V5.md` §3; V1/V6/V7/V12 ya los cierra CI, GHCR Public es el paso 3)
- ☐ 8. Verificaciones: descargas reales, FATAL simulado con GPU pequeña (creada a mano: el helper filtra `gpu_ram>=24`; código 20), `comfy watch`, secretos redactados en `/report.md`, `COMFYUI_EXTRA_ARGS="--fast --cpu"` visible en `env_masked` de `/status/api` (V27/S-07 verificado)
- ☐ 9. `comfy destroy <id>` ejecutado — instancia y cargos detenidos

---

*Generada a partir del estado verificado del repositorio en `main` (estado de código `0e8cafc`): README.md, docs/decisiones.md, docs/walkthrough.md, .github/workflows/docker-publish.yml y tools/mac/README.md.*
