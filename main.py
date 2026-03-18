import os
import asyncio
import re
import logging
import sys
from datetime import datetime, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY, SUIT_NAMES
)

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Vérification des credentials
if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, PREDICTION_CHANNEL={PREDICTION_CHANNEL_ID}")

# Client Telegram
session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# Variables globales - Mode SÉQUENTIEL: une prédiction à la fois
active_prediction = None  # Une seule prédiction active
last_prediction_result = None  # Résultat de la dernière prédiction (pour décalage après échec)
recent_games = {}
processed_messages = set()
last_transferred_game = None
current_game_number = 0
source_channel_ok = False
prediction_channel_ok = False
transfer_enabled = True

# Décalages
PREDICTION_OFFSET = 2  # Normal: +2
PREDICTION_OFFSET_AFTER_FAIL = 4  # Après échec: +4

def is_odd_number(n: int) -> bool:
    """Vérifie si un nombre est impair"""
    return n % 2 == 1

def get_next_odd_prediction(base: int, after_fail: bool = False) -> int:
    """Retourne le prochain numéro impair"""
    offset = PREDICTION_OFFSET_AFTER_FAIL if after_fail else PREDICTION_OFFSET
    target = base + offset
    if target % 2 == 0:
        target += 1
    return target

# Mapping des opposés
OPPOSITE_SUIT = {
    '♣️': '♠️', '♣': '♠️',
    '♠️': '♣️', '♠': '♣️',
    '❤️': '♦️', '❤': '♦️',
    '♥️': '♦️', '♥': '♦️',
    '♦️': '❤️', '♦': '❤️'
}

# Mapping de normalisation pour la comparaison
SUIT_NORMALIZE = {
    '♠️': 'spade', '♠': 'spade',
    '❤️': 'heart', '❤': 'heart',
    '♥️': 'heart', '♥': 'heart',
    '♦️': 'diamond', '♦': 'diamond',
    '♣️': 'club', '♣': 'club'
}

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message"""
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_parentheses_groups(message: str):
    """Extrait les groupes entre parenthèses"""
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suit_for_comparison(suit: str) -> str:
    """Normalise la couleur pour comparaison fiable"""
    if not suit:
        return ''
    suit = suit.strip()
    return SUIT_NORMALIZE.get(suit, suit.lower())

def get_all_suits_in_group(group_str: str) -> list:
    """Extrait TOUTES les couleurs présentes dans le groupe"""
    if not group_str:
        return []
    
    suits_found = []
    # Cherche tous les patterns de cartes
    pattern = r"[0-9]+|[AJQKajqk][♠♥♦♣❤️️]|[♠♥♦♣❤️️]"
    matches = re.findall(pattern, group_str)
    
    for match in matches:
        for char in match:
            if char in ['♠', '♥', '♦', '♣', '❤', '♠️', '❤️', '♦️', '♣️', '♥️']:
                normalized = normalize_suit_for_comparison(char)
                if normalized and normalized not in suits_found:
                    suits_found.append(normalized)
    
    return suits_found

def get_first_card_suit(group_str: str):
    """Extrait la couleur de la première carte du groupe"""
    match = re.search(r"[0-9AJQKajqk]+([♠♥♦♣❤️️])", group_str)
    if match:
        suit = match.group(1)
        normalized = normalize_suit_for_comparison(suit)
        return normalized
    return None

def get_opposite_suit(suit: str) -> str:
    """Retourne la couleur opposée"""
    # D'abord normaliser si c'est un symbole
    if suit in SUIT_NORMALIZE:
        normalized = SUIT_NORMALIZE[suit]
        opposites = {
            'club': 'spade', 'spade': 'club',
            'heart': 'diamond', 'diamond': 'heart'
        }
        opposite_normalized = opposites.get(normalized, normalized)
        # Retourner le symbole standard
        reverse_map = {'spade': '♠️', 'heart': '❤️', 'diamond': '♦️', 'club': '♣️'}
        return reverse_map.get(opposite_normalized, suit)
    return OPPOSITE_SUIT.get(suit, suit)

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé"""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

def has_suit_in_first_group(message_text: str, target_suit: str) -> bool:
    """Vérifie si la couleur cible est dans le premier groupe de parenthèses"""
    groups = extract_parentheses_groups(message_text)
    if not groups:
        return False
    
    first_group = groups[0]
    target_normalized = normalize_suit_for_comparison(target_suit)
    if not target_normalized:
        return False
    
    suits_in_group = get_all_suits_in_group(first_group)
    
    if target_normalized in suits_in_group:
        logger.info(f"✅ COULEUR TROUVÉE: {target_normalized} dans {suits_in_group}")
        return True
    
    logger.info(f"❌ Couleur NON trouvée: {target_normalized} pas dans {suits_in_group}")
    return False

