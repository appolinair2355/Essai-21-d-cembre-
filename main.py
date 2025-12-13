import os
import asyncio
import re
import logging
import sys
import json
import zipfile
import io
from datetime import datetime, timedelta, timezone, time
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_1_ID, SOURCE_CHANNEL_2_ID, PREDICTION_CHANNEL_ID, PORT,
    PREDICTION_RULES_MORNING, PREDICTION_RULES_AFTERNOON, PREDICTION_RULES_EVENING,
    VERIFICATION_EMOJIS, ALL_SUITS, SUIT_DISPLAY,
    DEFAULT_K, DEFAULT_A, DEFAULT_R, DEFAULT_ECART, MAX_GAME_NUMBER
)

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

logger.info(f"Configuration: SOURCE_1={SOURCE_CHANNEL_1_ID}, SOURCE_2={SOURCE_CHANNEL_2_ID}, PREDICTION={PREDICTION_CHANNEL_ID}")

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# Timezone WAT (West Africa Time, UTC+1) - même fuseau que le Bénin
WAT_TZ = timezone(timedelta(hours=1))

# Variables globales d'état
pending_predictions = {}
processed_messages = set()
current_game_number = 0
last_predicted_game = 0

# Paramètres configurables
k_position = DEFAULT_K          # Position de la carte à utiliser (1, 2, 3...)
a_offset = DEFAULT_A            # Offset pour la prédiction (N+a)
r_offset = DEFAULT_R            # Nombre d'essais de vérification (0 à 10)
ecart_list = []                 # Liste des écarts personnalisés
ecart_index = 0                 # Index actuel dans la liste des écarts
intelligent_mode = False        # Mode intelligent (règle inter): prédit la carte exacte à position k
admin_notifications = True      # Envoyer les notifications au chat privé admin

# Flags d'état des canaux
source_channel_1_ok = False
source_channel_2_ok = False
prediction_channel_ok = False
transfer_enabled = True

# Fichier de configuration persistante
CONFIG_FILE = 'bot_config.json'

def save_config():
    """Sauvegarde la configuration dans un fichier JSON."""
    config = {
        'k_position': k_position,
        'a_offset': a_offset,
        'r_offset': r_offset,
        'ecart_list': ecart_list,
        'ecart_index': ecart_index,
        'last_predicted_game': last_predicted_game,
        'intelligent_mode': intelligent_mode,
        'admin_notifications': admin_notifications
    }
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f)
        logger.info("Configuration sauvegardée")
    except Exception as e:
        logger.error(f"Erreur sauvegarde config: {e}")

