import os
import html
import json
import logging
import sqlite3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("publicador")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ===== CONFIGURACIÓN CON VARIABLES DE ENTORNO =====
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("❌ ERROR: Variable de entorno TELEGRAM_TOKEN no configurada")

CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))
GROUP_ID = int(os.environ.get("GROUP_ID", "0"))

if not CHANNEL_ID or not GROUP_ID:
    raise ValueError("❌ ERROR: CHANNEL_ID y GROUP_ID deben estar configurados en variables de entorno")

# Ruta de base de datos
if os.environ.get("RENDER"):
    DB_PATH = os.path.join("/tmp", "publicaciones.db")
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "publicaciones.db")

CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "").lstrip("@")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("")

MAX_ARCHIVOS = 20

MENU, CAMPO_VALOR, ARCHIVOS, CONFIRMAR = range(4)
ED_ELEGIR, ED_CAMPO, ED_VALOR = range(4, 7)
EL_ELEGIR = 10
LOTE_FILES = 11

ESTADOS = {
    "encurso": "En curso",
    "completa": "Completa",
    "pausada": "Pausada",
    "abandonada": "Abandonada",
}

IDIOMAS = {
    "espanol": "Español",
    "ingles": "Inglés",
}

TECLADO_PRINCIPAL = ReplyKeyboardMarkup(
    [
        ["📖 Publicar", "📚 Lote"],
        ["✏️ Editar", "🗑 Eliminar"],
    ],
    resize_keyboard=True,
)

def esc(texto: str) -> str:
    return html.escape(texto, quote=False)

def iniciar_db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS publicaciones ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "user_id INTEGER NOT NULL, username TEXT, nombre_usuario TEXT, "
        "nombre TEXT NOT NULL, capitulos INTEGER, estado TEXT NOT NULL, "
        "idioma TEXT, sinopsis TEXT, foto TEXT, ficha_msg_id INTEGER NOT NULL, "
        "archivos_msg_ids TEXT NOT NULL DEFAULT '[]', "
        "fecha TEXT NOT NULL, editada TEXT)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor TEXT)")
    try:
        con.execute("ALTER TABLE publicaciones ADD COLUMN idioma TEXT")
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()

def ejecutar(sql, params=(), fetch=None):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, params)
        res = cur.fetchall() if fetch == "all" else (cur.fetchone() if fetch == "one" else cur.lastrowid)
        con.commit()
        return res
    finally:
        con.close()

def publicaciones_de(user_id: int, limite: int = 20):
    return ejecutar(
        "SELECT * FROM publicaciones WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limite), fetch="all",
    )

def obtener_publicacion(pid: int):
    return ejecutar("SELECT * FROM publicaciones WHERE id = ?", (pid,), fetch="one")

def actualizar_publicacion(pid: int, **campos):
    sets = ", ".join(f"{c} = ?" for c in campos)
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ejecutar(
        f"UPDATE publicaciones SET {sets}, editada = ? WHERE id = ?",
        (*campos.values(), ahora, pid),
    )

def construir_ficha(nombre, capitulos, estado, idioma, sinopsis, autor, tiene_foto=False) -> str:
    limite = 900 if tiene_foto else 3800
    lineas_meta = [f"📖 <b>{esc(nombre)}</b>\n"]
    if capitulos:
        lineas_meta.append(f"📚 <b>Capítulos:</b> {capitulos}")
    lineas_meta.append(f"📌 <b>Estado:</b> {esc(estado)}")
    if idioma:
        lineas_meta.append(f"🌐 <b>Idioma:</b> {esc(idioma)}")
    
    pie = f"\n👤 <b>Traducción de:</b> {esc(autor)}"
    meta_txt = "\n".join(lineas_meta)
    
    if not sinopsis:
        return meta_txt + pie
    
    sinopsis_header = "\n📝 <b>Sinopsis:</b>\n"
    espacio_disponible = limite - len(meta_txt) - len(sinopsis_header) - len(pie)
    
    if espacio_disponible > 50:
        if len(sinopsis) > espacio_disponible:
            sinopsis_truncada = sinopsis[:espacio_disponible - 3].rstrip() + "..."
        else:
            sinopsis_truncada = sinopsis
        texto = f"{meta_txt}{sinopsis_header}{esc(sinopsis_truncada)}{pie}"
    else:
        texto = meta_txt + pie
    return texto

