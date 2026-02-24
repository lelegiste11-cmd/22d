"""
Bot Telegram de prédiction Baccarat - Version 6.0 FINAL
- 1 prédiction active maximum
- Vérification N, N+1, N+2, N+3 (3 rattrapages)
- Arrêt immédiat après premier trouvé
"""
import os
import asyncio
import re
import logging
import sys
import signal
import time
from datetime import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    PREDICTION_OFFSET, SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY, SUIT_NAMES
)

# ============ PROTECTION ANTI-ARRÊT 24/7 ============
def setup_protection():
    def ignore_shutdown(signum, frame):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] 🛡️  SIGNAL {signum} IGNORÉ")
        return
    
    for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
        signal.signal(sig, ignore_shutdown)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🛡️  PROTECTION 24/7 ACTIVE")

setup_protection()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Config: SOURCE={SOURCE_CHANNEL_ID}, PREDICTION={PREDICTION_CHANNEL_ID}, OFFSET={PREDICTION_OFFSET}")

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# ============ VARIABLES GLOBALES ============
active_prediction = None  # Une seule prédiction active
processed_messages = set()
source_channel_ok = False
prediction_channel_ok = False

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message"""
    try:
        match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
        if match:
            return int(match.group(1))
    except Exception as e:
        logger.error(f"Erreur extraction numéro: {e}")
    return None

def extract_parentheses_groups(message: str):
    """Extrait le contenu des parenthèses"""
    try:
        return re.findall(r"\(([^)]*)\)", message)
    except Exception as e:
        logger.error(f"Erreur extraction groupes: {e}")
        return []

def normalize_suits(group_str: str) -> str:
    """Normalise les symboles de couleur"""
    try:
        normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
        normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
        return normalized
    except Exception as e:
        logger.error(f"Erreur normalisation: {e}")
        return group_str

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    """Vérifie si la couleur target_suit est présente dans le groupe"""
    try:
        normalized_group = normalize_suits(group_str)
        normalized_target = normalize_suits(target_suit)
        count = normalized_group.count(normalized_target)
        return count >= 1
    except Exception as e:
        logger.error(f"Erreur vérification: {e}")
        return False

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé (contient ✅ ou 🔰)"""
    try:
        if '⏰' in message:
            return False
        return '✅' in message or '🔰' in message
    except Exception as e:
        logger.error(f"Erreur vérification: {e}")
        return False

def format_prediction_message(game_number: int, suit: str, status: str = "⏳ EN COURS") -> str:
    """Formate le message de prédiction"""
    suit_name = SUIT_NAMES.get(suit, suit)
    return f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
