import os
import asyncio
import re
import logging
import sys
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    PREDICTION_OFFSET, SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY, SUIT_NAMES
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
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

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, PREDICTION_CHANNEL_ID={PREDICTION_CHANNEL_ID}")
logger.info(f"Paramètre de prédiction: OFFSET={PREDICTION_OFFSET}")

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

pending_predictions = {}
queued_predictions = {}
recent_games = {}
processed_messages = set()
processed_finalized = set()
last_transferred_game = None
current_game_number = 0
prediction_offset = PREDICTION_OFFSET

MAX_PENDING_PREDICTIONS = 5
PROXIMITY_THRESHOLD = 2

source_channel_ok = False
prediction_channel_ok = False

# ============ VARIABLES GLOBALES ============
transfer_enabled = True

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message"""
    match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_parentheses_groups(message: str):
    """Extrait le contenu des parenthèses"""
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suits(group_str: str) -> str:
    """Normalise les symboles de couleur"""
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def get_suits_in_group(group_str: str):
    """Retourne la liste des couleurs présentes dans le groupe"""
    normalized = normalize_suits(group_str)
    return [s for s in ALL_SUITS if s in normalized]

def extract_first_card_suit(group_str: str):
    """Extrait la couleur de la première carte du groupe"""
    normalized = normalize_suits(group_str)
    for char in normalized:
        if char in ALL_SUITS:
            return SUIT_DISPLAY.get(char, char)
    return None

def get_suit_full_name(suit_symbol: str) -> str:
    """Retourne le nom complet de la couleur"""
    return SUIT_NAMES.get(suit_symbol, suit_symbol)

def get_alternate_suit(suit: str) -> str:
    """Retourne la couleur alternative (pour backup)"""
    return SUIT_MAPPING.get(suit, suit)

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé (contient ✅ ou 🔰)"""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

def format_prediction_message(game_number: int, suit: str, status: str = "⏳ EN COURS", result_group: str = None) -> str:
    """
    Formate le message de prédiction:
    📡 PRÉDICTION #74
    🎯 Couleur: ❤️ Cœur
    🌪️ Statut: ⏳ EN COURS / 🍯✅0️⃣ / 😶❌
    """
    suit_name = get_suit_full_name(suit)
    
    # Message initial
    if status == "⏳ EN COURS":
        return f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
🌪️ Statut: {status}"""
    
    # Message après résultat
    return f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
🌪️ Statut: {status}"""

async def send_prediction_to_channel(target_game: int, suit: str, base_game: int):
    """Envoie une prédiction au canal de prédiction immédiatement"""
    try:
        prediction_msg = format_prediction_message(target_game, suit, "⏳ EN COURS")
        
        msg_id = 0

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction envoyée au canal: Jeu #{target_game} - {suit}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction au canal: {e}")
        else:
            logger.warning(f"⚠️ Canal de prédiction non accessible, prédiction non envoyée")

        # Initialisation
        pending_predictions[target_game] = {
            'message_id': msg_id,
            'suit': suit,
            'base_game': base_game,
            'status': '⏳ EN COURS',
            'check_count': 0,  # 0=N, 1=N+1, 2=N+2, 3=N+3
            'last_checked_game': 0,
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"Prédiction active créée: Jeu #{target_game} - {suit} (basé sur #{base_game})")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

