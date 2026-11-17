import os
import json

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

DEFAULT_PROVIDERS = {
    "simxpro": {
        "name": "Simxpro",
        "domain": "simxpro.org",
        "url": "https://simxpro.org/api/stubs/handler_api.php",
        "api_key": "fS5DlwcHNaXyvE3gFMXh76N1vs5OBkCn0yLifWs0",
        "default_country": "6",
        "default_service": "bme",
        "currency": "₹",
        "charges_info": "Operator 31: ₹7.00 | Operator 7: ₹12.42",
        "price": 7.00,
        "operators": {
            "31": {"name": "Operator 31", "price": 7.00, "operator_id": "31", "service": "bme"},
            "7": {"name": "Operator 7", "price": 12.42, "operator_id": "7", "service": "bme"}
        }
    },
    "grizzlysms": {
        "name": "Grizzly SMS",
        "domain": "grizzlysms.com",
        "url": "https://api.grizzlysms.com/stubs/handler_api.php",
        "api_key": "",
        "default_country": "6",
        "default_service": "im3",
        "currency": "₹",
        "charges_info": "myIM3 Indosat: ~₹7.50 – ₹11.00",
        "price": 8.00
    },
    "nexnum": {
        "name": "NexNum.in",
        "domain": "nexnum.in",
        "url": "https://nexnum.in/stubs/handler_api.php",
        "api_key": "",
        "default_country": "6",
        "default_service": "is",
        "currency": "₹",
        "charges_info": "myIM3 Indosat: ~₹7.00 – ₹10.00",
        "price": 7.50
    },
    "sms_activate": {
        "name": "SMS-Activate",
        "domain": "sms-activate.org",
        "url": "https://api.sms-activate.org/stubs/handler_api.php",
        "api_key": "",
        "default_country": "6",
        "default_service": "im3",
        "currency": "₹",
        "charges_info": "myIM3 Indosat: ~₹8.00 – ₹12.00",
        "price": 8.00
    },
    "uotp": {
        "name": "uOTP.store",
        "domain": "uotp.store",
        "url": "https://uotp.store/api/stubs/handler_api.php",
        "api_key": "PG0wnEUis2v9AJagtKghxtbvIhiNlOwe1yWUDMKZ",
        "default_country": "6",
        "default_service": "bme",
        "currency": "₹",
        "charges_info": "Operator (Budget): ₹9.55 | Operator 8: ₹13.91",
        "price": 9.55,
        "operators": {
            "uotp_955": {
                "name": "Operator (₹9.55)",
                "price": 9.55,
                "operator_id": "1",
                "service": "bme"
            },
            "8": {
                "name": "Operator 8 (₹13.91)",
                "price": 13.91,
                "operator_id": "8",
                "service": "bme"
            }
        }
    }
}

