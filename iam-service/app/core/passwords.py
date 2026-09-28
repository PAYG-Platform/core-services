# iam-service/app/core/passwords.py

COMMON_WEAK_PASSWORDS_SET = {
    # Sequential & Repeated Numbers
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "00000000", "11111111", "22222222", "33333333", "44444444",
    "55555555", "66666666", "77777777", "88888888", "99999999",
    "123123123", "65432100", "987654321", "12341234", "11223344",

    # Keyboard Walks & Patterns
    "qwertyuiop", "asdfghjkl", "zxcvbnm", "qwerty123", "qwertzuiop",
    "azertyuiop", "1q2w3e4r", "1q2w3e4r5t", "qazwsxedc", "wsxedc123",
    "123qweasd", "qweasdzxc", "asdf1234", "zxcv1234", "q1w2e3r4",

    # Common Words & Variants
    "password", "password123", "pass1234", "pass@123", "password1234",
    "welcome", "welcome123", "welcome1", "letmein", "letmein123",
    "iloveyou", "princess", "sunshine", "football", "baseball",
    "dragon", "monkey", "shadow", "superman", "master", "master123",
    "secret", "secret123", "trustno1", "freedom", "whatever",

    # System & Admin Defaults
    "admin", "admin123", "administrator", "root", "root1234",
    "guest", "guest123", "user", "user1234", "default", "default123",
    "system", "system123", "server", "server123", "cisco", "cisco123",

    # VPN & Network Defaults
    "vpn12345", "vpn123456", "vpnpass", "vpnpassword", "proxy123",
    "paygvpn123", "tunnel123", "connect123", "mysecretvpn",

    # Years & Combinations
    "password2020", "password2021", "password2022", "password2023",
    "password2024", "password2025", "password2026", "admin2024",
    "admin2025", "admin2026", "20202020", "20242024", "20252025", "20262026"
}


def is_common_weak_password(password: str) -> bool:
    """
    Returns True if the password matches common dictionary entries,
    single-character repetitions, or simple numeric sequences.
    """
    if not password:
        return False

    lower = password.lower().strip()

    # 1. Direct dictionary match
    if lower in COMMON_WEAK_PASSWORDS_SET:
        return True

    # 2. Check for single character repetitions (e.g. "aaaaaaaa", "11111111")
    if len(set(lower)) == 1:
        return True

    # 3. Check for simple numeric sequences
    if lower in "01234567890123456789" or lower in "98765432109876543210":
        return True

    return False