async def update_prediction_status(game_number: int, new_status: str, result_group: str = None):
    """
    Met à jour le statut d'une prédiction et la supprime des actives si terminée
    """
    try:
        if game_number not in pending_predictions:
            logger.warning(f"⚠️ Prédiction #{game_number} non trouvée pour mise à jour")
            return False

        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        suit = pred['suit']
        
        # Créer le message mis à jour
        updated_msg = format_prediction_message(game_number, suit, new_status, result_group)

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {new_status}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour dans le canal: {e}")
        else:
            logger.warning(f"⚠️ Canal non accessible, statut mis à jour en mémoire uniquement")

        pred['status'] = new_status
        logger.info(f"Prédiction #{game_number} statut mis à jour: {new_status}")

        # Supprimer des prédictions actives si terminée
        if new_status in ['🍯✅0️⃣', '🍯✅1️⃣', '🍯✅2️⃣', '🍯✅3️⃣', '😶❌']:
            if game_number in pending_predictions:
                del pending_predictions[game_number]
                logger.info(f"Prédiction #{game_number} terminée et supprimée")

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """
    Vérifie si une prédiction est gagnée ou perdue.
    Condition: AU MOINS 1 carte de la couleur dans le premier groupe
    Logique séquentielle: N → N+1 → N+2 → N+3
    Finalise immédiatement si trouvé, ou à N+3 si perdu
    """
    normalized_group = normalize_suits(first_group)
    
    logger.info(f"=== VÉRIFICATION RÉSULTAT ===")
    logger.info(f"Message finalisé reçu: Jeu #{game_number}")
    logger.info(f"Premier groupe analysé: ({first_group})")
    logger.info(f"Prédictions en attente: {list(pending_predictions.keys())}")
    
    # Pour chaque prédiction active, vérifier si ce jeu correspond à son tour
    for pred_game, pred in list(pending_predictions.items()):
        target_suit = pred['suit']
        check_count = pred.get('check_count', 0)
        normalized_target = normalize_suits(target_suit)
        
        # Calculer quel numéro doit être vérifié pour cette étape
        expected_game = pred_game + check_count
        
        logger.info(f"  → Prédiction #{pred_game}: étape {check_count}, attend #{expected_game}, reçu #{game_number}")
        
        # Vérifier seulement si c'est le bon numéro pour cette étape
        if game_number != expected_game:
            continue
        
        # C'est le bon numéro, vérifier le résultat
        # CORRECTION: Au moins 1 carte (et non 3)
        suit_count = normalized_group.count(normalized_target)
        has_card = suit_count >= 1  # AU MOINS 1 carte suffit
        
        logger.info(f"  🔍 VÉRIFICATION #{pred_game}: {target_suit} trouvé {suit_count} fois dans ({first_group})")
        
        if has_card:
            # GAGNÉ ! Finaliser immédiatement avec le bon statut
            status_map = {0: '🍯✅0️⃣', 1: '🍯✅1️⃣', 2: '🍯✅2️⃣', 3: '🍯✅3️⃣'}
            new_status = status_map.get(check_count, '🍯✅0️⃣')
            
            await update_prediction_status(pred_game, new_status, first_group)
            logger.info(f"  🎉 PRÉDICTION #{pred_game} GAGNÉE! {suit_count}x {target_suit} trouvé | Statut: {new_status}")
            return True
        
        else:
            # PAS trouvé, passer à l'étape suivante
            new_check_count = check_count + 1
            pred['check_count'] = new_check_count
            pred['last_checked_game'] = game_number
            
            if new_check_count > 3:
                # Échec définitif après N+3 (4 tentatives), finaliser comme perdu
                await update_prediction_status(pred_game, '😶❌', first_group)
                logger.info(f"  💔 PRÉDICTION #{pred_game} PERDUE après 4 tentatives (aucune carte trouvée)")
                
                # Créer backup seulement si perdu
                suit = pred['suit']
                backup_game = pred_game + prediction_offset
                alternate_suit = get_alternate_suit(suit)
                await create_prediction(backup_game, alternate_suit, pred_game, is_backup=True)
                return False
            else:
                logger.info(f"  ⏳ #{pred_game}: Aucune carte {target_suit}, passage à l'étape {new_check_count} (vérifiera #{pred_game + new_check_count})")
    
    return None

async def create_prediction(target_game: int, suit: str, base_game: int, is_backup: bool = False):
    """Crée une nouvelle prédiction"""
    if target_game in pending_predictions or target_game in queued_predictions:
        logger.info(f"Prédiction #{target_game} déjà existante, ignorée")
        return False
    
    # Envoyer immédiatement la prédiction
    await send_prediction_to_channel(target_game, suit, base_game)
    return True

