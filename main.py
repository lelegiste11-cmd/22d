import os
import re
import logging
import asyncio
from datetime import datetime
from collections import defaultdict
from telegram import Bot, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# Import de la configuration
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID,
    PORT, DEFAULT_OFFSET, FAILURE_OFFSET,
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
    """Extrait la couleur de la première carte d'un groupe"""
    suits = [c for c in group_text if c in SUIT_OPPOSITE]
    return suits[0] if suits else None


def is_odd_number(n: int) -> bool:
    """Vérifie si un nombre se termine par 1,3,5,7,9"""
    last_digit = n % 10
    return last_digit in [1, 3, 5, 7, 9]


def build_prediction_text(target_n: int, suit: str, status: str) -> str:
    """Construit le texte de prédiction au format demandé"""
    suit_display = SUIT_EMOJI.get(suit, suit)
    return (
        f"🎰 PRÉDICTION #{target_n}\n"
        f"🎯 Couleur: {suit_display}\n"
        f"🌪️ Statut: {status}"
    )


def parse_game(text: str) -> dict | None:
    """Parse un message de jeu"""
    # Format : #N371. ✅7(5♥️7♦️5♥️) - 5(A♥️Q♦️4♣️) #T12
    GAME_RE = re.compile(
        r"#N(\d+)\.\s*(✅?)\s*\S+\(([^)]+)\)\s*-\s*(✅?)\s*\S+\(([^)]+)\)\s*#T\d+"
    )
    
    m = GAME_RE.search(text)
    if not m:
        return None
        
    group2_cards = m.group(5)
    return {
        "number": int(m.group(1)),
        "group1_cards": m.group(3),
        "group2_cards": group2_cards,
        "group1_suits": set(c for c in m.group(3) if c in SUIT_OPPOSITE),
        "group2_first_suit": extract_first_card_suit(group2_cards),
    }


# ─── Logique principale ───────────────────────────────────────────────────────

async def _update_prediction_status(bot: Bot, pred: dict, status: str) -> None:
    """Met à jour le statut d'une prédiction"""
    new_text = build_prediction_text(pred["target_n"], pred["suit"], status)
    try:
        await bot.edit_message_text(
            chat_id=pred["chat_id"],
            message_id=pred["msg_id"],
            text=new_text,
        )
        logger.info("Prédiction #%d → %s (couleur: %s)", pred["target_n"], status, pred["suit"])
    except TelegramError as e:
        logger.error("Impossible de modifier le message de prédiction : %s", e)


async def process_game(bot: Bot, chat_id: int, text: str) -> None:
    """Traite un message de jeu"""
    global last_prediction_failed

    game = parse_game(text)
    if not game:
        return

    n = game["number"]
    logger.info("Message jeu détecté : #N%d", n)

    # ── 1️⃣ VÉRIFICATION DES PRÉDICTIONS EN ATTENTE ─────────────────────────
    to_delete: list[int] = []

    for trigger_key, pred in list(pending_predictions.items()):
        target_n = pred["target_n"]
        max_n = pred["max_n"]
        suit = pred["suit"]

        if n < target_n or n > max_n:
            continue

        # Vérification: la couleur prédite est-elle dans le 1er groupe ?
        found = suit in game["group1_suits"]

        if found:
            offset = n - target_n
            if offset == 0:
                status = "✅0️⃣"
            elif offset == 1:
                status = "✅1️⃣"
            else:
                status = "✅2️⃣"
            
            await _update_prediction_status(bot, pred, status)
            to_delete.append(trigger_key)
            last_prediction_failed = False
            
        elif n == max_n:
            await _update_prediction_status(bot, pred, "❌")
            to_delete.append(trigger_key)
            last_prediction_failed = True

    for k in to_delete:
        pending_predictions.pop(k, None)

    # ── 2️⃣ DÉTECTION DE DÉCLENCHEUR ─────────────────────────────────────────
    # Déclencheur: 2ème groupe reçoit sa 1ère carte + numéro impair
    if game["group2_first_suit"] and is_odd_number(n) and n not in processed_games:
        source_suit = game["group2_first_suit"]
        predicted_suit = SUIT_OPPOSITE[source_suit]
        
        # Offset: +2 normal, +4 après échec
        offset = FAILURE_OFFSET if last_prediction_failed else DEFAULT_OFFSET
        target_n = n + offset
        max_n = target_n + 2

        logger.info(
            "Déclencheur #N%d: carte %s → prédit %s pour #N%d..%d",
            n, source_suit, predicted_suit, target_n, max_n
        )

        pred_text = build_prediction_text(target_n, predicted_suit, "⏳⏳")
        try:
            sent = await bot.send_message(
                chat_id=PREDICTION_CHANNEL_ID, 
                text=pred_text
            )
            
            pending_predictions[n] = {
                "msg_id": sent.message_id,
                "chat_id": PREDICTION_CHANNEL_ID,
                "suit": predicted_suit,
                "trigger_n": n,
                "target_n": target_n,
                "max_n": max_n,
                "source_suit": source_suit,
            }
            processed_games.add(n)
            
            logger.info("Prédiction envoyée: #N%d → %s", target_n, predicted_suit)
            
        except TelegramError as e:
            logger.error("Erreur envoi prédiction : %s", e)