def load_config():
    """Charge la configuration depuis un fichier JSON."""
    global k_position, a_offset, r_offset, ecart_list, ecart_index, last_predicted_game, intelligent_mode, admin_notifications
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
            k_position = config.get('k_position', DEFAULT_K)
            a_offset = config.get('a_offset', DEFAULT_A)
            r_offset = config.get('r_offset', DEFAULT_R)
            ecart_list = config.get('ecart_list', [])
            ecart_index = config.get('ecart_index', 0)
            last_predicted_game = config.get('last_predicted_game', 0)
            intelligent_mode = config.get('intelligent_mode', False)
            admin_notifications = config.get('admin_notifications', True)
            mode_str = "intelligent" if intelligent_mode else "statique"
            logger.info(f"Configuration chargée: k={k_position}, a={a_offset}, r={r_offset}, ecarts={ecart_list}, mode={mode_str}, notifications={'on' if admin_notifications else 'off'}")
    except Exception as e:
        logger.error(f"Erreur chargement config: {e}")

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message."""
    match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_parentheses_groups(message: str):
    """Extrait le contenu entre parenthèses."""
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suits(group_str: str) -> str:
    """Remplace les différentes variantes de symboles par un format unique."""
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def get_suits_in_group(group_str: str):
    """Liste toutes les couleurs présentes dans une chaîne."""
    normalized = normalize_suits(group_str)
    return [s for s in ALL_SUITS if s in normalized]

def get_suit_at_position(group_str: str, position: int) -> str:
    """
    Extrait la couleur à la position k dans le premier groupe.
    Position commence à 1.
    Exemple: "10♦️5♥️J♣️" avec position=1 retourne ♦, position=2 retourne ♥, position=3 retourne ♣
    """
    normalized = normalize_suits(group_str)
    suits_found = []
    for char in normalized:
        if char in ALL_SUITS:
            suits_found.append(char)
    
    if position <= 0 or position > len(suits_found):
        return None
    
    return suits_found[position - 1]

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    """Vérifie si la couleur cible est présente dans le groupe."""
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False

def get_current_time_slot():
    """
    Détermine la plage horaire actuelle selon l'heure béninoise (WAT).
    Retourne: 'morning' (00h-12h), 'afternoon' (13h-19h), 'evening' (19h01-23h59)
    """
    now = datetime.now(WAT_TZ)
    hour = now.hour
    minute = now.minute
    
    if hour >= 0 and hour <= 12:
        return 'morning'
    elif hour >= 13 and hour <= 19 and minute == 0:
        return 'afternoon'
    elif hour >= 13 and hour < 19:
        return 'afternoon'
    elif hour == 19 and minute >= 1:
        return 'evening'
    elif hour > 19:
        return 'evening'
    else:
        return 'afternoon'

def get_prediction_rules():
    """Retourne les règles de prédiction selon la plage horaire actuelle."""
    time_slot = get_current_time_slot()
    if time_slot == 'morning':
        return PREDICTION_RULES_MORNING
    elif time_slot == 'afternoon':
        return PREDICTION_RULES_AFTERNOON
    else:
        return PREDICTION_RULES_EVENING

def predict_suit(source_suit: str) -> str:
    """
    Applique les règles de prédiction selon l'heure béninoise.
    Prend la couleur source à la position k et retourne la couleur prédite.
    """
    rules = get_prediction_rules()
    normalized = normalize_suits(source_suit)
    for suit in ALL_SUITS:
        if suit in normalized:
            return rules.get(suit, suit)
    return source_suit

def get_current_ecart():
    """Retourne l'écart actuel selon la liste des écarts ou l'écart par défaut."""
    global ecart_index
    if not ecart_list:
        return DEFAULT_ECART
    
    if ecart_index >= len(ecart_list):
        ecart_index = 0
    
    return ecart_list[ecart_index]

def advance_ecart():
    """Avance à l'écart suivant dans la liste."""
    global ecart_index
    if ecart_list:
        ecart_index = (ecart_index + 1) % len(ecart_list)
        save_config()

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est un résultat final (non en cours)."""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

def can_predict_game(game_number: int) -> bool:
    """
    Vérifie si on peut prédire pour ce numéro de jeu.
    Évite les prédictions pour des numéros consécutifs (écart minimum).
    """
    global last_predicted_game
    ecart = get_current_ecart()
    
    if last_predicted_game == 0:
        return True
    
    if game_number >= last_predicted_game + ecart:
        return True
    
    return False

async def send_prediction_to_channel(target_game: int, predicted_suit: str):
    """Envoie la prédiction au canal de prédiction."""
    global last_predicted_game
    try:
        suit_display = SUIT_DISPLAY.get(predicted_suit, predicted_suit)
        prediction_msg = f"🔵{target_game}🔵:{suit_display} statut :⏳"
        
        msg_id = 0
        
        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction #{target_game} envoyée au canal (msg_id: {msg_id})")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction #{target_game}: {e}")
        else:
            logger.warning(f"⚠️ Canal de prédiction non accessible")
        
        pending_predictions[target_game] = {
            'message_id': msg_id,
            'suit': predicted_suit,
            'suit_display': suit_display,
            'status': '⏳',
            'check_count': 0,
            'max_checks': r_offset + 1,
            'created_at': datetime.now().isoformat()
        }
        
        last_predicted_game = target_game
        advance_ecart()
        save_config()
        
        logger.info(f"Prédiction active: Jeu #{target_game} - {suit_display}")
        return msg_id
        
    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

async def update_prediction_status(game_number: int, new_status: str):
    """Met à jour le message de prédiction dans le canal."""
    try:
        if game_number not in pending_predictions:
            return False
        
        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        suit_display = pred['suit_display']
        
        updated_msg = f"🔵{game_number}🔵:{suit_display} statut :{new_status}"
        
        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {new_status}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour: {e}")
        
        pred['status'] = new_status
        
        if new_status.startswith('✅') or new_status == '❌':
            del pending_predictions[game_number]
            logger.info(f"Prédiction #{game_number} terminée")
        
        return True
        
    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """
    Vérifie les résultats des prédictions actives.
    Utilise le système d'offset r pour vérifier sur plusieurs jeux consécutifs.
    """
    predictions_to_check = list(pending_predictions.keys())
    
    if not predictions_to_check:
        logger.debug(f"Aucune prédiction en attente pour le jeu #{game_number}")
        return
    
    logger.info(f"Vérification du jeu #{game_number} - Prédictions actives: {predictions_to_check}")
    
    for pred_game in predictions_to_check:
        if pred_game not in pending_predictions:
            continue
            
        pred = pending_predictions[pred_game]
        target_suit = pred['suit']
        suit_display = pred.get('suit_display', target_suit)
        check_count = pred.get('check_count', 0)
        max_checks = pred.get('max_checks', r_offset + 1)
        
        expected_game = pred_game + check_count
        
        logger.debug(f"Prédiction #{pred_game}: attend jeu #{expected_game} (N+{check_count}), reçu #{game_number}")
        
        if game_number == expected_game:
            logger.info(f"Match trouvé! Vérification de {suit_display} dans '{first_group}'")
            if has_suit_in_group(first_group, target_suit):
                success_emoji = VERIFICATION_EMOJIS.get(check_count, f"✅{check_count}️⃣")
                await update_prediction_status(pred_game, success_emoji)
                logger.info(f"✅ Prédiction #{pred_game} réussie à N+{check_count} - Statut: {success_emoji}")
            else:
                pred['check_count'] = check_count + 1
                
                if pred['check_count'] >= max_checks:
                    await update_prediction_status(pred_game, '❌')
                    logger.info(f"❌ Prédiction #{pred_game} échouée après {max_checks} vérifications")
                else:
                    logger.info(f"⏳ Prédiction #{pred_game}: vérification {check_count + 1}/{max_checks}, attente N+{check_count + 1}")

async def process_source_1_message(message_text: str, chat_id: int):
    """
    Traite les messages du canal source 1 (pour les prédictions).
    Extrait la carte à la position k et génère la prédiction.
    """
    global current_game_number
    
    try:
        if not is_message_finalized(message_text):
            return
        
        game_number = extract_game_number(message_text)
        if game_number is None:
            return
        
        current_game_number = game_number
        
        message_hash = f"src1_{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)
        
        if len(processed_messages) > 500:
            processed_messages.clear()
        
        groups = extract_parentheses_groups(message_text)
        if len(groups) < 1:
            return
        
        first_group = groups[0]
        
        source_suit = get_suit_at_position(first_group, k_position)
        if source_suit is None:
            logger.warning(f"Impossible de trouver une carte à la position {k_position} dans {first_group}")
            return
        
        if intelligent_mode:
            predicted_suit = source_suit
        else:
            predicted_suit = predict_suit(source_suit)
        
        target_game = game_number + a_offset
        
        if not can_predict_game(target_game):
            logger.info(f"Jeu #{target_game} trop proche du dernier prédit (#{last_predicted_game}), écart requis: {get_current_ecart()}")
            return
        
        if target_game in pending_predictions:
            logger.info(f"Prédiction #{target_game} déjà active")
            return
        
        time_slot = get_current_time_slot()
        source_display = SUIT_DISPLAY.get(source_suit, source_suit)
        predicted_display = SUIT_DISPLAY.get(predicted_suit, predicted_suit)
        mode_str = "🧠 Intelligent" if intelligent_mode else f"📐 Statique ({time_slot})"
        
        logger.info(f"Jeu #{game_number} - Position k={k_position}: {source_display} -> Prédiction: {predicted_display} pour #{target_game} (mode: {mode_str})")
        
        await send_prediction_to_channel(target_game, predicted_suit)
        
        if ADMIN_ID and ADMIN_ID != 0 and admin_notifications:
            try:
                now = datetime.now(WAT_TZ)
                admin_msg = f"""🎯 **Nouvelle prédiction automatique**