async def send_prediction(game_number: int, first_card_suit: str):
    """Envoie une prédiction au canal"""
    global active_prediction, last_prediction_result
    
    # Détermine si on utilise le décalage après échec
    after_fail = (last_prediction_result == '❌')
    target_game = get_next_odd_prediction(game_number, after_fail)
    
    # Convertir en symbole d'affichage
    display_suits = {
        'heart': '❤️', 'spade': '♠️', 
        'diamond': '♦️', 'club': '♣️'
    }
    opposite_normalized = normalize_suit_for_comparison(get_opposite_suit(first_card_suit))
    opposite_suit = display_suits.get(opposite_normalized, '❤️')
    suit_name = SUIT_NAMES.get(opposite_suit, opposite_suit)
    
    offset_used = PREDICTION_OFFSET_AFTER_FAIL if after_fail else PREDICTION_OFFSET
    
    prediction_msg = f"""🎰 PRÉDICTION #{target_game}
🎯 Couleur: {opposite_suit} {suit_name}
📊 Statut: ⏳⏳"""

    msg_id = 0
    if PREDICTION_CHANNEL_ID and prediction_channel_ok:
        try:
            pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
            msg_id = pred_msg.id
            logger.info(f"✅ PRÉDICTION ENVOYÉE: #{target_game} - {opposite_suit} (décalage: +{offset_used})")
        except Exception as e:
            logger.error(f"❌ Erreur envoi prédiction: {e}")
    else:
        logger.warning(f"⚠️ Canal de prédiction non accessible")

    active_prediction = {
        'game_number': target_game,
        'message_id': msg_id,
        'predicted_suit': opposite_normalized,
        'predicted_suit_display': opposite_suit,
        'base_game': game_number,
        'first_card_suit': first_card_suit,
        'status': '⏳⏳',
        'check_count': 0,
        'created_at': datetime.now().isoformat()
    }
    
    # Réinitialise le résultat précédent
    last_prediction_result = None
    
    return msg_id

async def update_prediction_status(game_number: int, status_code: str, status_emoji: str):
    """Met à jour le statut de la prédiction et libère pour la suivante"""
    global active_prediction, last_prediction_result
    
    if active_prediction is None or active_prediction['game_number'] != game_number:
        return False

    try:
        pred = active_prediction
        message_id = pred['message_id']
        suit_display = pred.get('predicted_suit_display', '❤️')
        suit_name = SUIT_NAMES.get(suit_display, suit_display)

        updated_msg = f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit_display} {suit_name}