DEFAULT_SETTINGS = {
    "bot_token": "9191272564:AAFSmnHwwSy6T2XPxpWQ195hB0uWCPZzVoA",
    "sms_provider": "simxpro",
    "sms_api_key": "fS5DlwcHNaXyvE3gFMXh76N1vs5OBkCn0yLifWs0",
    "country_id": "6",
    "service_code": "bme",
    "selected_operator": "7",
    "admin_ids": [],
    "operators": {
        "31": {
            "name": "Operator 31",
            "price": 7.00,
            "currency": "₹",
            "operator_id": "31",
            "service": "bme"
        },
        "7": {
            "name": "Operator 7",
            "price": 12.42,
            "currency": "₹",
            "operator_id": "7",
            "service": "bme"
        }
    },
    "stats": {
        "total_claims": 0,
        "total_attempts": 0,
        "total_refunded": 0
    },
    "providers": DEFAULT_PROVIDERS
}

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in DEFAULT_SETTINGS.items():
                if k not in data:
                    data[k] = v

            # Merge providers
            if "providers" not in data or not isinstance(data["providers"], dict):
                data["providers"] = {}

            for p_key, p_val in DEFAULT_PROVIDERS.items():
                if p_key not in data["providers"]:
                    data["providers"][p_key] = p_val.copy()
                else:
                    # Ensure essential fields exist
                    for field in ["name", "domain", "url", "default_country", "default_service", "charges_info", "currency"]:
                        if field in p_val and field not in data["providers"][p_key]:
                            data["providers"][p_key][field] = p_val[field]
                    if p_key == "simxpro":
                        data["providers"][p_key]["url"] = "https://simxpro.org/api/stubs/handler_api.php"
                        if not data["providers"][p_key].get("api_key") and data.get("sms_api_key"):
                            data["providers"][p_key]["api_key"] = data["sms_api_key"]
                    elif p_key == "uotp":
                        data["providers"][p_key]["default_service"] = "bme"
                        data["providers"][p_key]["charges_info"] = p_val["charges_info"]
                        data["providers"][p_key]["operators"] = p_val["operators"]
                        data["providers"][p_key]["price"] = p_val["price"]
                        if not data["providers"][p_key].get("api_key"):
                            data["providers"][p_key]["api_key"] = p_val.get("api_key", "")

            if "operators" not in data:
                data["operators"] = DEFAULT_SETTINGS["operators"]
            if "stats" not in data:
                data["stats"] = DEFAULT_SETTINGS["stats"]
            return data
    except Exception as e:
        print(f"Error loading settings: {e}")
        return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4)
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False

def get_setting(key, default=None):
    env_key = key.upper()
    if env_key in os.environ and os.environ[env_key].strip():
        return os.environ[env_key].strip()
    s = load_settings()
    return s.get(key, default)

def update_setting(key, value):
    s = load_settings()
    s[key] = value
    return save_settings(s)

def get_active_provider_key():
    s = load_settings()
    return s.get("sms_provider", "simxpro")

def get_active_provider_info():
    s = load_settings()
    prov_key = s.get("sms_provider", "simxpro")
    providers = s.get("providers", {})
    return providers.get(prov_key, DEFAULT_PROVIDERS.get(prov_key, DEFAULT_PROVIDERS["simxpro"]))

def set_provider_key(prov_key: str, api_key: str):
    s = load_settings()
    if "providers" not in s:
        s["providers"] = {}
    if prov_key not in s["providers"] and prov_key in DEFAULT_PROVIDERS:
        s["providers"][prov_key] = DEFAULT_PROVIDERS[prov_key].copy()
    
    clean_key = (api_key or "").strip()
    if prov_key in s["providers"]:
        s["providers"][prov_key]["api_key"] = clean_key
    
    if prov_key == s.get("sms_provider"):
        s["sms_api_key"] = clean_key
        
    return save_settings(s)

def set_active_provider(prov_key: str):
    s = load_settings()
    if prov_key in s.get("providers", {}) or prov_key in DEFAULT_PROVIDERS:
        s["sms_provider"] = prov_key
        prov_info = s.get("providers", {}).get(prov_key, DEFAULT_PROVIDERS.get(prov_key, {}))
        if prov_info.get("api_key"):
            s["sms_api_key"] = prov_info["api_key"]
        return save_settings(s)
    return False

def increment_stat(stat_name, amount=1):
    s = load_settings()
    stats = s.get("stats", {})
    stats[stat_name] = stats.get(stat_name, 0) + amount
    s["stats"] = stats
    save_settings(s)

def is_admin(user_id):
    s = load_settings()
    admins = s.get("admin_ids", [])
    if not admins:
        return True
    return int(user_id) in [int(a) for a in admins]

def add_admin(user_id):
    s = load_settings()
    admins = s.get("admin_ids", [])
    if int(user_id) not in [int(a) for a in admins]:
        admins.append(int(user_id))
        s["admin_ids"] = admins
        save_settings(s)
    return True

SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_session.json")
SAVED_SESSIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved_sessions.json")
MASTER_SESSIONS_TXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_all_claimed_sessions.txt")