📊 Source: Jeu #{game_number}
🎴 Carte position k={k_position}: {source_display}
🔮 Prédiction: {predicted_display}
📲 Cible: Jeu #{target_game}
🕐 Heure: {now.strftime('%H:%M')} WAT
📏 Écart: {get_current_ecart()}
🎲 Mode: {mode_str}"""
                await client.send_message(ADMIN_ID, admin_msg)
            except Exception as e:
                logger.error(f"Erreur notification admin: {e}")
        
    except Exception as e:
        logger.error(f"Erreur traitement source 1: {e}")
        import traceback
        logger.error(traceback.format_exc())

async def process_source_2_message(message_text: str, chat_id: int):
    """
    Traite les messages du canal source 2 (pour la vérification).
    Vérifie si les prédictions actives sont correctes.
    """
    global current_game_number
    
    try:
        if not is_message_finalized(message_text):
            return
        
        game_number = extract_game_number(message_text)
        if game_number is None:
            return
        
        current_game_number = game_number
        
        message_hash = f"src2_{game_number}_{message_text[:50]}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)
        
        groups = extract_parentheses_groups(message_text)
        if len(groups) < 1:
            return
        
        first_group = groups[0]
        
        logger.info(f"Vérification Jeu #{game_number} - Groupe1: {first_group}")
        
        await check_prediction_result(game_number, first_group)
        
    except Exception as e:
        logger.error(f"Erreur traitement source 2: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.NewMessage())
async def handle_all_messages(event):
    """Gestionnaire global pour tous les messages - debug et routage."""
    try:
        chat_id = event.chat_id
        message_text = event.message.text if event.message and event.message.text else ""
        
        logger.debug(f"[DEBUG] Message reçu de chat_id={chat_id}: {message_text[:100] if message_text else 'NO TEXT'}")
        
        if chat_id == SOURCE_CHANNEL_1_ID or chat_id == abs(SOURCE_CHANNEL_1_ID):
            logger.info(f"[SOURCE 1] Message reçu: {message_text[:100]}")
            if message_text:
                await process_source_1_message(message_text, chat_id)
        elif chat_id == SOURCE_CHANNEL_2_ID or chat_id == abs(SOURCE_CHANNEL_2_ID):
            logger.info(f"[SOURCE 2] Message reçu: {message_text[:100]}")
            if message_text:
                await process_source_2_message(message_text, chat_id)
        elif chat_id == PREDICTION_CHANNEL_ID or chat_id == abs(PREDICTION_CHANNEL_ID):
            logger.debug(f"[PREDICTION] Message ignoré (propre canal)")
        else:
            pass
    except Exception as e:
        logger.error(f"Erreur dans handle_all_messages: {e}")

@client.on(events.Raw())
async def handle_raw_updates(event):
    """Capture les mises à jour brutes pour le débogage."""
    try:
        from telethon.tl.types import UpdateNewChannelMessage
        if isinstance(event, UpdateNewChannelMessage):
            msg = event.message
            chat_id = getattr(msg.peer_id, 'channel_id', None)
            if chat_id:
                chat_id_full = -int(f"100{chat_id}")
                text = getattr(msg, 'message', '')
                logger.info(f"[RAW] UpdateNewChannelMessage - channel_id={chat_id}, full_id={chat_id_full}, text={text[:80] if text else 'NO TEXT'}")
                
                if chat_id_full == SOURCE_CHANNEL_1_ID and text:
                    logger.info(f"[RAW->SOURCE1] Traitement du message")
                    await process_source_1_message(text, chat_id_full)
                elif chat_id_full == SOURCE_CHANNEL_2_ID and text:
                    logger.info(f"[RAW->SOURCE2] Traitement du message")
                    await process_source_2_message(text, chat_id_full)
    except Exception as e:
        logger.error(f"Erreur raw update: {e}")

@client.on(events.NewMessage(pattern=r'^/k\s*(\d+)$'))
async def cmd_k(event):
    """Commande /k - Définit la position de la carte à utiliser."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global k_position
    try:
        new_k = int(event.pattern_match.group(1))
        if new_k < 1:
            await event.respond("❌ La position k doit être >= 1")
            return
        
        k_position = new_k
        save_config()
        await event.respond(f"✅ Position k définie à **{k_position}**\n\nLe bot utilisera maintenant la carte à la position {k_position} du premier groupe pour générer les prédictions.")
        logger.info(f"Position k mise à jour: {k_position}")
    except ValueError:
        await event.respond("❌ Veuillez entrer un nombre entier valide")