# ─── Handlers Telegram ───────────────────────────────────────────────────────

async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler pour les messages du canal source"""
    post = update.channel_post or update.edited_channel_post
    if not post or not post.text:
        return
    
    if post.chat_id == SOURCE_CHANNEL_ID:
        await process_game(context.bot, post.chat_id, post.text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("👋 Bot Légiste actif !\n/help pour les commandes")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
        
    await update.message.reply_text(
        "📖 *Aide — Bot Légiste*\n\n"
        "*Logique :*\n"
        "• Déclencheur: 2ème groupe reçoit 1ère carte (numéro impair)\n"
        "• Prédiction: opposé de la 1ère carte du 2ème groupe\n"
        "• Offset: +2 normal, +4 après échec\n"
        "• Vérification: 1er groupe uniquement\n\n"
        "*Commandes :*\n"
        "/stats — Statistiques\n"
        "/reset — Réinitialiser\n"
        "/offset — Voir l'offset",
        parse_mode=ParseMode.MARKDOWN,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
        
    pending = len(pending_predictions)
    offset_type = "+4 (après échec)" if last_prediction_failed else "+2 (normal)"
    
    text = (
        f"📊 *Statistiques*\n\n"
        f"🔮 Prédictions en attente: *{pending}*\n"
        f"📐 Offset: *{offset_type}*\n"
        f"🎮 Jeux traités: *{len(processed_games)}*"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global processed_games, pending_predictions, last_prediction_failed
    
    if update.effective_user.id != ADMIN_ID:
        return
        
    processed_games = set()
    pending_predictions = {}
    last_prediction_failed = False
    
    logger.info("Réinitialisation par %s", update.effective_user)
    await update.message.reply_text("🔄 Réinitialisation complète effectuée.")


async def offset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
        
    offset_type = "+4 (après échec)" if last_prediction_failed else "+2 (normal)"
    await update.message.reply_text(f"📐 Offset actuel : *{offset_type}*", parse_mode=ParseMode.MARKDOWN)


# ─── Point d'entrée ──────────────────────────────────────────────────────────

def main() -> None:
    # Validation de la configuration
    validate_config()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("offset", offset_command))

    app.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POSTS | filters.UpdateType.EDITED_CHANNEL_POST,
            channel_post_handler,
        )
    )

    logger.info("Bot démarré sur port %d", PORT)
    logger.info("Source: %s | Prédiction: %s", SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID)
    
    # Configuration Render.com (port 10000)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=None,  # Render gère le webhook automatiquement
    )


if __name__ == "__main__":
    main()