def panel_menu_markup(d: dict) -> InlineKeyboardMarkup:
    nombre_txt = f"📖 Nombre: {d.get('nombre') or '❌ Obligatorio'}"
    estado_txt = f"📌 Estado: {d.get('estado', 'En curso')}"
    cap_txt = f"📚 Capítulos: {d.get('capitulos') or 'Opcional'}"
    idio_txt = f"🌐 Idioma: {d.get('idioma') or 'Opcional'}"
    sin_txt = f"📝 Sinopsis: {'Configurada' if d.get('sinopsis') else 'Opcional'}"
    foto_txt = f"🖼 Portada: {'Configurada' if d.get('foto') else 'Opcional'}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(nombre_txt, callback_data="set:nombre")],
        [InlineKeyboardButton(estado_txt, callback_data="set:estado")],
        [InlineKeyboardButton(cap_txt, callback_data="set:capitulos"),
         InlineKeyboardButton(idio_txt, callback_data="set:idioma")],
        [InlineKeyboardButton(sin_txt, callback_data="set:sinopsis"),
         InlineKeyboardButton(foto_txt, callback_data="set:foto")],
        [InlineKeyboardButton("✅ Terminar y subir archivos", callback_data="terminar")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ])

async def es_miembro(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        m = await context.bot.get_chat_member(GROUP_ID, user_id)
        return m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER) or \
               (m.status == ChatMemberStatus.RESTRICTED and getattr(m, "is_member", False))
    except Exception:
        return False

async def es_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        m = await context.bot.get_chat_member(GROUP_ID, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await es_miembro(context, user.id):
        await update.message.reply_text("⛔ Solo los miembros del grupo pueden usar este bot.")
        return
    await update.message.reply_text(
        "👋 ¡Bienvenido al bot publicador de traducciones!\n\nUsa los botones inferiores para gestionar tus publicaciones:",
        reply_markup=TECLADO_PRINCIPAL,
    )

async def iniciar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await es_miembro(context, user.id):
        await update.message.reply_text("⛔ Solo los miembros del grupo pueden publicar traducciones.", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    context.user_data["novela"] = {"estado": "En curso", "archivos": []}
    await update.message.reply_text(
        "🛠 <b>Panel de Publicación</b>\n\nConfigura los datos usando los botones:",
        reply_markup=panel_menu_markup(context.user_data["novela"]),
        parse_mode=ParseMode.HTML,
    )
    await update.message.reply_text("⬇️ Panel activo (teclado inferior oculto)", reply_markup=ReplyKeyboardRemove())
    return MENU

async def iniciar_lote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await es_miembro(context, user.id):
        await update.message.reply_text("⛔ Solo los miembros del grupo pueden publicar en lote.", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    context.user_data["lote"] = {"archivos": []}
    await update.message.reply_text(
        "📎 Envía los archivos que quieres publicar en lote (máx. {}).\nCuando termines, pulsa el botón <b>Listo</b>.".format(MAX_ARCHIVOS),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Listo", callback_data="lote_listo")]]),
        parse_mode=ParseMode.HTML,
    )
    await update.message.reply_text("⬇️ Enviar archivos (teclado inferior oculto)", reply_markup=ReplyKeyboardRemove())
    return LOTE_FILES

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "cancelar":
        context.user_data.clear()
        await q.edit_message_text("Operación cancelada.")
        await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    if data == "terminar":
        d = context.user_data["novela"]
        if not d.get("nombre"):
            await q.answer("⚠️ El nombre es obligatorio para continuar.", show_alert=True)
            return MENU
        
        await q.message.reply_text(
            f"📎 Envía los <b>archivos</b> de la novela (máx. {MAX_ARCHIVOS}).\n"
            "Cuando termines, pulsa el botón <b>Listo</b>.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Listo", callback_data="archivos_listos")]]),
            parse_mode=ParseMode.HTML,
        )
        return ARCHIVOS

    if data.startswith("set:"):
        campo = data.split(":")[1]
        context.user_data["campo_actual"] = campo

        if campo == "estado":
            botones = [[InlineKeyboardButton(v, callback_data=f"est:{k}")] for k, v in ESTADOS.items()]
            await q.message.reply_text("📌 Selecciona el estado:", reply_markup=InlineKeyboardMarkup(botones))
            return CAMPO_VALOR
        elif campo == "idioma":
            botones = [[InlineKeyboardButton(v, callback_data=f"idio:{k}")] for k, v in IDIOMAS.items()]
            botones.append([InlineKeyboardButton("🗑 Quitar idioma", callback_data="idio:quitar")])
            await q.message.reply_text("🌐 Selecciona el idioma:", reply_markup=InlineKeyboardMarkup(botones))
            return CAMPO_VALOR
        elif campo == "sinopsis":
            await q.message.reply_text(
                "📝 Escribe la sinopsis:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Quitar sinopsis", callback_data="quitar_sinopsis")]]))
            return CAMPO_VALOR
        elif campo == "foto":
            await q.message.reply_text(
                "🖼 Envía la portada como foto:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Quitar portada", callback_data="quitar_foto")]]))
            return CAMPO_VALOR
        else:
            pedidos = {
                "nombre": "📖 Escribe el nombre de la novela:",
                "capitulos": "📚 Escribe el número total de capítulos (solo número):"
            }
            await q.message.reply_text(pedidos.get(campo, "Escribe el valor:"))
            return CAMPO_VALOR