@client.on(events.NewMessage(pattern=r'^/a\s*(\d+)$'))
async def cmd_a(event):
    """Commande /a - Définit l'offset de prédiction (N+a)."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global a_offset
    try:
        new_a = int(event.pattern_match.group(1))
        if new_a < 0:
            await event.respond("❌ L'offset a doit être >= 0")
            return
        
        a_offset = new_a
        save_config()
        await event.respond(f"✅ Offset a défini à **{a_offset}**\n\nLe bot prédira maintenant pour le jeu N+{a_offset} (si a=1, prédit N+1)")
        logger.info(f"Offset a mis à jour: {a_offset}")
    except ValueError:
        await event.respond("❌ Veuillez entrer un nombre entier valide")

@client.on(events.NewMessage(pattern=r'^/r\s*(\d+)$'))
async def cmd_r(event):
    """Commande /r - Définit le nombre d'essais de vérification (0 à 10)."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global r_offset
    try:
        new_r = int(event.pattern_match.group(1))
        if new_r < 0 or new_r > 10:
            await event.respond("❌ L'offset r doit être entre 0 et 10")
            return
        
        r_offset = new_r
        save_config()
        
        emojis_list = [VERIFICATION_EMOJIS[i] for i in range(r_offset + 1)]
        emojis_str = " ".join(emojis_list)
        
        await event.respond(f"""✅ Offset r défini à **{r_offset}**

**Vérification sur {r_offset + 1} jeu(x):** N+0 à N+{r_offset}

**Emojis de succès:**
{emojis_str}

Si aucun essai ne réussit → ❌""")
        logger.info(f"Offset r mis à jour: {r_offset}")
    except ValueError:
        await event.respond("❌ Veuillez entrer un nombre entier valide")