async def process_new_message(message_text: str, chat_id: int, is_finalized: bool = False):
    """
    Traite un nouveau message du canal source.
    - CRÉE les prédictions IMMÉDIATEMENT
    - VÉRIFIE et FINALISE les résultats UNIQUEMENT si finalisé
    """
    global current_game_number, last_transferred_game
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            logger.warning(f"⚠️ Numéro non trouvé dans: {message_text[:50]}...")
            return
        
        current_game_number = game_number
        
        # Éviter le traitement double
        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)
        
        if len(processed_messages) > 200:
            processed_messages.clear()
        
        groups = extract_parentheses_groups(message_text)
        if len(groups) < 1:
            logger.warning(f"⚠️ Aucun groupe trouvé dans: {message_text[:50]}...")
            return
        
        first_group = groups[0]
        
        logger.info(f"=" * 60)
        logger.info(f"📨 TRAITEMENT Jeu #{game_number} | Finalisé: {is_finalized}")
        logger.info(f"   Premier groupe: ({first_group})")
        
        # ========== CRÉATION DE PRÉDICTION (TOUJOURS) ==========
        first_card_suit = extract_first_card_suit(first_group)
        
        if first_card_suit:
            target_game = game_number + prediction_offset
            
            if target_game not in pending_predictions and len(pending_predictions) < MAX_PENDING_PREDICTIONS:
                await create_prediction(target_game, first_card_suit, game_number)
                logger.info(f"   🎯 NOUVELLE PRÉDICTION: #{target_game} - {first_card_suit} (dans +{prediction_offset} jeux)")
            elif target_game in pending_predictions:
                logger.info(f"   ⏭️ Prédiction #{target_game} existe déjà")
            else:
                logger.info(f"   ⏸️ Max prédictions atteint ({MAX_PENDING_PREDICTIONS})")
        else:
            logger.warning(f"   ⚠️ Impossible d'extraire la couleur de: ({first_group})")
        
        # ========== VÉRIFICATION ET FINALISATION (UNIQUEMENT SI FINALISÉ) ==========
        if is_finalized:
            finalized_hash = f"finalized_{game_number}"
            if finalized_hash not in processed_finalized:
                processed_finalized.add(finalized_hash)
                
                # Transfert du message si activé
                if transfer_enabled and ADMIN_ID and ADMIN_ID != 0 and last_transferred_game != game_number:
                    try:
                        transfer_msg = f"📨 **Message finalisé:**\n\n{message_text}"
                        await client.send_message(ADMIN_ID, transfer_msg)
                        last_transferred_game = game_number
                        logger.info(f"   📤 Message transféré à l'admin")
                    except Exception as e:
                        logger.error(f"   ❌ Erreur transfert: {e}")
                
                # Vérifier et finaliser les résultats
                logger.info(f"   ✅ MESSAGE FINALISÉ - Vérification du premier groupe...")
                await check_prediction_result(game_number, first_group)
                
                if len(processed_finalized) > 100:
                    processed_finalized.clear()
        else:
            logger.info(f"   ⏳ Message non finalisé, pas de vérification")
        
        # Stocker le jeu pour référence
        recent_games[game_number] = {
            'first_group': first_group,
            'timestamp': datetime.now().isoformat()
        }
        
        if len(recent_games) > 100:
            oldest = min(recent_games.keys())
            del recent_games[oldest]
            
    except Exception as e:
        logger.error(f"Erreur traitement message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ==================== EVENT HANDLERS ====================

@client.on(events.NewMessage())
async def handle_message(event):
    """Gère les nouveaux messages - PRÉDICTION IMMÉDIATE"""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"📥 Message reçu: {message_text[:80]}...")
            
            is_finalized = is_message_finalized(message_text)
            await process_new_message(message_text, chat_id, is_finalized)
            
    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    """Gère les messages édités (finalisation)"""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"✏️ Message édité: {message_text[:80]}...")
            
            is_finalized = is_message_finalized(message_text)
            
            if is_finalized:
                logger.info(f"✅ Finalisé - Vérification")
                await process_new_message(message_text, chat_id, is_finalized=True)
            else:
                logger.info(f"⏳ Pas encore finalisé")
            
    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# ==================== COMMANDES ADMIN ====================

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    
    await event.respond("""🤖 **Bot de Prédiction Baccarat - v3.3**

📡 PRÉDICTION #74
🎯 Couleur: ❤️ Cœur
🌪️ Statut: ⏳ EN COURS

**Règle:** Au moins 1 carte dans le premier groupe

**Résultats:**
🍯✅0️⃣ | 🍯✅1️⃣ | 🍯✅2️⃣ | 🍯✅3️⃣ | 😶❌

**Commandes:**
• `/status` - Voir les prédictions
• `/setoffset <n>` - Changer le décalage
• `/help` - Aide""")