async def recibir_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    campo = context.user_data.get("campo_actual")
    d = context.user_data["novela"]
    
    if update.message.photo and campo == "foto":
        d["foto"] = update.message.photo[-1].file_id
    elif update.message.text:
        txt = update.message.text.strip()
        if campo == "nombre":
            if not txt or len(txt) > 200:
                await update.message.reply_text("El nombre debe tener entre 1 y 200 caracteres.")
                return CAMPO_VALOR
            d["nombre"] = txt
        elif campo == "capitulos":
            if not txt.isdigit() or int(txt) <= 0:
                await update.message.reply_text("Escribe un número válido (ej. 120).")
                return CAMPO_VALOR
            d["capitulos"] = int(txt)
        elif campo == "sinopsis":
            d["sinopsis"] = txt

    await update.message.reply_text(
        "🛠 <b>Panel de Publicación actualizado:</b>",
        reply_markup=panel_menu_markup(d),
        parse_mode=ParseMode.HTML,
    )
    return MENU

async def callback_valor_especial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = context.user_data["novela"]
    data = q.data

    if data.startswith("est:"):
        d["estado"] = ESTADOS[data.split(":")[1]]
    elif data.startswith("idio:"):
        val = data.split(":")[1]
        d["idioma"] = None if val == "quitar" else IDIOMAS[val]
    elif data == "quitar_sinopsis":
        d["sinopsis"] = None
    elif data == "quitar_foto":
        d["foto"] = None

    await q.edit_message_text("✅ Actualizado.")
    await q.message.reply_text(
        "🛠 <b>Panel de Publicación:</b>",
        reply_markup=panel_menu_markup(d),
        parse_mode=ParseMode.HTML,
    )
    return MENU

async def recibir_archivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data["novela"]
    if len(d["archivos"]) >= MAX_ARCHIVOS:
        await update.message.reply_text(f"Máximo de {MAX_ARCHIVOS} archivos alcanzado.")
        return ARCHIVOS
    d["archivos"].append(update.message.document.file_id)
    await update.message.reply_text(f"📎 Archivos recibidos: {len(d['archivos'])}")
    return ARCHIVOS

async def recibir_lote_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.get("lote")
    if not d:
        d = {"archivos": []}
        context.user_data["lote"] = d
    if len(d["archivos"]) >= MAX_ARCHIVOS:
        await update.message.reply_text(f"Máximo de {MAX_ARCHIVOS} archivos alcanzado.")
        return LOTE_FILES
    doc = update.message.document
    d["archivos"].append({"file_id": doc.file_id, "file_name": doc.file_name})
    await update.message.reply_text(f"📎 Archivos recibidos (lote): {len(d['archivos'])}")
    return LOTE_FILES

async def archivos_listos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = context.user_data["novela"]
    if not d["archivos"]:
        await q.answer("Debes subir al menos un archivo.", show_alert=True)
        return ARCHIVOS
    await q.answer()
    await q.edit_message_reply_markup(None)

    autor = f"@{q.from_user.username}" if q.from_user.username else (q.from_user.full_name or "Anónimo")
    tiene_foto = bool(d.get("foto"))
    ficha_texto = construir_ficha(d["nombre"], d.get("capitulos"), d["estado"], d.get("idioma"), d.get("sinopsis"), autor, tiene_foto)

    if tiene_foto:
        await context.bot.send_photo(q.message.chat_id, d["foto"], caption=ficha_texto, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(q.message.chat_id, ficha_texto, parse_mode=ParseMode.HTML)

    await q.message.reply_text(
        f"¿Publicar esta novela con {len(d['archivos'])} archivo(s) en el canal?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Publicar", callback_data="publicar"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cancelar"),
        ]])
    )
    return CONFIRMAR

