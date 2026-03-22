import os
import re
import logging
import asyncio
from datetime import datetime
from http import HTTPStatus
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from telegram import Update, Bot
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID,
    PORT, WEBHOOK_URL, DEFAULT_OFFSET, FAILURE_OFFSET,
    SUIT_OPPOSITE, SUIT_EMOJI, validate_config
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── État global ──────────────────────────────────────────────────────────────
processed_games: set[int] = set()
pending_predictions: dict[int, dict] = {}
last_prediction_failed: bool = False


# ─── Utilitaires ─────────────────────────────────────────────────────────────

def extract_first_card_suit(group_text: str) -> str | None:
    suits = [c for c in group_text if c in SUIT_OPPOSITE]
    return suits[0] if suits else None


def is_odd_number(n: int) -> bool:
    return (n % 10) in [1, 3, 5, 7, 9]


def build_prediction_text(target_n: int, suit: str, status: str) -> str:
    suit_display = SUIT_EMOJI.get(suit, suit)
    return f"🎰 PRÉDICTION #{target_n}\n🎯 Couleur: {suit_display}\n🌪️ Statut: {status}"


def parse_game(text: str) -> dict | None:
    GAME_RE = re.compile(r"#N(\d+)\.\s*(✅?)\s*\S+\(([^)]+)\)\s*-\s*(✅?)\s*\S+\(([^)]+)\)\s*#T\d+")
    m = GAME_RE.search(text)
    if not m:
        return None
    group2_cards = m.group(5)
    return {
        "number": int(m.group(1)),
        "group1_suits": set(c for c in m.group(3) if c in SUIT_OPPOSITE),
        "group2_first_suit": extract_first_card_suit(group2_cards),
    }


# ─── Logique de prédiction ───────────────────────────────────────────────────

async def _update_prediction_status(bot: Bot, pred: dict, status: str) -> None:
    new_text = build_prediction_text(pred["target_n"], pred["suit"], status)
    try:
        await bot.edit_message_text(
            chat_id=pred["chat_id"],
            message_id=pred["msg_id"],
            text=new_text,
        )
        logger.info("Prédiction #%d → %s", pred["target_n"], status)
    except TelegramError as e:
        logger.error("Erreur modification message: %s", e)


async def process_game_message(bot: Bot, text: str) -> None:
    global last_prediction_failed

    game = parse_game(text)
    if not game:
        return

    n = game["number"]
    logger.info("Traitement jeu #N%d", n)

    # 1️⃣ VÉRIFICATION PRÉDICTIONS EN ATTENTE
    to_delete = []
    for trigger_key, pred in list(pending_predictions.items()):
        if n < pred["target_n"] or n > pred["max_n"]:
            continue

        found = pred["suit"] in game["group1_suits"]
        
        if found:
            offset = n - pred["target_n"]
            status = "✅0️⃣" if offset == 0 else "✅1️⃣" if offset == 1 else "✅2️⃣"
            await _update_prediction_status(bot, pred, status)
            to_delete.append(trigger_key)
            last_prediction_failed = False
        elif n == pred["max_n"]:
            await _update_prediction_status(bot, pred, "❌")
            to_delete.append(trigger_key)
            last_prediction_failed = True

    for k in to_delete:
        pending_predictions.pop(k, None)

    # 2️⃣ NOUVEAU DÉCLENCHEUR (2ème groupe, numéro impair)
    if game["group2_first_suit"] and is_odd_number(n) and n not in processed_games:
        source_suit = game["group2_first_suit"]
        predicted_suit = SUIT_OPPOSITE[source_suit]
        offset = FAILURE_OFFSET if last_prediction_failed else DEFAULT_OFFSET
        target_n = n + offset
        max_n = target_n + 2

        logger.info("Déclencheur #N%d: %s → prédit %s (offset +%d)", n, source_suit, predicted_suit, offset)

        try:
            sent = await bot.send_message(
                chat_id=PREDICTION_CHANNEL_ID,
                text=build_prediction_text(target_n, predicted_suit, "⏳⏳")
            )
            
            pending_predictions[n] = {
                "msg_id": sent.message_id,
                "chat_id": PREDICTION_CHANNEL_ID,
                "suit": predicted_suit,
                "target_n": target_n,
                "max_n": max_n,
            }
            processed_games.add(n)
        except TelegramError as e:
            logger.error("Erreur envoi: %s", e)


# ─── Handlers Telegram ───────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("👋 Bot actif! /help pour l'aide")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "📖 *Aide*\n"
        "• Déclencheur: 2ème groupe reçoit 1ère carte (impair)\n"
        "• Prédiction: opposé de cette carte\n"
        "• Offset: +2 normal, +4 après ❌\n"
        "• /stats - Statistiques\n"
        "• /reset - Réinitialiser",
        parse_mode=ParseMode.MARKDOWN
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    pending = len(pending_predictions)
    offset = "+4" if last_prediction_failed else "+2"
    await update.message.reply_text(
        f"📊 Stats:\n🔮 En attente: {pending}\n📐 Offset: {offset}\n🎮 Traités: {len(processed_games)}",
        parse_mode=ParseMode.MARKDOWN
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global processed_games, pending_predictions, last_prediction_failed
    if update.effective_user.id != ADMIN_ID:
        return
    processed_games.clear()
    pending_predictions.clear()
    last_prediction_failed = False
    await update.message.reply_text("🔄 Réinitialisé!")


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post = update.channel_post or update.edited_channel_post
    if not post or not post.text:
        return
    if post.chat_id == SOURCE_CHANNEL_ID:
        await process_game_message(context.bot, post.text)


# ─── Configuration Application PTB ────────────────────────────────────────────

# Initialisation PTB (sans updater pour webhook)
ptb = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .updater(None)  # Important: pas d'updater en mode webhook
    .build()
)

# Ajout des handlers
ptb.add_handler(CommandHandler("start", start))
ptb.add_handler(CommandHandler("help", help_command))
ptb.add_handler(CommandHandler("stats", stats_command))
ptb.add_handler(CommandHandler("reset", reset_command))
ptb.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, channel_post_handler))


# ─── Configuration FastAPI ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Gestion du cycle de vie de l'application"""
    # Démarrage
    if WEBHOOK_URL:
        await ptb.bot.set_webhook(url=f"{WEBHOOK_URL}/telegram")
        logger.info(f"Webhook configuré: {WEBHOOK_URL}/telegram")
    
    async with ptb:
        await ptb.start()
        logger.info("Bot démarré")
        yield
        await ptb.stop()
        logger.info("Bot arrêté")


app = FastAPI(lifespan=lifespan)


@app.post("/telegram")
async def process_update(request: Request):
    """Endpoint pour recevoir les webhooks Telegram"""
    data = await request.json()
    update = Update.de_json(data, ptb.bot)
    await ptb.process_update(update)
    return Response(status_code=HTTPStatus.OK)


@app.get("/")
async def health():
    """Health check pour Render"""
    return {"status": "alive", "predictions_pending": len(pending_predictions)}


@app.get("/webhook-info")
async def webhook_info():
    """Info sur le webhook configuré"""
    info = await ptb.bot.get_webhook_info()
    return {
        "url": info.url,
        "has_custom_certificate": info.has_custom_certificate,
        "pending_update_count": info.pending_update_count,
    }


# ─── Point d'entrée ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    validate_config()
    logger.info(f"Démarrage sur le port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
