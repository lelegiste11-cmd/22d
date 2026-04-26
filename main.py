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
last_prediction_number: int = 0  # Dernier numéro de JEU sur lequel on a prédit
PREDICTION_STEP: int = 3  # Saut fixe entre prédictions (toujours +3)
PREDICTION_OFFSET: int = 32  # Offset fixe N+32


# ─── Utilitaires ─────────────────────────────────────────────────────────────

def extract_first_card_suit(group_text: str) -> str | None:
    """Extrait la première enseigne (suit) trouvée dans le texte du groupe"""
    suits = [c for c in group_text if c in SUIT_OPPOSITE]
    return suits[0] if suits else None


def build_prediction_text(target_n: int, suit: str, status: str) -> str:
    suit_display = SUIT_EMOJI.get(suit, suit)
    return f"🎰 PRÉDICTION #{target_n}\n🎯 Couleur: {suit_display}\n🌪️ Statut: {status}"


def parse_game(text: str) -> dict | None:
    """
    Parse le message du jeu.
    Format attendu: #N384. 6(6♦️14♠️) - ✅9(9♥️J♥️) #T15 🔴#R
    """
    GAME_RE = re.compile(r"#N(\d+)\.\s*(✅?)\s*\S+\(([^)]+)\)\s*-\s*(✅?)\s*\S+\(([^)]+)\)\s*#T\d+")
    m = GAME_RE.search(text)
    if not m:
        return None
    
    group1_cards = m.group(3)  # 1er groupe entre parenthèses
    
    return {
        "number": int(m.group(1)),
        "group1_first_suit": extract_first_card_suit(group1_cards),  # 1ère carte du 1er groupe
        "group1_suits": set(c for c in group1_cards if c in SUIT_OPPOSITE),
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
    global last_prediction_number

    game = parse_game(text)
    if not game:
        return

    n = game["number"]
    logger.info("Traitement jeu #N%d", n)

    # 1️⃣ VÉRIFICATION PRÉDICTIONS EN ATTENTE (mise à jour des statuts)
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
        elif n == pred["max_n"]:
            await _update_prediction_status(bot, pred, "❌")
            to_delete.append(trigger_key)

    for k in to_delete:
        pending_predictions.pop(k, None)

    # 2️⃣ NOUVEAU DÉCLENCHEUR
    # On prédit immédiatement dès que le jeu est éligible (saut de +3 depuis la dernière prédiction)
    
    if game["group1_first_suit"] is None:
        return
    
    # Vérifier si ce jeu est éligible pour une prédiction
    if last_prediction_number == 0:
        # Première prédiction : on accepte le premier jeu valide
        is_eligible = n not in processed_games
    else:
        # Vérifier le saut de +3 depuis la dernière prédiction
        expected_n = last_prediction_number + PREDICTION_STEP
        is_eligible = (n == expected_n) and (n not in processed_games)
    
    if not is_eligible:
        return

    # Calcul de la prédiction
    source_suit = game["group1_first_suit"]
    target_n = n + PREDICTION_OFFSET
    max_n = target_n + 2  # Vérification sur target, target+1, target+2

    logger.info("Déclencheur #N%d: 1ère carte 1er groupe = %s → prédit %s pour #%d", 
                n, source_suit, source_suit, target_n)

    try:
        sent = await bot.send_message(
            chat_id=PREDICTION_CHANNEL_ID,
            text=build_prediction_text(target_n, source_suit, "⏳⏳")
        )
        
        pending_predictions[n] = {
            "msg_id": sent.message_id,
            "chat_id": PREDICTION_CHANNEL_ID,
            "suit": source_suit,
            "target_n": target_n,
            "max_n": max_n,
        }
        processed_games.add(n)
        last_prediction_number = n
        
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
        "• Déclencheur: 1ère carte du 1er groupe\n"
        "• Prédiction: même couleur pour le jeu N+32\n"
        "• Saut fixe: +3 entre prédictions\n"
        "• Prédiction immédiate, pas d'attente de vérification\n"
        "• /stats - Statistiques\n"
        "• /reset - Réinitialiser",
        parse_mode=ParseMode.MARKDOWN
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    pending = len(pending_predictions)
    await update.message.reply_text(
        f"📊 Stats:\n🔮 En attente: {pending}\n📐 Saut: +{PREDICTION_STEP}\n🎮 Dernière prédiction sur jeu: #{last_prediction_number}\n🎮 Traités: {len(processed_games)}",
        parse_mode=ParseMode.MARKDOWN
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global processed_games, pending_predictions, last_prediction_number
    if update.effective_user.id != ADMIN_ID:
        return
    processed_games.clear()
    pending_predictions.clear()
    last_prediction_number = 0
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