async def lote_listo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = context.user_data.get("lote")
    if not d or not d.get("archivos"):
        await q.answer("Debes subir al menos un archivo.", show_alert=True)
        return LOTE_FILES
    await q.edit_message_reply_markup(None)

    autor = f"@{q.from_user.username}" if q.from_user.username else (q.from_user.full_name or "Anónimo")
    bot = context.bot
    publicados = 0
    for item in d["archivos"]:
        file_id = item["file_id"]
        fname = item.get("file_name") or "Documento"
        nombre = os.path.splitext(fname)[0]
        ficha_texto = construir_ficha(nombre, None, "En curso", None, None, autor, False)
        try:
            m = await bot.send_document(CHANNEL_ID, file_id, caption=ficha_texto, parse_mode=ParseMode.HTML)
            ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                ejecutar(
                    "INSERT INTO publicaciones (user_id, username, nombre_usuario, nombre, capitulos, estado, idioma, sinopsis, foto, ficha_msg_id, archivos_msg_ids, fecha) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (q.from_user.id, q.from_user.username, q.from_user.full_name, nombre, None, "En curso", None, None, None, m.message_id, json.dumps([]), ahora),
                )
            except Exception:
                log.warning("No se pudo guardar la publicación en la DB. Seguiremos sin persistencia local.")
            publicados += 1
        except Exception as e:
            log.exception("Error publicando item de lote: %s", e)

    try:
        await actualizar_indice(bot)
    except Exception:
        log.exception("Error actualizando indice después de lote")

    await q.message.reply_text(f"✅ Publicados {publicados} archivo(s) en el canal.")
    context.user_data.clear()
    await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
    return ConversationHandler.END

async def publicar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(None)
    d = context.user_data.get("novela")
    if not d:
        return ConversationHandler.END

    autor = f"@{q.from_user.username}" if q.from_user.username else (q.from_user.full_name or "Anónimo")
    tiene_foto = bool(d.get("foto"))
    ficha_texto = construir_ficha(d["nombre"], d.get("capitulos"), d["estado"], d.get("idioma"), d.get("sinopsis"), autor, tiene_foto)

    try:
        if tiene_foto:
            ficha = await context.bot.send_photo(CHANNEL_ID, d["foto"], caption=ficha_texto, parse_mode=ParseMode.HTML)
        else:
            ficha = await context.bot.send_message(CHANNEL_ID, ficha_texto, parse_mode=ParseMode.HTML)

        ids_arch = []
        for i in range(0, len(d["archivos"]), 10):
            bloque = d["archivos"][i:i+10]
            if len(bloque) == 1:
                m = await context.bot.send_document(CHANNEL_ID, bloque[0], reply_to_message_id=ficha.message_id)
                ids_arch.append(m.message_id)
            else:
                ms = await context.bot.send_media_group(CHANNEL_ID, [InputMediaDocument(fid) for fid in bloque], reply_to_message_id=ficha.message_id)
                ids_arch.extend(x.message_id for x in ms)

        ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ejecutar(
            "INSERT INTO publicaciones (user_id, username, nombre_usuario, nombre, capitulos, estado, idioma, sinopsis, foto, ficha_msg_id, archivos_msg_ids, fecha) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (q.from_user.id, q.from_user.username, q.from_user.full_name, d["nombre"], d.get("capitulos"), d["estado"], d.get("idioma"), d.get("sinopsis"), d.get("foto"), ficha.message_id, json.dumps(ids_arch), ahora),
        )
    except Exception as e:
        log.exception("Error publicando")
        await q.message.reply_text(f"⚠️ Ocurrió un error al publicar en el canal: {str(e)[:100]}")
    else:
        try:
            await actualizar_indice(context.bot)
        except Exception:
            log.exception("Error actualizando indice después de publicar")
        await q.message.reply_text("✅ ¡Publicado con éxito en el canal!")

    context.user_data.clear()
    await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
    return ConversationHandler.END

