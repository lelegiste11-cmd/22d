"""
Bot Telegram de prédiction Baccarat - Version 4.0 PROTECTED
Prédiction séquentielle : une seule prédiction active à la fois
PROTECTION: Redémarrage automatique en cas d'arrêt (même à minuit)
"""
import os
import asyncio
import re
import logging
import sys
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
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
    """Configure la protection contre les arrêts automatiques"""
    
    def ignore_shutdown(signum, frame):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] 🛡️  SIGNAL D'ARRÊT REÇU (signum={signum}) - IGNORÉ PAR PROTECTION")
        # Ne fait rien = empêche l'arrêt
        return
    
    # Intercepte TOUS les signaux d'arrêt
    signal.signal(signal.SIGTERM, ignore_shutdown)
    signal.signal(signal.SIGINT, ignore_shutdown)
    signal.signal(signal.SIGHUP, ignore_shutdown)
    
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🛡️  PROTECTION ANTI-ARRÊT ACTIVÉE")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🛡️  Signaux SIGTERM, SIGINT, SIGHUP interceptés")

setup_protection()
# ================================================

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

MAX_PENDING_PREDICTIONS = 1  # MODIFIÉ: Une seule prédiction à la fois
PROXIMITY_THRESHOLD = 2

source_channel_ok = False
prediction_channel_ok = False

# ============ VARIABLES GLOBALES ============
transfer_enabled = True

def has_active_unresolved_predictions() -> bool:
    """
    Vérifie s'il y a des prédictions actives non finalisées.
    Retourne True si une prédiction est en cours (⏳ EN COURS).
    """
    for game_num, pred in pending_predictions.items():
        # Si la prédiction n'est pas résolue (pas de statut final)
        if not pred.get('resolved', False):
            return True
    return False

def get_active_prediction_count() -> int:
    """Retourne le nombre de prédictions actives non résolues"""
    count = 0
    for game_num, pred in pending_predictions.items():
        if not pred.get('resolved', False):
            count += 1
    return count

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

def extract_first_card_suit(group_str: str):
    """Extrait la couleur de la première carte du groupe"""
    try:
        normalized = normalize_suits(group_str)
        for char in normalized:
            if char in ALL_SUITS:
                return SUIT_DISPLAY.get(char, char)
    except Exception as e:
        logger.error(f"Erreur extraction carte: {e}")
    return None

def get_suit_full_name(suit_symbol: str) -> str:
    """Retourne le nom complet de la couleur"""
    return SUIT_NAMES.get(suit_symbol, suit_symbol)