@client.on(events.NewMessage(pattern=r'^/eca\s*(.+)$'))
async def cmd_eca(event):
    """Commande /eca - Définit les écarts entre les prédictions (ex: /eca 3,2,5)."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global ecart_list, ecart_index
    try:
        values_str = event.pattern_match.group(1).strip()
        
        if values_str.lower() == 'reset' or values_str == '0':
            ecart_list = []
            ecart_index = 0
            save_config()
            await event.respond(f"✅ Écarts réinitialisés. Écart par défaut: **{DEFAULT_ECART}**")
            return
        
        values = [int(x.strip()) for x in values_str.replace(' ', ',').split(',') if x.strip()]
        
        if not all(v >= 1 for v in values):
            await event.respond("❌ Tous les écarts doivent être >= 1")
            return
        
        ecart_list = values
        ecart_index = 0
        save_config()
        
        ecart_display = " → ".join([str(e) for e in ecart_list])
        await event.respond(f"""✅ Écarts personnalisés définis:

**Séquence:** {ecart_display}

Le bot utilisera ces écarts dans l'ordre, puis recommencera au début.
- Écart entre prédiction 1 et 2: {ecart_list[0] if len(ecart_list) > 0 else DEFAULT_ECART}
- Écart entre prédiction 2 et 3: {ecart_list[1] if len(ecart_list) > 1 else ecart_list[0] if ecart_list else DEFAULT_ECART}
etc.""")
        logger.info(f"Écarts mis à jour: {ecart_list}")
    except ValueError:
        await event.respond("❌ Format invalide. Utilisez: /eca 3,2,5")

@client.on(events.NewMessage(pattern='/inter'))
async def cmd_inter(event):
    """Commande /inter - Active/désactive le mode intelligent."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global intelligent_mode
    intelligent_mode = not intelligent_mode
    save_config()
    
    if intelligent_mode:
        await event.respond(f"""🧠 **Mode INTELLIGENT activé**

**Règle intelligente:**
• La carte à la position k={k_position} est prédite directement
• Pas de transformation selon les plages horaires
• Exemple: Si ♦️ est à la position {k_position}, le bot prédit ♦️ pour N+{a_offset}

Les règles statiques (plages horaires) sont désactivées.

Pour revenir au mode statique: /inter""")
    else:
        await event.respond(f"""📐 **Mode STATIQUE activé**

**Règle statique:**
• La carte à la position k={k_position} est transformée selon l'heure
• 00h-12h: ♣️↔♦️, ♠️↔❤️
• 13h-19h: ♣️↔♠️, ♦️↔❤️
• 19h01-23h59: ♠️↔♦️, ❤️↔♣️

Pour activer le mode intelligent: /inter""")
    
    logger.info(f"Mode {'intelligent' if intelligent_mode else 'statique'} activé")

