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

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Vérifications de sécurité
if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Config: SRC1={SOURCE_CHANNEL_1_ID}, SRC2={SOURCE_CHANNEL_2_ID}, PRED={PREDICTION_CHANNEL_ID}")

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

WAT_TZ = timezone(timedelta(hours=1))

# Variables globales
pending_predictions = {}
processed_messages = set()
current_game_number = 0
last_predicted_game = 0

# Paramètres
k_position = DEFAULT_K
a_offset = DEFAULT_A
r_offset = DEFAULT_R
ecart_list = []
ecart_index = 0
intelligent_mode = False
admin_notifications = True

# État des canaux
source_channel_1_ok = False
source_channel_2_ok = False
prediction_channel_ok = False

CONFIG_FILE = 'bot_config.json'

def save_config():
    config = {
        'k_position': k_position, 'a_offset': a_offset, 'r_offset': r_offset,
        'ecart_list': ecart_list, 'ecart_index': ecart_index,
        'last_predicted_game': last_predicted_game,
        'intelligent_mode': intelligent_mode, 'admin_notifications': admin_notifications
    }
    try:
        with open(CONFIG_FILE, 'w') as f: json.dump(config, f)
    except Exception as e: logger.error(f"Erreur save config: {e}")

def load_config():
    global k_position, a_offset, r_offset, ecart_list, ecart_index, last_predicted_game, intelligent_mode, admin_notifications
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f: config = json.load(f)
            k_position = config.get('k_position', DEFAULT_K)
            a_offset = config.get('a_offset', DEFAULT_A)
            r_offset = config.get('r_offset', DEFAULT_R)
            ecart_list = config.get('ecart_list', [])
            ecart_index = config.get('ecart_index', 0)
            last_predicted_game = config.get('last_predicted_game', 0)
            intelligent_mode = config.get('intelligent_mode', False)
            admin_notifications = config.get('admin_notifications', True)
    except Exception as e: logger.error(f"Erreur load config: {e}")

# --- Fonctions Utilitaires ---
def extract_game_number(message: str):
    match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
    return int(match.group(1)) if match else None

def extract_parentheses_groups(message: str):
    return re.findall(r"\(([^)]*)\)", message)

def normalize_suits(group_str: str) -> str:
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def get_suit_at_position(group_str: str, position: int) -> str:
    normalized = normalize_suits(group_str)
    suits_found = [char for char in normalized if char in ALL_SUITS]
    if position <= 0 or position > len(suits_found): return None
    return suits_found[position - 1]

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    normalized = normalize_suits(group_str)
    return target_suit in normalized

def get_current_time_slot():
    h = datetime.now(WAT_TZ).hour
    if 0 <= h <= 12: return 'morning'
    if 13 <= h < 19: return 'afternoon'
    if h == 19 and datetime.now(WAT_TZ).minute == 0: return 'afternoon'
    return 'evening'

def predict_suit(source_suit: str) -> str:
    rules = PREDICTION_RULES_MORNING
    slot = get_current_time_slot()
    if slot == 'afternoon': rules = PREDICTION_RULES_AFTERNOON
    elif slot == 'evening': rules = PREDICTION_RULES_EVENING
    return rules.get(source_suit, source_suit)

def get_current_ecart():
    if not ecart_list: return DEFAULT_ECART
    global ecart_index
    if ecart_index >= len(ecart_list): ecart_index = 0
    return ecart_list[ecart_index]

def advance_ecart():
    global ecart_index
    if ecart_list:
        ecart_index = (ecart_index + 1) % len(ecart_list)
        save_config()

def is_message_finalized(message: str) -> bool:
    if '⏰' in message: return False
    return '✅' in message or '🔰' in message

def can_predict_game(game_number: int) -> bool:
    if last_predicted_game == 0: return True
    return game_number >= last_predicted_game + get_current_ecart()
# --- Fonctions de Gestion des Messages ---