🌪️ Statut: {status_emoji}"""

        if PREDICTION_CHANNEL_ID and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {status_emoji}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour: {e}")

        pred['status'] = status_code
        last_prediction_result = status_code  # Mémorise le résultat
        
        logger.info(f"Prédiction #{game_number} statut: {status_code}")
        
        # Libère la prédiction active SEULEMENT si terminée (✅ ou ❌)
        if status_code in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
            logger.info(f"🏁 Prédiction #{game_number} TERMINÉE ({status_code}) - Prêt pour nouvelle prédiction")
            active_prediction = None  # Libère pour la prochaine prédiction

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

async def check_prediction_result(game_number: int, message_text: str):
    """Vérifie le résultat de la prédiction active"""
    global active_prediction
    
    if active_prediction is None:
        return None
        
    pred = active_prediction
    target_game = pred['game_number']
    predicted_suit = pred['predicted_suit']
    check_count = pred.get('check_count', 0)
    
    # Vérification au numéro exact
    if game_number == target_game and check_count == 0:
        if has_suit_in_first_group(message_text, predicted_suit):
            await update_prediction_status(target_game, '✅0️⃣', '🍯✅0️⃣')
            return 'success'
        else:
            pred['check_count'] = 1
            logger.info(f"🔍 #{target_game}: {predicted_suit} non trouvé, attente #{target_game+1}")
            return 'continue'
    
    # Vérification au numéro + 1
    elif game_number == target_game + 1 and check_count == 1:
        if has_suit_in_first_group(message_text, predicted_suit):
            await update_prediction_status(target_game, '✅1️⃣', '🍯✅1️⃣')
            return 'success'
        else:
            pred['check_count'] = 2
            logger.info(f"🔍 #{target_game}: {predicted_suit} non trouvé au +1, attente #{target_game+2}")
            return 'continue'
    
    # Vérification au numéro + 2
    elif game_number == target_game + 2 and check_count == 2:
        if has_suit_in_first_group(message_text, predicted_suit):
            await update_prediction_status(target_game, '✅2️⃣', '🍯✅2️⃣')
            return 'success'
        else:
            await update_prediction_status(target_game, '❌', '❌')
            return 'fail'
    
    # Si on dépasse le numéro + 2
    elif game_number > target_game + 2:
        await update_prediction_status(target_game, '❌', '❌')
        return 'fail'
    
    return None

async def process_new_message(message_text: str, chat_id: int, is_finalized: bool = False):
    """Traite un nouveau message - Mode SÉQUENTIEL"""
    global last_transferred_game, current_game_number
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        current_game_number = game_number

        # Évite le traitement en double
        message_hash = f"{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)
        if len(processed_messages) > 200:
            processed_messages.clear()

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 2:
            return

        first_group = groups[0]
        second_group = groups[1]

        logger.info(f"📥 Jeu #{game_number} reçu - G1: {first_group}, G2: {second_group}")

        # Transfert des messages finalisés à l'admin
        if is_finalized and transfer_enabled and ADMIN_ID and last_transferred_game != game_number:
            try:
                transfer_msg = f"📨 **Message finalisé:**\n\n{message_text}"
                await client.send_message(ADMIN_ID, transfer_msg)
                last_transferred_game = game_number
            except Exception as e:
                logger.error(f"❌ Erreur transfert: {e}")

        # VÉRIFICATION: Vérifie la prédiction active (si existe)
        if active_prediction is not None:
            result = await check_prediction_result(game_number, message_text)
            logger.info(f"Résultat vérification: {result}")

        # NOUVELLE PRÉDICTION: Uniquement si pas de prédiction active ET numéro impair
        if active_prediction is None and is_odd_number(game_number):
            second_group_clean = second_group.strip()
            if second_group_clean and second_group_clean != '0':
                first_card_suit = get_first_card_suit(second_group)
                if first_card_suit:
                    opposite = get_opposite_suit(first_card_suit)
                    logger.info(f"🎯 NOUVELLE PRÉDICTION depuis #{game_number}: {opposite}")
                    await send_prediction(game_number, first_card_suit)
                else:
                    logger.warning(f"⚠️ Pas de couleur trouvée dans le 2ème groupe: {second_group}")
            else:
                logger.info(f"⏭️ Jeu #{game_number}: 2ème groupe vide, pas de prédiction")

        # Stockage des données
        recent_games[game_number] = {
            'first_group': first_group,
            'second_group': second_group,
            'timestamp': datetime.now().isoformat()
        }
        
        if len(recent_games) > 100:
            oldest = min(recent_games.keys())
            del recent_games[oldest]

    except Exception as e:
        logger.error(f"Erreur traitement message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# Handlers Telegram
@client.on(events.NewMessage())
async def handle_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id

        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"Message reçu: {message_text[:80]}...")
            
            is_finalized = is_message_finalized(message_text)
            await process_new_message(message_text, chat_id, is_finalized)

    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id

        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"Message édité: {message_text[:80]}...")
            
            is_finalized = is_message_finalized(message_text)
            await process_new_message(message_text, chat_id, is_finalized)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# Commandes
@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond("""🤖 **Bot de Prédiction Baccarat - MODE SÉQUENTIEL**

**Règles:**
• **Une prédiction à la fois**
• Attend la **vérification complète** avant nouvelle prédiction
• Prédit uniquement sur numéros **impairs**
• Décalage: +2 (normal) ou +4 (après échec)

**Séquence:**
Prédit #53 → Attend vérification → ✅0️⃣/✅1️⃣/✅2️⃣/❌ → Puis prédit #55 (ou #57 si échec)
    
**Commandes:**
• `/status` - Voir l'état de la prédiction
• `/help` - Aide détaillée
• `/debug` - Informations de débogage
• `/checkchannels` - Vérifier l'accès aux canaux""")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return

    if event.sender_id != ADMIN_ID:
        await event.respond("Commande réservée à l'administrateur")
        return

    status_msg = f"📊 **État des prédictions:**\n\n"
    status_msg += f"🎮 Jeu actuel: #{current_game_number}\n"
    status_msg += f"📐 Décalage normal: +{PREDICTION_OFFSET}\n"
    status_msg += f"📐 Après échec: +{PREDICTION_OFFSET_AFTER_FAIL}\n"
    status_msg += f"🏁 Dernier résultat: {last_prediction_result or 'Aucun'}\n\n"

    if active_prediction:
        pred = active_prediction
        distance = pred['game_number'] - current_game_number
        status_msg += f"**🔮 Prédiction active:**\n"
        status_msg += f"• Jeu cible: #{pred['game_number']}\n"
        status_msg += f"• Couleur: {pred.get('predicted_suit_display', pred['predicted_suit'])}\n"
        status_msg += f"• Statut: {pred['status']}\n"
        status_msg += f"• Vérifications: {pred.get('check_count', 0)}/3\n"
        status_msg += f"• Distance: {distance} jeux\n\n"
        status_msg += "⏳ **En attente de vérification...**"
    else:
        status_msg += "**✅ Prêt pour nouvelle prédiction**"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/debug'))