@client.on(events.NewMessage(pattern='/stop'))
async def cmd_stop(event):
    """Commande /stop - Active/désactive les notifications privées."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global admin_notifications
    admin_notifications = not admin_notifications
    save_config()
    
    if admin_notifications:
        await event.respond("""✅ **Notifications ACTIVÉES**

Vous recevrez les messages de prédiction automatique dans ce chat.

Pour désactiver: /stop""")
    else:
        await event.respond("""🔇 **Notifications DÉSACTIVÉES**

Vous ne recevrez plus les messages de prédiction automatique dans ce chat.
Les prédictions continueront d'être envoyées au canal de prédiction.

Pour réactiver: /stop""")
    
    logger.info(f"Notifications admin {'activées' if admin_notifications else 'désactivées'}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    """Affiche l'état actuel du bot et des prédictions."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    now_wat = datetime.now(WAT_TZ)
    time_slot = get_current_time_slot()
    
    mode_str = "🧠 Intelligent" if intelligent_mode else f"📐 Statique"
    status_msg = f"""📊 **État du bot**

🕐 Heure WAT: {now_wat.strftime('%H:%M:%S')}
📍 Plage horaire: {time_slot}
🎮 Jeu actuel: #{current_game_number}
📲 Dernier prédit: #{last_predicted_game}

**⚙️ Paramètres:**
• Position k: {k_position}
• Offset a: {a_offset}
• Offset r: {r_offset} (vérifie N+0 à N+{r_offset})
• Écarts: {ecart_list if ecart_list else f"[défaut: {DEFAULT_ECART}]"}
• Index écart: {ecart_index}
• Mode: {mode_str}
• Notifications: {'✅ Activées' if admin_notifications else '🔇 Désactivées'}

**📡 Canaux:**
• Source 1 (prédictions): {SOURCE_CHANNEL_1_ID} {'✅' if source_channel_1_ok else '❌'}
• Source 2 (vérifications): {SOURCE_CHANNEL_2_ID} {'✅' if source_channel_2_ok else '❌'}
• Prédiction: {PREDICTION_CHANNEL_ID} {'✅' if prediction_channel_ok else '❌'}
"""
    
    if pending_predictions:
        status_msg += f"\n**🔮 Prédictions actives ({len(pending_predictions)}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            checks = pred.get('check_count', 0)
            max_checks = pred.get('max_checks', r_offset + 1)
            status_msg += f"• #{game_num}: {pred['suit_display']} - {pred['status']} (vérifié {checks}/{max_checks})\n"
    else:
        status_msg += "\n**🔮 Aucune prédiction active**\n"
    
    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset(event):
    """Réinitialise toutes les données du bot."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    global pending_predictions, processed_messages, current_game_number, last_predicted_game
    global k_position, a_offset, r_offset, ecart_list, ecart_index, intelligent_mode, admin_notifications
    
    pending_predictions.clear()
    processed_messages.clear()
    current_game_number = 0
    last_predicted_game = 0
    k_position = DEFAULT_K
    a_offset = DEFAULT_A
    r_offset = DEFAULT_R
    ecart_list = []
    ecart_index = 0
    intelligent_mode = False
    admin_notifications = True
    
    save_config()
    
    await event.respond("""✅ **Réinitialisation complète effectuée**

Tous les paramètres sont revenus aux valeurs par défaut:
• k = 1
• a = 0
• r = 1
• écarts = [défaut: 4]
• mode = statique
• notifications = activées