🌪️ Statut: {status}"""

async def send_prediction(target_game: int, suit: str, base_game: int):
    """Envoie une prédiction au canal"""
    global active_prediction
    
    try:
        msg = format_prediction_message(target_game, suit, "⏳ EN COURS")
        msg_id = 0

        if PREDICTION_CHANNEL_ID and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction envoyée: #{target_game} - {suit}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi: {e}")

        # Stocker la prédiction active
        active_prediction = {
            'game_number': target_game,
            'suit': suit,
            'base_game': base_game,
            'message_id': msg_id,
            'check_phase': 0,  # 0=N, 1=N+1, 2=N+2, 3=N+3
            'created_at': datetime.now().isoformat()
        }
        
        logger.info(f"🎯 ACTIVE: #{target_game} - {suit} | Vérifiera: N={target_game}, N+1={target_game+1}, N+2={target_game+2}, N+3={target_game+3}")
        return True
        
    except Exception as e:
        logger.error(f"Erreur création: {e}")
        return False

async def update_prediction_status(new_status: str):
    """Met à jour le statut de la prédiction active"""
    global active_prediction
    
    if not active_prediction:
        return False
    
    try:
        game_number = active_prediction['game_number']
        suit = active_prediction['suit']
        message_id = active_prediction['message_id']
        
        updated_msg = format_prediction_message(game_number, suit, new_status)

        # Mettre à jour le message Telegram
        if PREDICTION_CHANNEL_ID and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Statut mis à jour: {new_status}")
            except Exception as e:
                logger.error(f"❌ Erreur édition: {e}")
                # Envoyer nouveau message si édition échoue
                try:
                    await client.send_message(PREDICTION_CHANNEL_ID, updated_msg)
                except:
                    pass

        logger.info(f"🎉 PRÉDICTION #{game_number} TERMINÉE: {new_status}")
        
        # Supprimer la prédiction active (libère pour nouvelle prédiction)
        active_prediction = None
        logger.info("🔓 Prédiction libérée, prêt pour nouvelle prédiction")
        
        return True
        
    except Exception as e:
        logger.error(f"Erreur mise à jour: {e}")
        return False

async def check_prediction(game_number: int, first_group: str):
    """
    Vérifie si le jeu actuel correspond à la prédiction active
    Retourne True si la prédiction est résolue (trouvé ou perdu)
    """
    global active_prediction
    
    if not active_prediction:
        return False  # Aucune prédiction active
    
    pred_game = active_prediction['game_number']
    phase = active_prediction['check_phase']
    expected_game = pred_game + phase
    
    # Ce message est-il pour cette phase de vérification?
    if game_number != expected_game:
        return False  # Pas le bon numéro, ignorer
    
    suit = active_prediction['suit']
    found = has_suit_in_group(first_group, suit)
    
    logger.info(f"🔍 VÉRIFICATION #{pred_game} phase {phase} sur jeu #{game_number}")
    logger.info(f"   Recherche: {suit} dans ({first_group}) → {'✅ TROUVÉ' if found else '❌ NON'}")
    
    if found:
        # TROUVÉ ! Mettre à jour et libérer
        status_map = {0: '✅0️⃣', 1: '✅1️⃣', 2: '✅2️⃣', 3: '✅3️⃣'}
        status = status_map.get(phase, f'✅{phase}️⃣')
        await update_prediction_status(status)
        return True  # Résolue
        
    else:
        # PAS TROUVÉ, passer à phase suivante
        new_phase = phase + 1
        active_prediction['check_phase'] = new_phase
        
        if new_phase > 3:
            # Épuisé les 4 phases (0,1,2,3) = N, N+1, N+2, N+3
            logger.info(f"💔 PERDU après 4 vérifications (N à N+3)")
            await update_prediction_status('❌')
            return True  # Résolue (perdu)
        else:
            rattrapage = {1: '1er', 2: '2ème', 3: '3ème'}.get(new_phase, f'{new_phase}ème')
            logger.info(f"⏳ Passage au {rattrapage} rattrapage (vérifiera #{pred_game + new_phase})")
            return False  # Continue

async def create_prediction(game_number: int, first_group: str):
    """
    Crée une nouvelle prédiction UNIQUEMENT si aucune n'est active
    """
    global active_prediction
    
    # VÉRIFICATION CRUCIALE: Attendre que la prédiction active soit finalisée
    if active_prediction:
        logger.info(f"⏸️ BLOQUÉ: Prédiction #{active_prediction['game_number']} en cours, attente finalisation...")
        return False
    
    # Extraire la couleur du premier groupe
    normalized = normalize_suits(first_group)
    first_suit = None
    
    for char in normalized:
        if char in ALL_SUITS:
            first_suit = SUIT_DISPLAY.get(char, char)
            break
    
    if not first_suit:
        logger.warning(f"⚠️ Aucune couleur dans ({first_group})")
        return False
    
    # Créer la prédiction
    target_game = game_number + PREDICTION_OFFSET
    await send_prediction(target_game, first_suit, game_number)
    return True

@client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
async def handle_new_message(event):
    """Gestionnaire de messages du canal source"""
    try:
        message_text = event.message.text
        if not message_text:
            return
        
        game_number = extract_game_number(message_text)
        if game_number is None:
            return
        
        # Anti-doublon
        msg_hash = f"{game_number}_{message_text[:40]}"
        if msg_hash in processed_messages:
            return
        processed_messages.add(msg_hash)
        if len(processed_messages) > 200:
            processed_messages.clear()
        
        # Extraire premier groupe
        groups = extract_parentheses_groups(message_text)
        if not groups:
            return
        
        first_group = groups[0]
        is_finalized = is_message_finalized(message_text)
        
        logger.info(f"\n{'='*60}")
        logger.info(f"📥 #{game_number} | Finalisé: {is_finalized} | ({first_group})")
        
        # ÉTAPE 1: Si finalisé, vérifier la prédiction active
        if is_finalized:
            resolved = await check_prediction(game_number, first_group)
            if resolved:
                logger.info("✅ Prédiction résolue, nouvelle prédiction possible")
        
        # ÉTAPE 2: Créer nouvelle prédiction (uniquement si aucune active)
        created = await create_prediction(game_number, first_group)
        if created:
            logger.info("✨ Nouvelle prédiction créée")
        
        logger.info(f"{'='*60}")
        
    except Exception as e:
        logger.error(f"❌ Erreur handler: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def health_check(request):
    return web.Response(text="OK", status=200)

async def run_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Web port {PORT}")

async def check_channels():
    global source_channel_ok, prediction_channel_ok
    try:
        await client.get_entity(SOURCE_CHANNEL_ID)
        source_channel_ok = True
        logger.info("✅ Source OK")
    except Exception as e:
        logger.error(f"❌ Source: {e}")
    
    try:
        await client.get_entity(PREDICTION_CHANNEL_ID)
        prediction_channel_ok = True
        logger.info("✅ Prédiction OK")
    except Exception as e:
        logger.error(f"❌ Prédiction: {e}")

async def main():
    await run_web_server()
    await client.start(bot_token=BOT_TOKEN)
    logger.info("🤖 Bot connecté")
    await check_channels()
    
    # Boucle infinie protégée
    while True:
        try:
            await client.run_until_disconnected()
            logger.warning("⚠️ Déconnexion, reconnexion...")
            await asyncio.sleep(5)
            if not client.is_connected():
                await client.connect()
        except Exception as e:
            logger.error(f"💥 Erreur: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Manuel")
    except Exception as e:
        logger.error(f"💥 FATAL: {e}")
        time.sleep(3)
        os.execv(sys.executable, [sys.executable] + sys.argv)