@client.on(events.NewMessage(pattern='/setoffset'))
async def cmd_setoffset(event):
    if event.is_group or event.is_channel:
        return
    
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Réservé admin")
        return
    
    global prediction_offset
    
    try:
        text = event.message.message
        parts = text.split()
        
        if len(parts) < 2:
            await event.respond(f"Usage: `/setoffset <n>`\nActuel: **{prediction_offset}**")
            return
        
        new_offset = int(parts[1])
        
        if new_offset < 1 or new_offset > 50:
            await event.respond("Décalage: 1-50")
            return
        
        prediction_offset = new_offset
        await event.respond(f"✅ Décalage: **+{prediction_offset}**")
        
    except ValueError:
        await event.respond("Entrez un nombre")
    except Exception as e:
        logger.error(f"Erreur: {e}")
        await event.respond(f"❌ Erreur")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Réservé admin")
        return
    
    status_msg = f"📊 **État:**\n\n"
    status_msg += f"🎮 Jeu: #{current_game_number}\n"
    status_msg += f"📏 Décalage: +{prediction_offset}\n"
    status_msg += f"🎯 Condition: ≥1 carte dans 1er groupe\n\n"
    
    if pending_predictions:
        status_msg += f"**🔮 Actives ({len(pending_predictions)}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            suit_name = get_suit_full_name(pred['suit'])
            etape = pred.get('check_count', 0)
            etape_txt = {0: 'N', 1: 'N+1', 2: 'N+2', 3: 'N+3'}.get(etape, f'N+{etape}')
            status_msg += f"• #{game_num}: {pred['suit']} | Vérifier: {etape_txt} | {pred['status']}\n"
    else:
        status_msg += "**🔮 Aucune prédiction active**\n"
    
    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    await event.respond(f"""📖 **Aide v3.3**

**Format:**
📡 PRÉDICTION #N
🎯 Couleur: [suit] [nom]
🌪️ Statut: [statut]

**Règle de victoire:**
Au moins **1 carte** de la couleur dans la **première parenthèse**

**Statuts:**
• ⏳ EN COURS = En attente
• 🍯✅0️⃣ = Trouvé à N (immédiat)
• 🍯✅1️⃣ = Trouvé à N+1
• 🍯✅2️⃣ = Trouvé à N+2
• 🍯✅3️⃣ = Trouvé à N+3
• 😶❌ = Perdu (pas trouvé après N+3)

**Décalage:** +{prediction_offset}""")

# ==================== TRANSFERT COMMANDS ====================

@client.on(events.NewMessage(pattern='/transfert'))
async def cmd_transfert(event):
    if event.is_group or event.is_channel:
        return
    global transfer_enabled
    transfer_enabled = True
    await event.respond("✅ Transfert ON")

@client.on(events.NewMessage(pattern='/stoptransfert'))
async def cmd_stop_transfert(event):
    if event.is_group or event.is_channel:
        return
    global transfer_enabled
    transfer_enabled = False
    await event.respond("⛔ Transfert OFF")

# ==================== WEB SERVER ====================

async def index(request):
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot Baccarat v3.3</title>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial; margin: 40px; background: #1a1a2e; color: #eee; }}
            h1 {{ color: #00d4ff; }}
            .status {{ background: #16213e; padding: 20px; border-radius: 10px; margin: 20px 0; }}
        </style>
    </head>
    <body>
        <h1>📡 Bot Baccarat v3.3</h1>
        <div class="status">
            <div><strong>Jeu:</strong> #{current_game_number}</div>
            <div><strong>Décalage:</strong> +{prediction_offset}</div>
            <div><strong>Actives:</strong> {len(pending_predictions)}</div>
            <div><strong>Règle:</strong> ≥1 carte dans 1er groupe</div>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server: 0.0.0.0:{PORT}")

async def start_bot():
    global source_channel_ok, prediction_channel_ok
    try:
        logger.info("🚀 Démarrage v3.3...")
        logger.info("🎯 Règle: ≥1 carte dans le premier groupe")
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot connecté")
        
        me = await client.get_me()
        logger.info(f"Bot: @{getattr(me, 'username', 'Unknown')}")
        
        try:
            source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info(f"✅ Source: {getattr(source_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Source: {e}")
        
        try:
            pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            try:
                test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🤖 v3.3 connecté!")
                await asyncio.sleep(1)
                await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
                prediction_channel_ok = True
                logger.info(f"✅ Prédiction: {getattr(pred_entity, 'title', 'N/A')}")
            except Exception as e:
                logger.warning(f"⚠️ Prédiction lecture seule: {e}")
        except Exception as e:
            logger.error(f"❌ Prédiction: {e}")
        
        logger.info(f"⚙️ OFFSET=+{prediction_offset}")
        return True
        
    except Exception as e:
        logger.error(f"Erreur: {e}")
        return False

async def main():
    try:
        await start_web_server()
        success = await start_bot()
        if not success:
            return
        logger.info("🤖 Bot opérationnel!")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Erreur: {e}")
    finally:
        await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêt")
    except Exception as e:
        logger.error(f"Fatal: {e}")