Les prédictions actives ont été effacées.""")
    logger.warning("Reset complet effectué")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    """Affiche l'aide."""
    if event.is_group or event.is_channel:
        return
    
    mode_str = "🧠 Intelligent" if intelligent_mode else "📐 Statique"
    await event.respond(f"""🤖 **Bot de Prédiction Baccarat**

**📌 Commandes de configuration:**
• `/k <n>` - Position de la carte à utiliser (1, 2, 3...)
• `/a <n>` - Offset de prédiction (prédit pour N+a)
• `/r <n>` - Essais de vérification (0-10)
• `/eca <n1,n2,n3>` - Écarts personnalisés
• `/eca reset` - Réinitialiser les écarts
• `/inter` - Basculer entre mode intelligent/statique
• `/stop` - Activer/désactiver les notifications privées

**📊 Commandes d'information:**
• `/status` - État du bot
• `/reset` - Réinitialiser tout
• `/deploy` - Télécharger les fichiers pour Render.com
• `/help` - Cette aide

**🎲 Mode actuel:** {mode_str}

**🧠 Mode Intelligent (/inter):**
• Prédit directement la carte à la position k
• Pas de transformation

**📐 Mode Statique (règles horaires):**
• 00h-12h: ♣️↔♦️, ♠️↔❤️
• 13h-19h: ♣️↔♠️, ♦️↔❤️
• 19h01-23h59: ♠️↔♦️, ❤️↔♣️

**📡 Canaux configurés:**
• Source 1 (prédictions): {SOURCE_CHANNEL_1_ID}
• Source 2 (vérifications): {SOURCE_CHANNEL_2_ID}""")

@client.on(events.NewMessage(pattern='/deploy'))
async def cmd_deploy(event):
    """Commande /deploy - Fournit le lien de téléchargement des fichiers."""
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return
    
    deploy_url = f"https://{os.getenv('REPL_SLUG', 'bot')}.{os.getenv('REPL_OWNER', 'user')}.repl.co/download" if os.getenv('REPL_SLUG') else f"http://localhost:{PORT}/download"
    
    await event.respond(f"""📦 **Téléchargement pour Render.com**

**Lien de téléchargement:**
{deploy_url}

**Instructions:**
1. Cliquez sur le lien pour télécharger le fichier ZIP
2. Extrayez le ZIP sur votre ordinateur
3. Créez un nouveau Web Service sur render.com
4. Connectez votre dépôt GitHub ou uploadez les fichiers
5. Le service utilisera automatiquement le port 10000

**Variables d'environnement à configurer sur Render:**
• `API_ID` - Votre API ID Telegram
• `API_HASH` - Votre API Hash Telegram
• `BOT_TOKEN` - Token de votre bot
• `ADMIN_ID` - Votre ID Telegram
• `TELEGRAM_SESSION` - (optionnel) String de session
• `PORT` - Sera automatiquement défini par Render

**Fichiers inclus:**
• main.py - Code principal du bot
• config.py - Configuration
• requirements.txt - Dépendances Python
• render.yaml - Configuration Render.com
• README_DEPLOY.md - Instructions détaillées""")

async def download_zip(request):
    """Route pour télécharger le fichier ZIP déployable."""
    files_to_include = [
        'main.py',
        'config.py', 
        'requirements.txt',
        'render.yaml',
        'README_DEPLOY.md'
    ]
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename in files_to_include:
            if os.path.exists(filename):
                with open(filename, 'r', encoding='utf-8') as f:
                    content = f.read()
                if filename == 'config.py':
                    content = content.replace(f"PORT = int(os.getenv('PORT') or '5000')", "PORT = int(os.getenv('PORT') or '10000')")
                zf.writestr(filename, content)
    
    zip_buffer.seek(0)
    
    return web.Response(
        body=zip_buffer.getvalue(),
        content_type='application/zip',
        headers={
            'Content-Disposition': 'attachment; filename="dina.zip"'
        }
    )