async def cmd_debug(event):
    if event.is_group or event.is_channel:
        return

    debug_msg = f"""🔍 **Informations de débogage:**

**Configuration:**
• Source Channel: {SOURCE_CHANNEL_ID}
• Prediction Channel: {PREDICTION_CHANNEL_ID}
• Admin ID: {ADMIN_ID}
• Décalage normal: +{PREDICTION_OFFSET}
• Décalage après échec: +{PREDICTION_OFFSET_AFTER_FAIL}

**État:**
• Jeu actuel: #{current_game_number}
• Prédiction active: {'Oui' if active_prediction else 'Non'}
• Dernier résultat: {last_prediction_result or 'Aucun'}
• Port: {PORT}

**Mode SÉQUENTIEL:**
• Une prédiction à la fois
• Attend fin de vérification (✅ ou ❌)
• Puis prédit le prochain impair
"""
    await event.respond(debug_msg)

@client.on(events.NewMessage(pattern='/checkchannels'))
async def cmd_checkchannels(event):
    global source_channel_ok, prediction_channel_ok

    if event.is_group or event.is_channel:
        return

    await event.respond("🔍 Vérification des accès aux canaux...")

    result_msg = "📡 **Résultat de la vérification:**\n\n"

    try:
        source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
        source_title = getattr(source_entity, 'title', 'N/A')
        source_channel_ok = True
        result_msg += f"✅ **Canal source** ({SOURCE_CHANNEL_ID}):\n"
        result_msg += f"   Nom: {source_title}\n\n"
    except Exception as e:
        source_channel_ok = False
        result_msg += f"❌ **Canal source**: {str(e)[:100]}\n\n"

    try:
        pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
        pred_title = getattr(pred_entity, 'title', 'N/A')
        
        test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🔍 Test de connexion...")
        await asyncio.sleep(1)
        await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
        prediction_channel_ok = True
        result_msg += f"✅ **Canal prédiction** ({PREDICTION_CHANNEL_ID}):\n"
        result_msg += f"   Nom: {pred_title}\n"
        result_msg += f"   Écriture: OK"
    except Exception as e:
        prediction_channel_ok = False
        result_msg += f"❌ **Canal prédiction**: {str(e)[:100]}"

    await event.respond(result_msg)

@client.on(events.NewMessage(pattern='/transfert'))
async def cmd_transfert(event):
    global transfer_enabled
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID:
        await event.respond("Commande réservée à l'administrateur")
        return
    transfer_enabled = True
    await event.respond("✅ Transfert activé!")

@client.on(events.NewMessage(pattern='/stoptransfert'))
async def cmd_stoptransfert(event):
    global transfer_enabled
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID:
        await event.respond("Commande réservée à l'administrateur")
        return
    transfer_enabled = False
    await event.respond("⛔ Transfert désactivé.")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(f"""📖 **Aide - Bot de Prédiction (Mode Séquentiel)**

**Logique de prédiction SÉQUENTIELLE:**

Le bot prédit **une seule prédiction à la fois** et attend la vérification complète.

**Exemple de flux:**
1. 
Jeu #51 (impair) avec 2ème groupe  → 🎰 PRÉDICTION #53
2. 
Attend les jeux #53, #54, #55 pour vérification
 
#53: vérifie G1 → si couleur trouvée → ✅0️⃣
 
#54: si pas trouvé → vérifie → ✅1️⃣
 
#55: si pas trouvé → vérifie → ✅2️⃣ ou ❌
3. 
Une fois statut mis à jour (✅ ou ❌)  → Nouvelle prédiction depuis le prochain impair disponible
4. 
Si résultat était ❌ (échec)  → Décalage +4 au lieu de +2  → Ex: #55 échoue → prédit #59 (pas #57)
    
**Règles:**
• Prédit uniquement sur jeux impairs (51, 53, 55, 57, 59...)
• Attend fin de vérification avant nouvelle prédiction
• Décalage normal: +2
• Décalage après échec: +4

**Commandes:**
• `/start` - Démarrer
• `/status` - Voir l'état de la prédiction active
• `/transfert` - Activer transfert
• `/stoptransfert` - Désactiver transfert
• `/checkchannels` - Vérifier canaux
• `/debug` - Infos système""")