async def send_prediction_to_channel(target_game: int, predicted_suit: str):
    global last_predicted_game
    try:
        suit_display = SUIT_DISPLAY.get(predicted_suit, predicted_suit)
        prediction_msg = f"🔵{target_game}🔵:{suit_display} statut :⏳"
        msg_id = 0
        
        if PREDICTION_CHANNEL_ID and prediction_channel_ok:
            try:
                sent = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = sent.id
                logger.info(f"✅ Prédiction #{target_game} envoyée")
            except Exception as e: logger.error(f"❌ Erreur envoi: {e}")
        
        pending_predictions[target_game] = {
            'message_id': msg_id, 'suit': predicted_suit, 'suit_display': suit_display,
            'status': '⏳', 'check_count': 0, 'max_checks': r_offset + 1
        }
        last_predicted_game = target_game
        advance_ecart()
        save_config()
    except Exception as e: logger.error(f"Erreur send_prediction: {e}")

async def update_prediction_status(game_number: int, new_status: str):
    if game_number not in pending_predictions: return
    pred = pending_predictions[game_number]
    try:
        msg = f"🔵{game_number}🔵:{pred['suit_display']} statut :{new_status}"
        if pred['message_id'] > 0 and prediction_channel_ok:
            await client.edit_message(PREDICTION_CHANNEL_ID, pred['message_id'], msg)
        
        if new_status.startswith('✅') or new_status == '❌':
            del pending_predictions[game_number]
    except Exception as e: logger.error(f"Erreur update: {e}")

async def check_prediction_result(game_number: int, first_group: str):
    for pred_game in list(pending_predictions.keys()):
        if pred_game not in pending_predictions: continue
        pred = pending_predictions[pred_game]
        expected_game = pred_game + pred['check_count']
        
        if game_number == expected_game:
            if has_suit_in_group(first_group, pred['suit']):
                emoji = VERIFICATION_EMOJIS.get(pred['check_count'], "✅")
                await update_prediction_status(pred_game, emoji)
            else:
                pred['check_count'] += 1
                if pred['check_count'] >= pred['max_checks']:
                    await update_prediction_status(pred_game, '❌')

async def process_source_1_message(message_text: str):
    """Logique pour SOURCE 1 : PRÉDICTION"""
    global current_game_number
    if not is_message_finalized(message_text): return
    
    gn = extract_game_number(message_text)
    if not gn: return
    current_game_number = gn
    
    # Anti-doublon simple
    h = f"s1_{gn}"
    if h in processed_messages: return
    processed_messages.add(h)
    if len(processed_messages) > 200: processed_messages.clear()
    
    groups = extract_parentheses_groups(message_text)
    if not groups: return
    
    suit = get_suit_at_position(groups[0], k_position)
    if not suit: return
    
    pred_suit = suit if intelligent_mode else predict_suit(suit)
    target = gn + a_offset
    
    if can_predict_game(target) and target not in pending_predictions:
        await send_prediction_to_channel(target, pred_suit)

async def process_source_2_message(message_text: str):
    """Logique pour SOURCE 2 : VÉRIFICATION"""
    global current_game_number
    if not is_message_finalized(message_text): return
    
    gn = extract_game_number(message_text)
    if not gn: return
    current_game_number = gn
    
    groups = extract_parentheses_groups(message_text)
    if groups:
        await check_prediction_result(gn, groups[0])

@client.on(events.NewMessage())
async def handle_all_messages(event):
    """
    CORRECTION MAJEURE: Utilise abs() pour comparer les IDs.
    Cela corrige le problème où Source 1 ou 2 n'étaient pas reconnus.
    """
    try:
        chat_id = event.chat_id
        text = event.message.text or ""
        # On compare la valeur absolue pour gérer les IDs -100xxxx vs 100xxxx
        abs_id = abs(chat_id)
        
        if abs_id == abs(SOURCE_CHANNEL_1_ID):
            logger.info(f"[SOURCE 1] Reçu: {text[:30]}")
            if text: await process_source_1_message(text)
            
        elif abs_id == abs(SOURCE_CHANNEL_2_ID):
            logger.info(f"[SOURCE 2] Reçu: {text[:30]}")
            if text: await process_source_2_message(text)
            
    except Exception as e:
        logger.error(f"Erreur handler: {e}")

# Handler RAW pour plus de robustesse
@client.on(events.Raw())
async def handle_raw_updates(event):
    try:
        from telethon.tl.types import UpdateNewChannelMessage
        if isinstance(event, UpdateNewChannelMessage):
            cid = getattr(event.message.peer_id, 'channel_id', None)
            if cid:
                full_id = int(f"-100{cid}")
                text = getattr(event.message, 'message', '')
                
                # Même logique de comparaison absolue
                if abs(full_id) == abs(SOURCE_CHANNEL_1_ID):
                    await process_source_1_message(text)
                elif abs(full_id) == abs(SOURCE_CHANNEL_2_ID):
                    await process_source_2_message(text)
    except Exception: pass
    # --- Commandes Admin ---

