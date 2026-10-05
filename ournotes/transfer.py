"""Password rules and generation for the game's ID/password transfer flow."""
import re
import secrets
import string


def validate_password(password):
    # MasterText datalink_password_dialog_cation: 8-16 half-width characters.
    if not isinstance(password, str) or not re.fullmatch(r'[!-~]{8,16}', password):
        raise ValueError('Transfer password must contain 8-16 printable ASCII characters without spaces')
    return password


def generate_password():
    groups = (string.ascii_uppercase, string.ascii_lowercase, string.digits, '!@#_-')
    chars = [secrets.choice(group) for group in groups]
    chars.extend(secrets.choice(''.join(groups)) for _ in range(12))
    secrets.SystemRandom().shuffle(chars)
    return ''.join(chars)


def profile_id(state):
    value = (state.get('credential') or {}).get('profile_id') or state.get(
        'last_player_data', {}).get('player_data', {}).get('my_profile', {}).get('profile_id')
    if not value or not str(value).isdigit() or int(value) <= 0:
        raise ValueError('Missing numeric player profile ID; fetch player data before binding transfer')
    return str(value)