async def index(request):
    html = f"""<!DOCTYPE html>
<html>
<head><title>Bot Prédiction Baccarat</title></head>
<body>
<h1>🎯 Bot de Prédiction Baccarat</h1>
<p>Le bot est en ligne.</p>
<p><strong>Jeu actuel:</strong> #{current_game_number}</p>
<p><strong>Paramètres:</strong> k={k_position}, a={a_offset}, r={r_offset}</p>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    """Démarre le serveur web."""
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    app.router.add_get('/download', download_zip)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Serveur web démarré sur le port {PORT}")

async def schedule_daily_reset():
    """Tâche planifiée pour la réinitialisation quotidienne à 00h59 WAT."""
    wat_tz = timezone(timedelta(hours=1))
    reset_time = time(0, 59, tzinfo=wat_tz)
    
    logger.info(f"Tâche de reset planifiée pour {reset_time} WAT.")
    
    while True:
        now = datetime.now(wat_tz)
        target_datetime = datetime.combine(now.date(), reset_time, tzinfo=wat_tz)
        if now >= target_datetime:
            target_datetime += timedelta(days=1)
        
        time_to_wait = (target_datetime - now).total_seconds()
        logger.info(f"Prochain reset dans {timedelta(seconds=time_to_wait)}")
        await asyncio.sleep(time_to_wait)
        
        logger.warning("🚨 RESET QUOTIDIEN À 00h59 WAT DÉCLENCHÉ!")
        
        global pending_predictions, processed_messages, current_game_number, last_predicted_game, ecart_index
        
        pending_predictions.clear()
        processed_messages.clear()
        current_game_number = 0
        last_predicted_game = 0
        ecart_index = 0
        
        save_config()
        logger.warning("✅ Données réinitialisées pour le nouveau cycle")

async def start_bot():
    """Démarre le client Telegram."""
    global source_channel_1_ok, source_channel_2_ok, prediction_channel_ok
    try:
        await client.start(bot_token=BOT_TOKEN)
        
        logger.info("Bot connecté. Vérification des accès aux canaux...")
        
        try:
            entity1 = await client.get_entity(SOURCE_CHANNEL_1_ID)
            source_channel_1_ok = True
            logger.info(f"✅ Canal source 1 accessible: {getattr(entity1, 'title', SOURCE_CHANNEL_1_ID)}")
        except Exception as e:
            source_channel_1_ok = False
            logger.error(f"❌ Canal source 1 ({SOURCE_CHANNEL_1_ID}) non accessible: {e}")
        
        try:
            entity2 = await client.get_entity(SOURCE_CHANNEL_2_ID)
            source_channel_2_ok = True
            logger.info(f"✅ Canal source 2 accessible: {getattr(entity2, 'title', SOURCE_CHANNEL_2_ID)}")
        except Exception as e:
            source_channel_2_ok = False
            logger.error(f"❌ Canal source 2 ({SOURCE_CHANNEL_2_ID}) non accessible: {e}")
        
        try:
            entity3 = await client.get_entity(PREDICTION_CHANNEL_ID)
            prediction_channel_ok = True
            logger.info(f"✅ Canal prédiction accessible: {getattr(entity3, 'title', PREDICTION_CHANNEL_ID)}")
        except Exception as e:
            prediction_channel_ok = False
            logger.error(f"❌ Canal prédiction ({PREDICTION_CHANNEL_ID}) non accessible: {e}")
        
        if ADMIN_ID and ADMIN_ID != 0:
            try:
                status_msg = f"""🤖 **Bot démarré**

**État des canaux:**
• Source 1 ({SOURCE_CHANNEL_1_ID}): {'✅' if source_channel_1_ok else '❌'}
• Source 2 ({SOURCE_CHANNEL_2_ID}): {'✅' if source_channel_2_ok else '❌'}
• Prédiction ({PREDICTION_CHANNEL_ID}): {'✅' if prediction_channel_ok else '❌'}

**Paramètres:**
• k={k_position}, a={a_offset}, r={r_offset}
• Écarts: {ecart_list if ecart_list else f'[défaut: {DEFAULT_ECART}]'}

⚠️ Si un canal est ❌, ajoutez le bot comme administrateur dans ce canal."""
                await client.send_message(ADMIN_ID, status_msg)
            except Exception as e:
                logger.error(f"Erreur envoi status admin: {e}")
        
        return True
    except Exception as e:
        logger.error(f"Erreur démarrage: {e}")
        return False

async def main():
    """Fonction principale."""
    try:
        load_config()
        
        await start_web_server()
        
        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage du bot")
            return
        
        asyncio.create_task(schedule_daily_reset())
        
        logger.info("Bot opérationnel - En attente de messages...")
        logger.info(f"Paramètres: k={k_position}, a={a_offset}, r={r_offset}, écarts={ecart_list}")
        
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Erreur dans main: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