async def editar_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await es_miembro(context, user.id):
        await update.message.reply_text("⛔ Solo los miembros del grupo pueden editar publicaciones.", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    if await es_admin(context, user.id):
        filas = ejecutar("SELECT * FROM publicaciones ORDER BY id DESC LIMIT 30", fetch="all")
    else:
        filas = publicaciones_de(user.id)

    if not filas:
        await update.message.reply_text("Aún no hay publicaciones registradas.", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    botones = [
        [InlineKeyboardButton(f"✏️ {f['nombre'][:35]} (#{f['id']})", callback_data=f"ed:{f['id']}")]
        for f in filas
    ]
    botones.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")])
    await update.message.reply_text(
        "✏️ Selecciona la publicación que deseas <b>editar</b>:",
        reply_markup=InlineKeyboardMarkup(botones),
        parse_mode=ParseMode.HTML,
    )
    await update.message.reply_text("⬇️ Menú de edición activo", reply_markup=ReplyKeyboardRemove())
    return ED_ELEGIR

async def editar_elegir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "cancelar":
        context.user_data.clear()
        await q.edit_message_text("Operación cancelada.")
        await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    pub = obtener_publicacion(int(data.split(":")[1]))
    if not pub:
        await q.edit_message_text("No encontré esa publicación.")
        await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    context.user_data["ed_id"] = pub["id"]
    botones = [
        [InlineKeyboardButton("📖 Nombre", callback_data="ecam:nombre"),
         InlineKeyboardButton("📚 Capítulos", callback_data="ecam:capitulos")],
        [InlineKeyboardButton("📌 Estado", callback_data="ecam:estado"),
         InlineKeyboardButton("🌐 Idioma", callback_data="ecam:idioma")],
        [InlineKeyboardButton("📝 Sinopsis", callback_data="ecam:sinopsis"),
         InlineKeyboardButton("🖼 Portada", callback_data="ecam:portada")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ]
    await q.edit_message_text(
        f"✏️ <b>{esc(pub['nombre'])}</b>\n¿Qué campo quieres cambiar?",
        reply_markup=InlineKeyboardMarkup(botones),
        parse_mode=ParseMode.HTML,
    )
    return ED_CAMPO

async def editar_campo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "cancelar":
        context.user_data.clear()
        await q.edit_message_text("Operación cancelada.")
        await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    campo = data.split(":")[1]
    pub = obtener_publicacion(context.user_data["ed_id"])
    context.user_data["ed_campo"] = campo

    if campo == "estado":
        botones = [[InlineKeyboardButton(v, callback_data=f"enest:{k}")] for k, v in ESTADOS.items()]
        await q.edit_message_text("📌 Selecciona el nuevo estado:", reply_markup=InlineKeyboardMarkup(botones))
    elif campo == "idioma":
        botones = [[InlineKeyboardButton(v, callback_data=f"enidio:{k}")] for k, v in IDIOMAS.items()]
        botones.append([InlineKeyboardButton("🗑 Quitar idioma", callback_data="enidio:quitar")])
        await q.edit_message_text("🌐 Selecciona el nuevo idioma:", reply_markup=InlineKeyboardMarkup(botones))
    elif campo == "sinopsis":
        await q.edit_message_text(
            "📝 Escribe la nueva sinopsis:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Quitar sinopsis", callback_data="enquitar_sin")]]))
    elif campo == "portada":
        await q.edit_message_text("🖼 Envía la nueva portada como foto:")
    else:
        pedidos = {
            "nombre": "📖 Escribe el nuevo nombre:",
            "capitulos": "📚 Escribe el nuevo número de capítulos:"
        }
        await q.edit_message_text(pedidos.get(campo, "Escribe el nuevo valor:"))
    return ED_VALOR

async def editar_valor_recibir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    campo = context.user_data.get("ed_campo")
    pub = obtener_publicacion(context.user_data["ed_id"]) 
    bot = context.bot
    valor = None

    if update.message.photo and campo == "portada":
        valor = update.message.photo[-1].file_id
    elif update.message.text:
        txt = update.message.text.strip()
        if campo == "nombre":
            if not txt or len(txt) > 200:
                await update.message.reply_text("El nombre debe tener entre 1 y 200 caracteres.")
                return ED_VALOR
            valor = txt
        elif campo == "capitulos":
            if not txt.isdigit() or int(txt) <= 0:
                await update.message.reply_text("Escribe un número válido.")
                return ED_VALOR
            valor = int(txt)
        elif campo == "sinopsis":
            valor = txt

    if valor is not None or campo == "sinopsis":
        await aplicar_cambio_edicion(bot, pub, campo, valor)
        await update.message.reply_text("✅ Publicación actualizada con éxito.", reply_markup=TECLADO_PRINCIPAL)
        context.user_data.clear()
        return ConversationHandler.END
    return ED_VALOR

async def editar_valor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    pub = obtener_publicacion(context.user_data["ed_id"]) 
    bot = context.bot
    campo = context.user_data.get("ed_campo")
    valor = None

    if data.startswith("enest:"):
        campo = "estado"
        valor = ESTADOS[data.split(":")[1]]
    elif data.startswith("enidio:"):
        campo = "idioma"
        val = data.split(":")[1]
        valor = None if val == "quitar" else IDIOMAS[val]
    elif data == "enquitar_sin":
        campo = "sinopsis"
        valor = None

    if campo and (valor is not None or campo == "sinopsis"):
        await aplicar_cambio_edicion(bot, pub, campo, valor)
        await q.edit_message_text("✅ Publicación actualizada con éxito.")
        await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
        context.user_data.clear()
        return ConversationHandler.END
    return ED_VALOR