@client.on(events.NewMessage(pattern=r'^/k\s*(\d+)$'))
async def cmd_k(event):
    if event.sender_id != ADMIN_ID: return
    global k_position
    k_position = int(event.pattern_match.group(1))
    save_config()
    await event.respond(f"✅ k={k_position}")

@client.on(events.NewMessage(pattern=r'^/a\s*(\d+)$'))
async def cmd_a(event):
    if event.sender_id != ADMIN_ID: return
    global a_offset
    a_offset = int(event.pattern_match.group(1))
    save_config()
    await event.respond(f"✅ a={a_offset}")

@client.on(events.NewMessage(pattern=r'^/r\s*(\d+)$'))
async def cmd_r(event):
    if event.sender_id != ADMIN_ID: return
    global r_offset
    r_offset = int(event.pattern_match.group(1))
    save_config()
    await event.respond(f"✅ r={r_offset}")

@client.on(events.NewMessage(pattern=r'^/eca\s*(.+)$'))
async def cmd_eca(event):
    if event.sender_id != ADMIN_ID: return
    global ecart_list, ecart_index
    val = event.pattern_match.group(1)
    if val == 'reset': ecart_list = []
    else: ecart_list = [int(x) for x in val.split(',') if x.strip().isdigit()]
    ecart_index = 0
    save_config()
    await event.respond(f"✅ ecarts={ecart_list}")

@client.on(events.NewMessage(pattern='/inter'))
async def cmd_inter(event):
    if event.sender_id != ADMIN_ID: return
    global intelligent_mode
    intelligent_mode = not intelligent_mode
    save_config()
    await event.respond(f"✅ Mode Intelligent: {intelligent_mode}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.sender_id != ADMIN_ID: return
    msg = f"""📊 **État**
S1: {SOURCE_CHANNEL_1_ID} {'✅' if source_channel_1_ok else '❌'}
S2: {SOURCE_CHANNEL_2_ID} {'✅' if source_channel_2_ok else '❌'}
Pred: {PREDICTION_CHANNEL_ID} {'✅' if prediction_channel_ok else '❌'}
Param: k={k_position} a={a_offset} r={r_offset}
Preds actives: {len(pending_predictions)}
"""
    await event.respond(msg)

@client.on(events.NewMessage(pattern='/deploy'))
async def cmd_deploy(event):
    if event.sender_id != ADMIN_ID: return
    await event.respond(f"Lien: http://localhost:{PORT}/download")

# --- Serveur Web & Main ---

async def download_zip(request):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in ['main.py', 'config.py', 'requirements.txt', 'render.yaml']:
            if os.path.exists(f): zf.writestr(f, open(f).read())
    return web.Response(body=buf.getvalue(), content_type='application/zip')

async def start_web():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot Online"))
    app.router.add_get('/download', download_zip)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()

async def schedule_reset():
    while True:
        now = datetime.now(WAT_TZ)
        target = now.replace(hour=0, minute=59, second=0)
        if now >= target: target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        pending_predictions.clear()
        processed_messages.clear()
        logger.info("♻️ Reset quotidien")

async def main():
    load_config()
    await start_web()
    
    global source_channel_1_ok, source_channel_2_ok, prediction_channel_ok
    await client.start(bot_token=BOT_TOKEN)
    
    # Vérification des accès
    try:
        await client.get_entity(SOURCE_CHANNEL_1_ID)
        source_channel_1_ok = True
    except: logger.warning("⚠️ Source 1 inaccessible")
    
    try:
        await client.get_entity(SOURCE_CHANNEL_2_ID)
        source_channel_2_ok = True
    except: logger.warning("⚠️ Source 2 inaccessible")
    
    try:
        await client.get_entity(PREDICTION_CHANNEL_ID)
        prediction_channel_ok = True
    except: logger.warning("⚠️ Canal prédiction inaccessible")
    
    asyncio.create_task(schedule_reset())
    logger.info("Bot Démarré et Prêt.")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Crash: {e}")