def get_alternate_suit(suit: str) -> str:
    """Retourne la couleur alternative (pour backup)"""
    return SUIT_MAPPING.get(suit, suit)

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé (contient ✅ ou 🔰)"""
    try:
        if '⏰' in message:
            return False
        return '✅' in message or '🔰' in message
    except Exception as e:
        logger.error(f"Erreur vérification finalisation: {e}")
        return False

def format_prediction_message(game_number: int, suit: str, status: str = "⏳ EN COURS", result_group: str = None) -> str:
    """
    Formate le message de prédiction:
    📡 PRÉDICTION #74
    🎯 Couleur: ❤️ Cœur
    🌪️ Statut: ⏳ EN COURS / ✅0️⃣ / ✅1️⃣ / ✅2️⃣ / ✅3️⃣ / ❌
    """
    try:
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
    except Exception as e:
        logger.error(f"Erreur format message: {e}")
        return f"Erreur formatage #{game_number}"

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
            'check_count': 0,  # 0=N (prédit), 1=N+1 (1er rattrapage), 2=N+2 (2ème), 3=N+3 (3ème)
            'last_checked_game': 0,
            'created_at': datetime.now().isoformat(),
            'resolved': False  # NOUVEAU: indique si la prédiction est déjà résolue
        }

        logger.info(f"Prédiction active créée: Jeu #{target_game} - {suit} (basé sur #{base_game})")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
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
        pred['resolved'] = True  # Marquer comme résolue
        logger.info(f"Prédiction #{game_number} statut mis à jour: {new_status}")

        # Supprimer des prédictions actives si terminée
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣', '❌']:
            if game_number in pending_predictions:
                del pending_predictions[game_number]
                logger.info(f"Prédiction #{game_number} terminée et supprimée")
                logger.info(f"📋 Prédictions restantes: {len(pending_predictions)}")

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """
    Vérifie si une prédiction est gagnée ou perdue.
    Condition: AU MOINS 1 carte de la couleur dans le premier groupe
    Vérification sur: N (prédit), N+1 (1er rattrapage), N+2 (2ème), N+3 (3ème)
    
    NOUVEAU: Crée une nouvelle prédiction uniquement après finalisation complète
    """
    try:
        normalized_group = normalize_suits(first_group)
        
        logger.info(f"=== VÉRIFICATION RÉSULTAT ===")
        logger.info(f"Message finalisé reçu: Jeu #{game_number}")
        logger.info(f"Premier groupe analysé: ({first_group})")
        logger.info(f"Prédictions en attente: {list(pending_predictions.keys())}")
        
        # CRUCIAL: Créer une copie pour éviter les problèmes de modification pendant l'itération
        predictions_to_check = list(pending_predictions.items())
        
        for pred_game, pred in predictions_to_check:
            try:
                # Vérifier si la prédiction existe toujours et n'est pas déjà résolue
                if pred_game not in pending_predictions:
                    continue
                
                # Si déjà résolue, ignorer
                if pred.get('resolved', False):
                    logger.info(f"  ⏭️ Prédiction #{pred_game} déjà résolue, ignorée")
                    continue
                    
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
                # CONDITION: AU MOINS 1 carte de la couleur
                suit_count = normalized_group.count(normalized_target)
                has_card = suit_count >= 1  # AU MOINS 1 carte suffit !
                
                logger.info(f"  🔍 VÉRIFICATION #{pred_game}: {target_suit} trouvé {suit_count} fois (condition: ≥1)")
                
                if has_card:
                    # GAGNÉ ! Finaliser immédiatement avec le bon statut
                    status_map = {0: '✅0️⃣', 1: '✅1️⃣', 2: '✅2️⃣', 3: '✅3️⃣'}
                    new_status = status_map.get(check_count, '✅0️⃣')
                    
                    await update_prediction_status(pred_game, new_status, first_group)
                    logger.info(f"  🎉 PRÉDICTION #{pred_game} GAGNÉE! {suit_count}x {target_suit} trouvé | Statut: {new_status}")
                    
                    # NOUVEAU: Continuation après victoire
                    # On crée une nouvelle prédiction basée sur le jeu actuel (game_number)
                    try:
                        new_target_game = game_number + prediction_offset
                        new_suit = extract_first_card_suit(first_group)
                        
                        if new_suit:
                            logger.info(f"🔄 CONTINUATION APRÈS VICTOIRE: Préparation prédiction #{new_target_game}")
                            if new_target_game not in pending_predictions and new_target_game not in queued_predictions:
                                await create_prediction(new_target_game, new_suit, game_number, is_continuation=True)
                                logger.info(f"   ✨ NOUVELLE PRÉDICTION #{new_target_game} - {new_suit} (continuation après victoire)")
                            else:
                                logger.info(f"   ⏭️ Prédiction #{new_target_game} existe déjà")
                        else:
                            logger.warning(f"   ⚠️ Impossible d'extraire couleur pour continuation")
                    except Exception as e:
                        logger.error(f"   ❌ Erreur continuation victoire: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                    
                else:
                    # PAS trouvé, passer à l'étape suivante (rattrapage)
                    new_check_count = check_count + 1
                    
                    # Vérifier si la prédiction existe toujours avant de modifier
                    if pred_game not in pending_predictions:
                        continue
                        
                    pending_predictions[pred_game]['check_count'] = new_check_count
                    pending_predictions[pred_game]['last_checked_game'] = game_number
                    
                    # Vérifier si on a épuisé les 3 rattrapages (4 tentatives total: N, N+1, N+2, N+3)
                    if new_check_count > 3:
                        # Échec définitif après N+3 (3ème rattrapage), finaliser comme perdu
                        await update_prediction_status(pred_game, '❌', first_group)
                        logger.info(f"  💔 PRÉDICTION #{pred_game} PERDUE après 3 rattrapages (aucune carte trouvée)")
                        
                        # NOUVEAU: Continuation après défaite
                        # On crée une nouvelle prédiction basée sur le jeu actuel (game_number)
                        try:
                            new_target_game = game_number + prediction_offset
                            new_suit = extract_first_card_suit(first_group)
                            
                            if new_suit:
                                logger.info(f"🔄 CONTINUATION APRÈS DÉFAITE: Préparation prédiction #{new_target_game}")
                                # Vérifier si pas déjà existante
                                if new_target_game not in pending_predictions and new_target_game not in queued_predictions:
                                    await create_prediction(new_target_game, new_suit, game_number, is_continuation=True)
                                    logger.info(f"   ✨ NOUVELLE PRÉDICTION #{new_target_game} - {new_suit} (continuation après défaite)")
                                else:
                                    logger.info(f"   ⏭️ Prédiction #{new_target_game} existe déjà")
                            else:
                                logger.warning(f"   ⚠️ Impossible d'extraire couleur pour continuation")
                        except Exception as e:
                            logger.error(f"   ❌ Erreur continuation défaite: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                    else:
                        # Passer au rattrapage suivant
                        rattrapage_txt = {1: '1er', 2: '2ème', 3: '3ème'}.get(new_check_count, f'{new_check_count}ème')
                        logger.info(f"  ⏳ #{pred_game}: Aucune carte {target_suit}, passage au {rattrapage_txt} rattrapage (vérifiera #{pred_game + new_check_count})")
                        
            except Exception as e:
                logger.error(f"  ❌ Erreur traitement prédiction #{pred_game}: {e}")
                import traceback
                logger.error(traceback.format_exc())
                continue
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Erreur globale check_prediction_result: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def create_prediction(target_game: int, suit: str, base_game: int, is_backup: bool = False, is_continuation: bool = False):
    """
    Crée une nouvelle prédiction UNIQUEMENT si aucune n'est active
    """
    try:
        # VÉRIFICATION CRUCIALE: Ne pas créer si une prédiction est déjà active
        if has_active_unresolved_predictions():
            logger.warning(f"🚫 Impossible de créer prédiction #{target_game}: une prédiction est déjà active")
            logger.info(f"   📋 Prédictions actives: {[g for g, p in pending_predictions.items() if not p.get('resolved', False)]}")
            return False
        
        if target_game in pending_predictions or target_game in queued_predictions:
            logger.info(f"Prédiction #{target_game} déjà existante, ignorée")
            return False
        
        # Envoyer immédiatement la prédiction
        await send_prediction_to_channel(target_game, suit, base_game)
        
        if is_continuation:
            logger.info(f"🔄 Prédiction de continuation créée: #{target_game} après résultat de #{base_game}")
        
        return True
    except Exception as e:
        logger.error(f"Erreur création prédiction: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def process_new_message(message_text: str, chat_id: int, is_finalized: bool = False):
    """
    Traite un nouveau message du canal source.
    - CRÉE les prédictions UNIQUEMENT si aucune n'est active
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
        logger.info(f"   🔍 Prédictions actives non résolues: {get_active_prediction_count()}")
        
        # ========== CRÉATION DE PRÉDICTION (UNIQUEMENT SI AUCUNE ACTIVE) ==========
        try:
            # VÉRIFICATION: Ne créer une prédiction que si aucune n'est en attente de finalisation
            if has_active_unresolved_predictions():
                logger.info(f"   ⏸️ PRÉDICTION BLOQUÉE: Une prédiction est déjà active et non finalisée")
                logger.info(f"   📋 En attente: {list(pending_predictions.keys())}")
                logger.info(f"   ⏳ Attente de la finalisation avant nouvelle prédiction...")
            else:
                # Aucune prédiction active - on peut en créer une nouvelle
                first_card_suit = extract_first_card_suit(first_group)
                
                if first_card_suit:
                    target_game = game_number + prediction_offset
                    
                    if target_game not in pending_predictions and target_game not in queued_predictions:
                        await create_prediction(target_game, first_card_suit, game_number)
                        logger.info(f"   ✨ NOUVELLE PRÉDICTION: #{target_game} - {first_card_suit} (basé sur #{game_number})")
                    else:
                        logger.info(f"   ⏭️ Prédiction #{target_game} existe déjà")
                else:
                    logger.warning(f"   ⚠️ Impossible d'extraire la couleur du premier groupe: ({first_group})")
                    
        except Exception as e:
            logger.error(f"   ❌ Erreur création prédiction: {e}")
            import traceback
            logger.error(traceback.format_exc())
        
        # ========== VÉRIFICATION RÉSULTATS (UNIQUEMENT SI FINALISÉ) ==========
        if is_finalized:
            try:
                # Vérifier si ce message finalise des prédictions en attente
                await check_prediction_result(game_number, first_group)
            except Exception as e:
                logger.error(f"   ❌ Erreur vérification résultat: {e}")
                import traceback
                logger.error(traceback.format_exc())
        else:
            logger.info(f"   ⏳ Message non finalisé, pas de vérification de résultat")
        
        logger.info(f"=" * 60)
        
    except Exception as e:
        logger.error(f"❌ Erreur globale process_new_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def check_channels():
    """Vérifie l'accès aux canaux"""
    global source_channel_ok, prediction_channel_ok
    
    try:
        # Vérifier canal source
        try:
            entity = await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info(f"✅ Canal source accessible: {entity.title} (ID: {SOURCE_CHANNEL_ID})")
        except Exception as e:
            source_channel_ok = False
            logger.error(f"❌ Canal source inaccessible: {e}")
        
        # Vérifier canal de prédiction
        try:
            entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            prediction_channel_ok = True
            logger.info(f"✅ Canal prédiction accessible: {entity.title} (ID: {PREDICTION_CHANNEL_ID})")
        except Exception as e:
            prediction_channel_ok = False
            logger.error(f"❌ Canal prédiction inaccessible: {e}")
            
    except Exception as e:
        logger.error(f"Erreur vérification canaux: {e}")

@client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
async def handle_new_message(event):
    """Gestionnaire de nouveaux messages"""
    try:
        message_text = event.message.text
        chat_id = event.chat_id
        
        if not message_text:
            return
        
        # Vérifier si c'est un message finalisé
        is_finalized = is_message_finalized(message_text)
        
        # Traiter le message
        await process_new_message(message_text, chat_id, is_finalized)
        
    except Exception as e:
        logger.error(f"Erreur handler message: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def health_check(request):
    """Endpoint de vérification de santé pour Render"""
    return web.Response(text="OK", status=200)

async def run_web_server():
    """Lance le serveur web pour keep-alive"""
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Serveur web démarré sur le port {PORT}")

async def main():
    """Fonction principale avec boucle infinie protégée"""
    
    # Démarrer le serveur web (pour Render/keep-alive)
    await run_web_server()
    
    # Connexion Telegram
    await client.start(bot_token=BOT_TOKEN)
    logger.info("🤖 Bot démarré et connecté à Telegram")
    
    # Vérifier les canaux
    await check_channels()
    
    # ============ BOUCLE INFINIE PROTÉGÉE ============
    logger.info("🛡️  ENTRÉE EN MODE PROTECTION 24/7")
    logger.info("🛡️  Le bot ne s'arrêtera JAMAIS (même à minuit)")
    
    restart_count = 0
    
    while True:
        try:
            # Garder le client connecté indéfiniment
            await client.run_until_disconnected()
            
            # Si on arrive ici, c'est que la connexion a été perdue
            restart_count += 1
            logger.warning(f"⚠️  Connexion perdue (redémarrage #{restart_count})")
            logger.info("⏳ Reconnexion dans 5 secondes...")
            
            # Attente avant reconnexion
            await asyncio.sleep(5)
            
            # Tenter de se reconnecter
            if not client.is_connected():
                await client.connect()
                logger.info("🔌 Reconnecté à Telegram")
                
        except Exception as e:
            logger.error(f"❌ Erreur dans la boucle principale: {e}")
            logger.info("⏳ Nouvelle tentative dans 10 secondes...")
            await asyncio.sleep(10)
            continue  # JAMAIS DE BREAK OU EXIT
    
    # Cette ligne ne sera JAMAIS atteinte
    logger.error("❌ SORTIE DE BOUCLE INATTENDUE - Cela ne devrait pas arriver!")

# ============ PROTECTION FINALE ============
if __name__ == "__main__":
    try:
        # Lancer la boucle asyncio avec protection maximale
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Arrêt manuel détecté (Ctrl+C)")
    except Exception as e:
        logger.error(f"💥 ERREUR FATALE: {e}")
        logger.info("🔁 Redémarrage automatique dans 3 secondes...")
        time.sleep(3)
        # Relancer le script
        os.execv(sys.executable, [sys.executable] + sys.argv)