async def aplicar_cambio_edicion(bot, pub, campo, valor):
    nombre = valor if campo == "nombre" else pub["nombre"]
    capitulos = valor if campo == "capitulos" else pub["capitulos"]
    estado = valor if campo == "estado" else pub["estado"]
    idioma = valor if campo == "idioma" else pub["idioma"]
    sinopsis = valor if campo == "sinopsis" else pub["sinopsis"]
    foto = valor if campo == "portada" else pub["foto"]

    autor = f"@{pub['username']}" if pub["username"] else (pub["nombre_usuario"] or "Anónimo")
    tiene_foto = bool(foto)
    texto = construir_ficha(nombre, capitulos, estado, idioma, sinopsis, autor, tiene_foto)

    if campo == "portada":
        await bot.edit_message_media(
            chat_id=CHANNEL_ID,
            message_id=pub["ficha_msg_id"],
            media=InputMediaPhoto(foto, caption=texto, parse_mode=ParseMode.HTML),
        )
        actualizar_publicacion(pub["id"], foto=foto)
    else:
        if pub["foto"]:
            try:
                await bot.edit_message_caption(chat_id=CHANNEL_ID, message_id=pub["ficha_msg_id"], caption=texto, parse_mode=ParseMode.HTML)
            except Exception:
                pass
        else:
            try:
                await bot.edit_message_text(texto, chat_id=CHANNEL_ID, message_id=pub["ficha_msg_id"], parse_mode=ParseMode.HTML)
            except Exception:
                pass
        
        if campo == "nombre":
            actualizar_publicacion(pub["id"], nombre=nombre)
        elif campo == "capitulos":
            actualizar_publicacion(pub["id"], capitulos=capitulos)
        elif campo == "estado":
            actualizar_publicacion(pub["id"], estado=estado)
        elif campo == "idioma":
            actualizar_publicacion(pub["id"], idioma=idioma)
        elif campo == "sinopsis":
            actualizar_publicacion(pub["id"], sinopsis=sinopsis)

    await actualizar_indice(bot)

