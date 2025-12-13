"""
Configuration du bot Telegram de prédiction Baccarat
Version avec 2 canaux sources et nouvelles règles de prédiction
"""
import os

def parse_channel_id(env_var: str, default: str) -> int:
    value = os.getenv(env_var) or default
    channel_id = int(value)
    if channel_id > 0 and len(str(channel_id)) >= 10:
        channel_id = -channel_id
    return channel_id

# Canal source 1: Pour les règles de prédiction automatique
SOURCE_CHANNEL_1_ID = parse_channel_id('SOURCE_CHANNEL_1_ID', '-1003424179389')

# Canal source 2: Pour la vérification des statuts
SOURCE_CHANNEL_2_ID = parse_channel_id('SOURCE_CHANNEL_2_ID', '-1002682552255')

# Canal de prédiction (où le bot envoie ses prédictions)
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1003430118891')

ADMIN_ID = int(os.getenv('ADMIN_ID') or '0')

API_ID = int(os.getenv('API_ID') or '0')
API_HASH = os.getenv('API_HASH') or ''
BOT_TOKEN = os.getenv('BOT_TOKEN') or ''

PORT = int(os.getenv('PORT') or '10000')

# Règles de prédiction selon les plages horaires béninoises (WAT = UTC+1)
# Plage 1: 00h00 - 12h59 (minuit à midi)
PREDICTION_RULES_MORNING = {
    '♣': '♦',  # Trèfle -> Carreau
    '♦': '♣',  # Carreau -> Trèfle
    '♠': '♥',  # Pique -> Coeur
    '♥': '♠',  # Coeur -> Pique
}

# Plage 2: 13h00 - 19h00
PREDICTION_RULES_AFTERNOON = {
    '♣': '♠',  # Trèfle -> Pique
    '♠': '♣',  # Pique -> Trèfle
    '♦': '♥',  # Carreau -> Coeur
    '♥': '♦',  # Coeur -> Carreau
}

# Plage 3: 19h01 - 23h59
PREDICTION_RULES_EVENING = {
    '♠': '♦',  # Pique -> Carreau
    '♦': '♠',  # Carreau -> Pique
    '♥': '♣',  # Coeur -> Trèfle
    '♣': '♥',  # Trèfle -> Coeur
}

# Emojis de vérification selon l'offset (N+0, N+1, N+2, etc.)
VERIFICATION_EMOJIS = {
    0: "✅0️⃣",   # 1er essai (N+0)
    1: "✅1️⃣",   # 2ème essai (N+1)
    2: "✅2️⃣",   # 3ème essai (N+2)
    3: "✅3️⃣",   # 4ème essai (N+3)
    4: "✅4️⃣",   # 5ème essai (N+4)
    5: "✅5️⃣",   # 6ème essai (N+5)
    6: "✅6️⃣",   # 7ème essai (N+6)
    7: "✅7️⃣",   # 8ème essai (N+7)
    8: "✅8️⃣",   # 9ème essai (N+8)
    9: "✅9️⃣",   # 10ème essai (N+9)
    10: "✅🔟"   # 11ème essai (N+10)
}

ALL_SUITS = ['♠', '♥', '♦', '♣']
SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}

# Valeurs par défaut pour les paramètres
DEFAULT_K = 1           # Position de la carte par défaut
DEFAULT_A = 0           # Offset de prédiction par défaut
DEFAULT_R = 1           # Nombre d'essais de vérification par défaut
DEFAULT_ECART = 3       # Écart par défaut entre les prédictions (si #1 prédit, prochain #4)
MAX_GAME_NUMBER = 1440  # Numéro de jeu maximum avant reset du cycle