def save_last_session(phone: str, token_id: str):
    import datetime
    data = {
        "phone": phone,
        "token_id": token_id,
        "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return True
    except Exception as e:
        print(f"Error saving session: {e}")
        return False

def save_successful_session(phone: str, token_id: str, gemini_link: str = "", direct_google_url: str = "", source: str = "Auto-Claimer"):
    """
    Permanently records every successful claim session in:
    1. last_session.json (for quick single session reuse)
    2. saved_sessions.json (structured database of all claimed accounts)
    3. gemini_all_claimed_sessions.txt (master persistent human & machine-readable file)
    """
    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Update last single session
    save_last_session(phone, token_id)

    # 2. Append/Update in saved_sessions.json
    sessions = []
    if os.path.exists(SAVED_SESSIONS_FILE):
        try:
            with open(SAVED_SESSIONS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                sessions = loaded if isinstance(loaded, list) else []
        except Exception:
            sessions = []

    entry = {
        "phone": phone,
        "token_id": token_id,
        "gemini_link": gemini_link,
        "direct_google_url": direct_google_url,
        "saved_at": now_str,
        "source": source
    }

    # Deduplicate by token_id or phone
    existing_idx = None
    for idx, s in enumerate(sessions):
        if s.get("token_id") == token_id or (phone and s.get("phone") == phone):
            existing_idx = idx
            break

    if existing_idx is not None:
        sessions[existing_idx] = entry
    else:
        sessions.append(entry)

    try:
        with open(SAVED_SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error writing saved_sessions.json: {e}")

    # 3. Append to master sessions text file
    try:
        file_exists = os.path.exists(MASTER_SESSIONS_TXT)
        with open(MASTER_SESSIONS_TXT, "a", encoding="utf-8") as f:
            if not file_exists:
                f.write("# ========================================================\n")
                f.write("# MyIM3 Google Gemini 6M Master Claimed Sessions Database\n")
                f.write("# Format: [Date] Phone | Gemini Link | Token\n")
                f.write("# ========================================================\n\n")
            f.write(f"[{now_str}] Phone: +{phone} | Link: {gemini_link} | Token: {token_id}\n")
    except Exception as e:
        print(f"Error logging to master sessions txt: {e}")

    return entry

def get_all_saved_sessions() -> list:
    """Returns all persistently saved sessions (newest first)."""
    if not os.path.exists(SAVED_SESSIONS_FILE):
        return []
    try:
        with open(SAVED_SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return list(reversed(data))
            return []
    except Exception as e:
        print(f"Error loading saved sessions: {e}")
        return []

def get_saved_sessions_count() -> int:
    return len(get_all_saved_sessions())

def format_sessions_export(sessions_list: list, title: str = "Claimed Gemini Account Sessions") -> str:
    """Formats a list of session objects into a clean, professional .txt report with copyable tokens."""
    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "=" * 60,
        f"🤖 MyIM3 Google Gemini 6M - {title}",
        f"Generated: {now_str}",
        f"Total Accounts: {len(sessions_list)}",
        "=" * 60,
        ""
    ]

    for idx, item in enumerate(sessions_list, 1):
        phone = item.get("phone", "N/A")
        token = item.get("token_id", "")
        link = item.get("gemini_link", "")
        g_url = item.get("direct_google_url", "")
        saved_at = item.get("saved_at", now_str)

        lines.append(f"[{idx}] Account: +{phone}")
        lines.append(f"• Saved At: {saved_at}")
        if link:
            lines.append(f"• Gemini Short Link: {link}")
        if g_url:
            lines.append(f"• Google One Direct: {g_url}")
        lines.append("• Session Token (JWT):")
        lines.append(token)
        lines.append("-" * 60)

    lines.append("")
    lines.append("# Machine-Readable Quick Copy (Phone : Token : Link):")
    for item in sessions_list:
        p = item.get("phone", "")
        t = item.get("token_id", "")
        l = item.get("gemini_link", "")
        lines.append(f"{p}:{t}:{l}")

    return "\n".join(lines) + "\n"

def get_last_session():
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading session: {e}")
        return None

def clear_last_session():
    try:
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        return True
    except Exception:
        return False