async def eliminar_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await es_miembro(context, user.id):
        await update.message.reply_text("⛔ Solo los miembros del grupo pueden eliminar publicaciones.", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    if await es_admin(context, user.id):
        filas = ejecutar("SELECT * FROM publicaciones ORDER BY id DESC LIMIT 50", fetch="all")
    else:
        filas = publicaciones_de(user.id)

    if not filas:
        await update.message.reply_text("No hay publicaciones registradas.", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    botones = [
        [InlineKeyboardButton(f"🗑 {f['nombre'][:35]} (#{f['id']})", callback_data=f"el:{f['id']}")]
        for f in filas
    ]
    botones.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")])
    await update.message.reply_text(
        "🗑️ Selecciona la publicación que deseas <b>eliminar</b> del canal y del índice:",
        reply_markup=InlineKeyboardMarkup(botones),
        parse_mode=ParseMode.HTML,
    )
    await update.message.reply_text("⬇️ Menú de eliminación activo", reply_markup=ReplyKeyboardRemove())
    return ED_ELEGIR

async def eliminar_ejecutar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "cancelar":
        context.user_data.clear()
        await q.edit_message_text("Operación cancelada.")
        await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    pid = int(data.split(":")[1])
    pub = obtener_publicacion(pid)

    if not pub:
        await q.edit_message_text("No encontré esa publicación.")
        await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    if pub["user_id"] != q.from_user.id and not await es_admin(context, q.from_user.id):
        await q.edit_message_text("No tienes permisos para eliminar esta publicación.")
        await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    bot = context.bot
    try:
        archivos_ids = json.loads(pub["archivos_msg_ids"] or "[]")
        if archivos_ids:
            try:
                await bot.delete_messages(CHANNEL_ID, archivos_ids)
            except Exception:
                pass

        try:
            await bot.delete_message(CHANNEL_ID, pub["ficha_msg_id"])
        except Exception:
            pass

        ejecutar("DELETE FROM publicaciones WHERE id = ?", (pid,))
    except Exception as e:
        log.exception("Error al eliminar la publicación %s", pid)
        await q.edit_message_text(f"⚠️ Ocurrió un error al intentar eliminar: {str(e)[:100]}")
        await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
        return ConversationHandler.END

    try:
        await actualizar_indice(bot)
    except Exception:
        log.exception("Error actualizando indice después de eliminar")

    await q.edit_message_text("✅ Publicación eliminada del canal, de la base de datos y del índice con éxito.")
    await q.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
    return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Operación cancelada.")
        await query.message.reply_text("Menú principal:", reply_markup=TECLADO_PRINCIPAL)
    else:
        await update.message.reply_text("Operación cancelada.", reply_markup=TECLADO_PRINCIPAL)
    return ConversationHandler.END

SIN_VISTA_PREVIA = LinkPreviewOptions(is_disabled=True)

def meta_get(clave):
    try:
        r = ejecutar("SELECT valor FROM meta WHERE clave = ?", (clave,), fetch="one")
        return r["valor"] if r else None
    except Exception:
        return None

def meta_set(clave, valor):
    try:
        ejecutar(
            "INSERT INTO meta (clave, valor) VALUES (?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
            (clave, valor),
        )
    except Exception:
        log.warning("No se pudo guardar meta en la base de datos")

def enlace(msg_id: int) -> str:
    if CHANNEL_USERNAME:
        return f"https://t.me/{CHANNEL_USERNAME}/{msg_id}"
    return f"https://t.me/c/{str(CHANNEL_ID).removeprefix('-100')}/{msg_id}"

def _clave(nombre: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", nombre.strip())
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()

def textos_indice(pubs) -> list[str]:
    if not pubs:
        return ["📚 <b>Índice de novelas</b>\n\nTodavía no hay publicaciones."]

    ordenadas = sorted(pubs, key=lambda p: _clave(p["nombre"]))
    mensajes = []
    actual, ents, inicial = f"📚 <b>Índice de novelas</b> ({len(ordenadas)})\n", 1, None

    for p in ordenadas:
        ini = _clave(p["nombre"])[:1].upper()
        if not ini.isalpha():
            ini = "#"
        
        cap_str = f' · {p["capitulos"]} caps' if p["capitulos"] is not None else ''
        idio_str = f' [{esc(p["idioma"])}]' if p["idioma"] else ''
        
        linea = (
            f'• <a href="{enlace(p["ficha_msg_id"])}">{esc(p["nombre"])}</a>'
            f'{cap_str}{idio_str} · {esc(p["estado"]) }'
        )
        if len(actual) + len(linea) + 40 > 4000 or ents + 2 > 95:
            mensajes.append(actual)
            actual, ents, inicial = "📚 <b>Índice (continuación)</b>\n", 1, None
        if ini != inicial:
            actual += f"\n<b>{esc(ini)}</b>"
            ents += 1
            inicial = ini
        actual += "\n" + linea
        ents += 1

    mensajes.append(actual)
    return mensajes

def ids_indice() -> list[int]:
    val = meta_get("indice_ids")
    if val:
        try:
            return json.loads(val)
        except Exception:
            return []
    return []

async def actualizar_indice(bot):
    try:
        pubs = []
        try:
            pubs = ejecutar("SELECT * FROM publicaciones", fetch="all")
        except Exception:
            pubs = []

        textos = textos_indice(pubs)
        ids = ids_indice()

        if not ids:
            try:
                chat = await bot.get_chat(CHANNEL_ID)
                pinned = getattr(chat, "pinned_message", None)
                if pinned and getattr(pinned, "text", "").startswith("📚"):
                    ids = [pinned.message_id]
            except Exception:
                pass

        nuevos = []

        for i, t in enumerate(textos):
            if i < len(ids):
                try:
                    await bot.edit_message_text(
                        t, chat_id=CHANNEL_ID, message_id=ids[i],
                        parse_mode=ParseMode.HTML, link_preview_options=SIN_VISTA_PREVIA,
                    )
                    nuevos.append(ids[i])
                    continue
                except Exception:
                    pass
            m = await bot.send_message(
                CHANNEL_ID, t, parse_mode=ParseMode.HTML, link_preview_options=SIN_VISTA_PREVIA
            )
            nuevos.append(m.message_id)
            if i == 0:
                try:
                    await bot.pin_chat_message(CHANNEL_ID, m.message_id, disable_notification=True)
                except Exception:
                    pass

        for mid in ids[len(textos):]:
            try:
                await bot.delete_message(CHANNEL_ID, mid)
            except Exception:
                pass

        try:
            meta_set("indice_ids", json.dumps(nuevos))
        except Exception:
            log.warning("No se pudo guardar indice en la DB; el índice se mantendrá en mensajes anclados si es posible.")
    except Exception:
        log.exception("No se pudo actualizar el índice")

# --- Inicialización de la aplicación Telegram ---
iniciar_db()
ptb_app = Application.builder().token(TOKEN).build()

conv_publicar = ConversationHandler(
    entry_points=[
        CommandHandler("publicar", iniciar, filters=filters.ChatType.PRIVATE),
        MessageHandler(filters.Regex("^📖 Publicar$") & filters.ChatType.PRIVATE, iniciar),
    ],
    states={
        MENU: [CallbackQueryHandler(menu_callback, pattern=r"^(set:|terminar|cancelar)")],
        CAMPO_VALOR: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_valor),
            MessageHandler(filters.PHOTO, recibir_valor),
            CallbackQueryHandler(callback_valor_especial, pattern=r"^(est:|idio:|quitar_)") ,
        ],
        ARCHIVOS: [
            MessageHandler(filters.Document.ALL, recibir_archivo),
            CallbackQueryHandler(archivos_listos, pattern=r"^archivos_listos$"),
        ],
        CONFIRMAR: [
            CallbackQueryHandler(publicar, pattern=r"^publicar$"),
            CallbackQueryHandler(cancelar, pattern=r"^cancelar$"),
        ],
    },
    fallbacks=[CommandHandler("cancelar", cancelar)],
    allow_reentry=True,
)

conv_lote = ConversationHandler(
    entry_points=[
        CommandHandler("lote", iniciar_lote, filters=filters.ChatType.PRIVATE),
        MessageHandler(filters.Regex("^📚 Lote$") & filters.ChatType.PRIVATE, iniciar_lote),
    ],
    states={
        LOTE_FILES: [
            MessageHandler(filters.Document.ALL, recibir_lote_file),
            CallbackQueryHandler(lote_listo, pattern=r"^lote_listo$"),
        ],
    },
    fallbacks=[CommandHandler("cancelar", cancelar)],
    allow_reentry=True,
)

conv_editar = ConversationHandler(
    entry_points=[
        CommandHandler("editar", editar_inicio, filters=filters.ChatType.PRIVATE),
        MessageHandler(filters.Regex("^✏️ Editar$") & filters.ChatType.PRIVATE, editar_inicio),
    ],
    states={
        ED_ELEGIR: [
            CallbackQueryHandler(editar_elegir, pattern=r"^(ed:\d+|cancelar)$"),
        ],
        ED_CAMPO: [
            CallbackQueryHandler(editar_campo, pattern=r"^(ecam:|cancelar)"),
        ],
        ED_VALOR: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, editar_valor_recibir),
            MessageHandler(filters.PHOTO, editar_valor_recibir),
            CallbackQueryHandler(editar_valor_callback, pattern=r"^(enest:|enidio:|enquitar_sin)"),
        ],
    },
    fallbacks=[CommandHandler("cancelar", cancelar)],
    allow_reentry=True,
)