# Serveur Web
async def index(request):
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot Prédiction Baccarat</title>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; background: #1a1a2e; color: #eee; }}
            h1 {{ color: #00d9ff; }}
            .status {{ background: #16213e; padding: 20px; border-radius: 10px; margin: 20px 0; }}
            .active {{ color: #00ff88; }}
            .waiting {{ color: #ffaa00; }}
            .inactive {{ color: #ff6b6b; }}
            a {{ color: #00d9ff; }}
        </style>
    </head>
    <body>
        <h1>🎯 Bot de Prédiction Baccarat (Mode Séquentiel)</h1>
        <div class="status">
            <h2>Statut du Bot</h2>
            <p><strong>🎮 Jeu actuel:</strong> #{current_game_number}</p>
            <p><strong>🔮 État:</strong> <span class="{'active' if active_prediction else 'inactive'}">{'🟡 En cours...' if active_prediction else '🟢 Prêt'}</span></p>
            <p><strong>📐 Décalage:</strong> +{PREDICTION_OFFSET} (normal) / +{PREDICTION_OFFSET_AFTER_FAIL} (après échec)</p>
            <p><strong>🏁 Dernier résultat:</strong> {last_prediction_result or 'Aucun'}</p>
            <p><strong>📡 Canal Source:</strong> {'✅ Connecté' if source_channel_ok else '❌ Non connecté'}</p>
            <p><strong>🎯 Canal Prédiction:</strong> {'✅ Connecté' if prediction_channel_ok else '❌ Non connecté'}</p>
        </div>
        <ul>
            <li><a href="/health">Health Check</a></li>
            <li><a href="/status">Statut (JSON)</a></li>
        </ul>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def status_api(request):
    status_data = {
        "status": "running",
        "source_channel": SOURCE_CHANNEL_ID,
        "source_channel_ok": source_channel_ok,
        "prediction_channel": PREDICTION_CHANNEL_ID,
        "prediction_channel_ok": prediction_channel_ok,
        "current_game": current_game_number,
        "prediction_active": active_prediction is not None,
        "prediction_data": {
            "game_number": active_prediction['game_number'] if active_prediction else None,
            "suit": active_prediction.get('predicted_suit_display', active_prediction['predicted_suit']) if active_prediction else None,
            "status": active_prediction['status'] if active_prediction else None,
            "check_count": active_prediction.get('check_count', 0) if active_prediction else 0
        } if active_prediction else None,
        "last_result": last_prediction_result,
        "prediction_offset": PREDICTION_OFFSET,
        "prediction_offset_after_fail": PREDICTION_OFFSET_AFTER_FAIL,
        "timestamp": datetime.now().isoformat()
    }
    return web.json_response(status_data)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', status_api)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"✅ Serveur web démarré sur 0.0.0.0:{PORT}")

async def start_bot():
    global source_channel_ok, prediction_channel_ok
    try:
        logger.info("🚀 Démarrage du bot (Mode Séquentiel)...")
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot Telegram connecté")

        session = client.session.save()
        if session:
            logger.info(f"🔑 Session: {session[:50]}...")

        me = await client.get_me()
        username = getattr(me, 'username', 'Unknown')
        logger.info(f"🤖 Bot opérationnel: @{username}")

        logger.info("🔍 Vérification des canaux...")
        
        try:
            source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info(f"✅ Canal source: {getattr(source_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Canal source inaccessible: {e}")

        try:
            pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🤖 Bot séquentiel connecté!")
            await asyncio.sleep(1)
            await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
            prediction_channel_ok = True
            logger.info(f"✅ Canal prédiction: {getattr(pred_entity, 'title', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Canal prédiction inaccessible: {e}")

        logger.info(f"📐 Mode SÉQUENTIEL: +{PREDICTION_OFFSET}/+{PREDICTION_OFFSET_AFTER_FAIL}")
        logger.info("⏳ Une prédiction à la fois, attend vérification complète")
        logger.info("👀 Surveillance active du canal source...")

        return True
    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def main():
    try:
        await start_web_server()
        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage, arrêt du bot")
            return
        
        logger.info("🎉 Bot séquentiel opérationnel!")
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"❌ Erreur: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"❌ Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