conv_eliminar = ConversationHandler(
    entry_points=[
        CommandHandler("eliminar", eliminar_inicio, filters=filters.ChatType.PRIVATE),
        MessageHandler(filters.Regex("^🗑 Eliminar$") & filters.ChatType.PRIVATE, eliminar_inicio),
    ],
    states={
        EL_ELEGIR: [
            CallbackQueryHandler(eliminar_ejecutar, pattern=r"^(el:\d+|cancelar)$"),
        ],
    },
    fallbacks=[CommandHandler("cancelar", cancelar)],
    allow_reentry=True,
)

ptb_app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
ptb_app.add_handler(conv_publicar)
ptb_app.add_handler(conv_lote)
ptb_app.add_handler(conv_editar)
ptb_app.add_handler(conv_eliminar)

# --- Contexto de lifespan para FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🚀 Iniciando aplicación...")
    await ptb_app.initialize()
    await ptb_app.start()
    log.info("✅ Aplicación Telegram inicializada")
    
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/telegram-webhook"
        log.info(f"🔗 Configurando webhook: {webhook_url}")
        try:
            await ptb_app.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
            log.info(f"✅ Webhook configurado correctamente en: {webhook_url}")
        except Exception as e:
            log.error(f"❌ Error configurando webhook: {e}")
    else:
        log.warning("⚠️ RENDER_EXTERNAL_URL no configurada. El bot no recibirá mensajes.")
    
    yield
    
    log.info("🛑 Deteniendo aplicación...")
    await ptb_app.stop()
    log.info("✅ Aplicación detenida")

# --- Crear aplicación FastAPI ---
app = FastAPI(lifespan=lifespan)

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        log.debug(f"📨 Update recibido: {data.get('update_id')}")
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
        return {"ok": True}
    except Exception as e:
        log.error(f"❌ Error procesando update: {e}")
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

@app.get("/health")
async def health():
    return {"status": "ok", "db": DB_PATH, "webhook_url": f"{RENDER_EXTERNAL_URL}/telegram-webhook" if RENDER_EXTERNAL_URL else "Not configured"}

@app.get("/")
async def root():
    return {"status": "Bot activo", "endpoints": ["/health", "/telegram-webhook"]}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    log.info(f"🌐 Iniciando servidor FastAPI en puerto {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
