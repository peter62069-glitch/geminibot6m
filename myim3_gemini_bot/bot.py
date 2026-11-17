import os
import sys
import re
import json
import time
import html
import signal
import datetime
import threading
import http.server
import socketserver
import telebot
from telebot import types

# Handle clean shutdown on Render container restarts / deployments
def handle_sigterm(signum, frame):
    print("🛑 Received termination signal from host/Render. Shutting down bot gracefully...", flush=True)
    try:
        bot.stop_polling()
    except Exception:
        pass
    os._exit(0)

try:
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
except Exception:
    pass

import config
import batch_manager
from im3_api import MyIM3Client, format_msisdn, is_indosat_prefix, verify_gemini_link
from sms_service import SMSService

# Initialize clients
im3_client = MyIM3Client()

def get_sms_service(provider_key: str = None) -> SMSService:
    settings = config.load_settings()
    prov_key = provider_key or settings.get("sms_provider", "simxpro")
    providers = settings.get("providers", {})
    provider_info = providers.get(prov_key, config.DEFAULT_PROVIDERS.get(prov_key, {}))
    url = provider_info.get("url", "https://simxpro.org/api/stubs/handler_api.php")
    api_key = provider_info.get("api_key") or (settings.get("sms_api_key", "") if prov_key == "simxpro" else "")
    return SMSService(provider_url=url, api_key=api_key)

# Bot token from settings
BOT_TOKEN = config.get_setting("bot_token")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Active user sessions for multi-step dialogues
user_states = {}

# Active running auto-claimer jobs: {chat_id: {"stop": False, "thread": ...}}
active_claimers = {}

# Active running bulk session jobs: {chat_id: {"stop": False, "thread": ...}}
active_bulk_logins = {}

# Master claims log file
MASTER_CLAIMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_all_claimed_links.txt")

# Register native Telegram persistent menu commands
try:
    bot.set_my_commands([
        types.BotCommand("start", "🤖 Open Main Menu"),
        types.BotCommand("session", "🔑 Session Login (Single / Bulk)"),
        types.BotCommand("sessions", "💾 Download All Saved Sessions (.txt)"),
        types.BotCommand("balance", "💰 Check Live SMS Balance"),
        types.BotCommand("providers", "📡 SMS Providers & Custom Keys"),
        types.BotCommand("claims", "📁 Download Claimed Links (.txt)"),
        types.BotCommand("cancel", "❌ Cancel Current Action")
    ])
except Exception as e:
    print(f"Warning setting bot commands: {e}")

# --- HELPER KEYBOARDS ---

def get_main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    # Left column: All Green (style="success")
    # Right column: All Blue (style="primary")
    # Bottom button: Blue (style="primary")
    b1 = types.InlineKeyboardButton("📱 Manual Login", callback_data="btn_manual_login", style="success")
    b2 = types.InlineKeyboardButton("⚡ Auto-Claimer (SMS API)", callback_data="btn_auto_login", style="primary")
    b3 = types.InlineKeyboardButton("🔑 Session Login", callback_data="btn_session_login", style="success")
    b4 = types.InlineKeyboardButton("📋 View / Copy Session", callback_data="btn_view_session", style="primary")
    b5 = types.InlineKeyboardButton("📁 Download Links (.txt)", callback_data="btn_download_txt", style="success")
    b6 = types.InlineKeyboardButton("💰 Check Live Balance", callback_data="btn_check_balance", style="primary")
    b7 = types.InlineKeyboardButton("📡 SMS Providers & Keys", callback_data="btn_providers", style="primary")
    markup.add(b1, b2)
    markup.add(b3, b4)
    markup.add(b5, b6)
    markup.add(b7)
    return markup

def get_cancel_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="btn_cancel", style="danger"))
    return markup

# --- COMMAND HANDLERS ---

@bot.message_handler(commands=['start'])
def handle_start(message):
    user_states.pop(message.chat.id, None)
    settings = config.load_settings()
    active_prov_key = settings.get("sms_provider", "simxpro")
    prov_info = settings.get("providers", {}).get(active_prov_key, config.DEFAULT_PROVIDERS.get(active_prov_key, {}))
    prov_name = prov_info.get("name", active_prov_key)
    charges = prov_info.get("charges_info", "Standard rates")
    has_key = "🟢 Connected" if prov_info.get("api_key") else "🔴 Key Not Set"

    welcome_text = (
        "🤖 <b>Welcome to MyIM3 Google Gemini 6M Automation Bot!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "This bot authenticates with Indosat Ooredoo (myIM3) to claim and generate "
        "<b>Google Gemini AI Plus (6 Months Free / 400GB)</b> promo links.\n\n"
        f"📡 <b>Active SMS Provider:</b> <code>{html.escape(prov_name)}</code> ({has_key})\n"
        f"🏷 <b>myIM3 Charges:</b> <code>{html.escape(charges)}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <i>Select an option below to proceed:</i>"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard())

@bot.message_handler(commands=['session'])
def handle_session_cmd(message):
    show_session_view(message.chat.id)

@bot.message_handler(commands=['bulk'])
def handle_bulk_cmd(message):
    start_session_login(message.chat.id)

@bot.message_handler(commands=['sessions'])
def handle_sessions_cmd(message):
    send_all_saved_sessions_file(message.chat.id)

@bot.message_handler(commands=['balance'])
def handle_balance_cmd(message):
    check_balance_action(message.chat.id)

@bot.message_handler(commands=['providers'])
def handle_providers_cmd(message):
    show_providers_menu(message.chat.id)

@bot.message_handler(commands=['claims'])
def handle_claims_cmd(message):
    show_batch_selection_menu(message.chat.id)

@bot.message_handler(commands=['admin'])
def handle_admin_cmd(message):
    user_id = message.from_user.id
    settings = config.load_settings()
    admins = settings.get("admin_ids", [])

    if not admins:
        # First time setup: allow user to claim master admin
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("👑 Claim Master Admin", callback_data="adm_claim_self"))
        bot.send_message(
            message.chat.id,
            "👑 <b>Admin Panel Setup:</b>\n\nNo administrators are registered yet.\nClick below to register yourself as Master Admin.",
            reply_markup=markup
        )
        return

    if not config.is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ <b>Access Denied:</b> You are not authorized.")
        return

    show_admin_panel(message.chat.id)

@bot.message_handler(commands=['cancel'])
def handle_cancel_cmd(message):
    chat_id = message.chat.id
    user_states.pop(chat_id, None)
    if chat_id in active_claimers:
        active_claimers[chat_id]["stop"] = True
    bot.send_message(chat_id, "❌ Action has been cancelled.", reply_markup=get_main_keyboard())

# --- CALLBACK QUERY HANDLER ---

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    chat_id = call.message.chat.id
    user_id = call.from_user.id
    msg_id = call.message.message_id
    data = call.data

    if data == "btn_back_main":
        user_states.pop(chat_id, None)
        try:
            bot.edit_message_text(
                "🤖 <b>Main Menu</b>\n\nPlease select an option:",
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=get_main_keyboard()
            )
        except Exception:
            bot.send_message(chat_id, "🤖 <b>Main Menu</b>", reply_markup=get_main_keyboard())

    elif data == "btn_cancel":
        user_states.pop(chat_id, None)
        if chat_id in active_claimers:
            active_claimers[chat_id]["stop"] = True
        if chat_id in active_bulk_logins:
            active_bulk_logins[chat_id]["stop"] = True
        try:
            bot.edit_message_text("❌ Current process cancelled.", chat_id=chat_id, message_id=msg_id, reply_markup=get_main_keyboard())
        except Exception:
            bot.send_message(chat_id, "❌ Current process cancelled.", reply_markup=get_main_keyboard())

    elif data == "btn_stop_bulk":
        if chat_id in active_bulk_logins:
            active_bulk_logins[chat_id]["stop"] = True
            bot.answer_callback_query(call.id, "🛑 Stopping bulk session claimer...")
        else:
            bot.answer_callback_query(call.id, "No active bulk session running.")

    elif data == "btn_stop_claimer":
        if chat_id in active_claimers:
            active_claimers[chat_id]["stop"] = True
            bot.answer_callback_query(call.id, "🛑 Stopping auto-claimer...")
            try:
                bot.edit_message_text(
                    "🛑 <b>Stopping Auto-Claimer...</b>\n\nHalting engine, releasing numbers, and compiling session report. Please wait...",
                    chat_id=chat_id,
                    message_id=msg_id
                )
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "Auto-claimer is already stopped.")
            try:
                bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
            except Exception:
                pass

    elif data == "btn_check_balance":
        check_balance_action(chat_id)

    elif data == "btn_download_txt":
        show_batch_selection_menu(chat_id, edit_message_id=msg_id)

    elif data.startswith("view_batch_"):
        batch_id = data.replace("view_batch_", "")
        handle_batch_download_single(chat_id, batch_id)

    elif data == "dl_batch_all":
        handle_batch_download_all(chat_id)

    elif data == "btn_view_session":
        show_session_view(chat_id, edit_message_id=msg_id)

    elif data == "btn_clear_session":
        config.clear_last_session()
        bot.answer_callback_query(call.id, "🗑️ Saved session cleared!", show_alert=True)
        show_session_view(chat_id, edit_message_id=msg_id)

    elif data == "btn_claim_from_session":
        claim_from_saved_session(chat_id)

    elif data == "btn_bulk_session_login":
        start_session_login(chat_id)

    elif data == "btn_download_sessions":
        send_all_saved_sessions_file(chat_id)

    elif data == "btn_providers" or data == "btn_settings":
        show_providers_menu(chat_id, edit_message_id=msg_id)

    elif data.startswith("manage_prov_"):
        prov_key = data.replace("manage_prov_", "")
        show_provider_details(chat_id, prov_key, edit_message_id=msg_id)

    elif data.startswith("activate_prov_"):
        prov_key = data.replace("activate_prov_", "")
        config.set_active_provider(prov_key)
        settings = config.load_settings()
        p_info = settings.get("providers", {}).get(prov_key, {})
        prov_name = p_info.get("name", prov_key)
        bot.answer_callback_query(call.id, f"✅ Active Provider set to: {prov_name}!")
        show_provider_details(chat_id, prov_key, edit_message_id=msg_id)

    elif data.startswith("setkey_prov_"):
        prov_key = data.replace("setkey_prov_", "")
        settings = config.load_settings()
        p_info = settings.get("providers", {}).get(prov_key, config.DEFAULT_PROVIDERS.get(prov_key, {}))
        name = p_info.get("name", prov_key)
        domain = p_info.get("domain", "")

        user_states[chat_id] = {
            "action": "WAITING_PROVIDER_API_KEY",
            "provider_key": prov_key
        }

        bot.send_message(
            chat_id,
            f"🔑 <b>Enter Custom API Key for {html.escape(name)} ({html.escape(domain)}):</b>\n\n"
            f"1. Login to <code>https://{html.escape(domain)}</code>\n"
            "2. Navigate to your Profile or API Settings page.\n"
            "3. Copy your API Key and paste it here in the chat.\n\n"
            "<i>(Send /cancel to return to menu)</i>",
            reply_markup=get_cancel_keyboard()
        )

    elif data.startswith("checkbal_prov_"):
        prov_key = data.replace("checkbal_prov_", "")
        sms = get_sms_service(prov_key)
        b_res = sms.get_balance()
        p_info = config.load_settings().get("providers", {}).get(prov_key, {})
        name = p_info.get("name", prov_key)
        if b_res["success"]:
            bot.answer_callback_query(call.id, f"💰 {name} Balance: ₹{b_res['balance']:.2f}", show_alert=True)
        else:
            bot.answer_callback_query(call.id, f"❌ {name}: {b_res['message'][:60]}", show_alert=True)
        show_provider_details(chat_id, prov_key, edit_message_id=msg_id)

    elif data == "btn_manual_login":
        start_manual_login(chat_id)

    elif data == "btn_session_login":
        start_session_login(chat_id)

    elif data == "btn_auto_login":
        start_auto_login_flow(chat_id, edit_message_id=msg_id)

    elif data.startswith("select_op_"):
        op_id = data.replace("select_op_", "")
        validate_balance_and_select_op(chat_id, op_id, edit_message_id=msg_id)

    elif data.startswith("force_claim_"):
        op_id = data.replace("force_claim_", "")
        show_quantity_selector(chat_id, op_id, edit_message_id=msg_id)

    elif data.startswith("claim_qty_"):
        payload = data[len("claim_qty_"):]
        if "_" in payload:
            op_or_mode, qty_str = payload.rsplit("_", 1)
        else:
            op_or_mode = "direct"
            qty_str = payload

        if qty_str == "custom":
            user_states[chat_id] = {
                "action": "WAITING_CUSTOM_QTY",
                "operator": op_or_mode if op_or_mode != "direct" else None
            }
            bot.send_message(chat_id, "🔢 <b>Enter desired quantity of valid Gemini links to claim:</b>\n(e.g., 1, 3, 5)", reply_markup=get_cancel_keyboard())
        else:
            try:
                qty = int(qty_str)
            except ValueError:
                qty = 1
            op_arg = op_or_mode if op_or_mode != "direct" else None
            start_auto_claimer_execution(chat_id, operator=op_arg, target_claims=qty)

    # --- ADMIN CALLBACKS (Hidden from main UI) ---
    elif data == "adm_claim_self":
        config.add_admin(user_id)
        bot.answer_callback_query(call.id, "You are now Master Admin!")
        show_admin_panel(chat_id, edit_message_id=msg_id)

    elif data == "adm_stats":
        show_admin_stats(chat_id, edit_message_id=msg_id)

    elif data == "adm_download_log":
        send_master_log_file(chat_id)

# --- BALANCE & PROVIDERS MANAGEMENT ---

def check_balance_action(chat_id):
    settings = config.load_settings()
    active_prov = settings.get("sms_provider", "simxpro")
    providers = settings.get("providers", {})
    p_info = providers.get(active_prov, config.DEFAULT_PROVIDERS.get(active_prov, {}))
    name = p_info.get("name", active_prov)
    domain = p_info.get("domain", "")
    charges = p_info.get("charges_info", "N/A")

    sms = get_sms_service(active_prov)
    bot.send_chat_action(chat_id, "typing")
    res = sms.get_balance()

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🔄 Refresh Live Balance", callback_data="btn_check_balance", style="primary"),
        types.InlineKeyboardButton("📡 Manage Providers & Keys", callback_data="btn_providers", style="primary"),
        types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger")
    )

    if res["success"]:
        text = (
            f"💰 <b>Live SMS Account Balance</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 <b>Active Provider:</b> <code>{html.escape(name)} ({html.escape(domain)})</code>\n"
            f"✅ <b>Current Balance:</b> <code>₹{res['balance']:.2f}</code>\n"
            f"🏷 <b>myIM3 Charges:</b> <code>{html.escape(charges)}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Your balance is ready for Auto-Claimer!</i>"
        )
    else:
        text = (
            f"❌ <b>Balance Check Failed ({html.escape(name)}):</b>\n\n"
            f"<code>{html.escape(res['message'])}</code>\n\n"
            f"💡 <i>Please verify your API key for {html.escape(name)} in the Providers menu.</i>"
        )
    bot.send_message(chat_id, text, reply_markup=markup)

def show_providers_menu(chat_id, edit_message_id=None):
    settings = config.load_settings()
    active_prov = settings.get("sms_provider", "simxpro")
    providers = settings.get("providers", {})

    text = (
        "📡 <b>SMS Gateway Providers & Custom API Keys</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select a provider to view its Indosat (myIM3) charges, check live balance, or add your custom API Key:\n\n"
    )

    prov_icons = {
        "simxpro": "⚡",
        "grizzlysms": "🐻",
        "nexnum": "🚀",
        "uotp": "📱"
    }

    # Order of providers: Simxpro, Grizzly SMS, NexNum.in, uOTP.store
    p_keys = ["simxpro", "grizzlysms", "nexnum", "uotp"]
    markup = types.InlineKeyboardMarkup(row_width=1)

    for p_key in p_keys:
        p_info = providers.get(p_key, config.DEFAULT_PROVIDERS.get(p_key, {}))
        name = p_info.get("name", p_key)
        domain = p_info.get("domain", "")
        charges = p_info.get("charges_info", "N/A")
        key = p_info.get("api_key", "")
        is_active = (p_key == active_prov)

        status_tag = "🟢 <b>[ACTIVE]</b>" if is_active else "⚪"
        key_tag = "✅ Key Saved" if key else "❌ No Key"

        text += (
            f"{prov_icons.get(p_key, '📡')} <b>{html.escape(name)}</b> (<code>{html.escape(domain)}</code>)\n"
            f"   • Status: {status_tag} | {key_tag}\n"
            f"   • myIM3 Rate: <code>{html.escape(charges)}</code>\n\n"
        )

        active_label = f"⭐ {name} (Active)" if is_active else f"{prov_icons.get(p_key, '📡')} Manage {name}"
        active_style = "success" if is_active else "primary"
        markup.add(types.InlineKeyboardButton(active_label, callback_data=f"manage_prov_{p_key}", style=active_style))

    markup.add(types.InlineKeyboardButton("🔙 Back to Main Menu", callback_data="btn_back_main", style="danger"))

    text += "<i>Click a provider below to configure its API Key, check balance, or make it Active:</i>"

    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup)

def show_provider_details(chat_id, prov_key, edit_message_id=None):
    settings = config.load_settings()
    active_prov = settings.get("sms_provider", "simxpro")
    providers = settings.get("providers", {})
    p_info = providers.get(prov_key, config.DEFAULT_PROVIDERS.get(prov_key, {}))

    name = p_info.get("name", prov_key)
    domain = p_info.get("domain", "")
    charges = p_info.get("charges_info", "N/A")
    raw_key = p_info.get("api_key", "")
    is_active = (prov_key == active_prov)

    key_masked = (raw_key[:6] + "..." + raw_key[-4:]) if len(raw_key) > 10 else ("Configured" if raw_key else "Not set")

    bal_str = "Key not configured"
    if raw_key:
        sms = get_sms_service(prov_key)
        b_res = sms.get_balance()
        if b_res["success"]:
            bal_str = f"₹{b_res['balance']:.2f}"
        else:
            bal_str = f"Error: {b_res['message'][:40]}"

    text = (
        f"📡 <b>Provider: {html.escape(name)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Official Website:</b> <code>https://{html.escape(domain)}</code>\n"
        f"🏷 <b>myIM3 (Indosat) Charges:</b> <code>{html.escape(charges)}</code>\n"
        f"🔑 <b>API Key:</b> <code>{html.escape(key_masked)}</code>\n"
        f"💰 <b>Live Balance:</b> <code>{html.escape(bal_str)}</code>\n"
        f"⚡ <b>Auto-Claimer Status:</b> {'🟢 ACTIVE PROVIDER' if is_active else '⚪ Inactive'}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Select an action below:</i>"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton(f"🔑 Set / Change {name} API Key", callback_data=f"setkey_prov_{prov_key}", style="primary"))

    if not is_active:
        markup.add(types.InlineKeyboardButton(f"✅ Set {name} as Active Provider", callback_data=f"activate_prov_{prov_key}", style="success"))

    if raw_key:
        markup.add(types.InlineKeyboardButton(f"💰 Refresh {name} Balance", callback_data=f"checkbal_prov_{prov_key}", style="primary"))

    markup.add(types.InlineKeyboardButton("🔙 Back to All Providers", callback_data="btn_providers", style="primary"))
    markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger"))

    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup)

# --- SESSION VIEW & QUICK CLAIM HELPERS ---

def show_session_view(chat_id, edit_message_id=None):
    sess = config.get_last_session()
    saved_count = config.get_saved_sessions_count()
    markup = types.InlineKeyboardMarkup(row_width=1)

    if not sess or not sess.get("token_id"):
        markup.add(
            types.InlineKeyboardButton("🔑 Login via Session Token", callback_data="btn_session_login", style="primary"),
            types.InlineKeyboardButton("📱 Manual OTP Login", callback_data="btn_manual_login", style="success")
        )
        if saved_count > 0:
            markup.add(
                types.InlineKeyboardButton(f"💾 Download All Saved Sessions ({saved_count})", callback_data="btn_download_sessions", style="success")
            )
        markup.add(
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger")
        )
        text = (
            "📋 <b>Saved myIM3 Login Sessions</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "❌ <i>No active single session loaded.</i>\n\n"
            f"📦 <b>Total Persistently Saved Accounts:</b> <code>{saved_count}</code>\n\n"
            "💡 <b>How sessions are saved:</b>\n"
            "• Log in using <b>📱 Manual Login</b>\n"
            "• Or paste an existing token via <b>🔑 Session Login</b> (single or multiple tokens)\n"
            "• Or run the <b>⚡ Auto-Claimer</b> (auto-saves every successful account!)\n\n"
            "All sessions are permanently preserved in your sessions database!"
        )
    else:
        phone = sess.get("phone", "N/A")
        token_id = sess.get("token_id", "")
        saved_time = sess.get("saved_at", sess.get("timestamp", "Recent"))

        prof = im3_client.get_profile(token_id)
        is_valid = (str(prof.get("status", "")) == "0")
        status_text = "🟢 <b>ACTIVE & VALID</b>" if is_valid else "🔴 <b>EXPIRED / INVALID</b>"

        text = (
            "📋 <b>Active myIM3 Login Session</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Phone:</b> <code>+{html.escape(phone)}</code>\n"
            f"⚡ <b>Session Status:</b> {status_text}\n"
            f"🕒 <b>Saved Time:</b> <code>{html.escape(saved_time)}</code>\n"
            f"📦 <b>Total Saved Accounts:</b> <code>{saved_count}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔑 <b>Session Token (Tap to Copy):</b>\n\n"
            f"<code>{html.escape(token_id)}</code>\n\n"
            "💡 <i>In Telegram, tap directly on the code box above to copy the full token to your clipboard.</i>"
        )

        markup.add(
            types.InlineKeyboardButton("🎁 Claim Gemini from Session", callback_data="btn_claim_from_session", style="success"),
            types.InlineKeyboardButton(f"💾 Download All Saved Sessions ({saved_count})", callback_data="btn_download_sessions", style="success"),
            types.InlineKeyboardButton("🔄 Refresh & Re-verify Session", callback_data="btn_view_session", style="primary"),
            types.InlineKeyboardButton("🗑️ Clear Single Saved Session", callback_data="btn_clear_session", style="danger"),
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="primary")
        )

    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup)

def claim_from_saved_session(chat_id):
    sess = config.get_last_session()
    if not sess or not sess.get("token_id"):
        bot.send_message(chat_id, "⚠️ No active saved session found! Please log in first.", reply_markup=get_main_keyboard())
        return

    token_id = sess.get("token_id")
    phone = sess.get("phone", "")

    wait_msg = bot.send_message(chat_id, "🔐 <i>Validating saved session and generating Google Gemini 6M offer...</i>")

    offer_data = im3_client.get_gemini_details(token_id, phone=phone)
    if offer_data.get("is_verified") and offer_data.get("gemini_link"):
        send_single_gemini_success(chat_id, offer_data)
        record_claim_to_master_log(offer_data)
        batch_manager.create_batch([offer_data["gemini_link"]], phones=[phone], label="Saved Session")
        config.increment_stat("total_claims", 1)
        config.save_successful_session(
            phone=phone or offer_data.get("phone", ""),
            token_id=token_id,
            gemini_link=offer_data.get("gemini_link", ""),
            direct_google_url=offer_data.get("direct_google_url", ""),
            source="Saved Session Claim"
        )
        try:
            bot.delete_message(chat_id, wait_msg.message_id)
        except Exception:
            pass
    else:
        bot.edit_message_text(
            "❌ <b>Google Gemini Offer Not Available for this Session!</b>\n\n"
            f"📱 <b>Phone:</b> <code>+{html.escape(phone or 'N/A')}</code>\n"
            "IOH Gateway response: <code>URL not available</code>\n\n"
            "💡 <i>This account does not currently have an active Google Gemini promo benefit on Indosat.</i>",
            chat_id=chat_id,
            message_id=wait_msg.message_id,
            reply_markup=get_main_keyboard()
        )

# --- MANUAL LOGIN LOGIC WITH DIAGNOSTICS ---

def start_manual_login(chat_id):
    user_states[chat_id] = {"action": "WAITING_MANUAL_PHONE"}
    bot.send_message(
        chat_id,
        "📱 <b>Manual Login Mode</b>\n\n"
        "Please send your registered Indosat (myIM3) phone number:\n\n"
        "<b>Accepted Formats:</b>\n"
        "• <code>+6285712345678</code>\n"
        "• <code>628571234567</code>\n"
        "• <code>085712345678</code>\n\n"
        "<i>(Prefixes must be official Indosat: 0814, 0815, 0816, 0855, 0856, 0857, 0858, 0895, 0896, 0897, 0898, 0899)</i>\n\n"
        "(Send /cancel to return to menu)",
        reply_markup=get_cancel_keyboard()
    )

def handle_manual_phone(message):
    chat_id = message.chat.id
    phone_input = message.text.strip()
    formatted = format_msisdn(phone_input)

    if not is_indosat_prefix(formatted):
        bot.send_message(
            chat_id,
            f"❌ <b>Non-Indosat Number Detected!</b>\n\n"
            f"Number <code>+{html.escape(formatted)}</code> does not match official Indosat prefixes.\n"
            "myIM3 only accepts genuine Indosat SIM cards. Please enter a valid Indosat number:",
            reply_markup=get_cancel_keyboard()
        )
        return

    wait_msg = bot.send_message(chat_id, f"⏳ <i>Sending OTP to myIM3 for number <code>+{html.escape(formatted)}</code>...</i>")
    res = im3_client.send_otp(formatted)

    if res["success"]:
        transid = res["transid"]
        user_states[chat_id] = {
            "action": "WAITING_MANUAL_OTP",
            "transid": transid,
            "phone": formatted
        }
        bot.edit_message_text(
            f"✅ <b>OTP Sent Successfully!</b> 📩\n\n"
            f"📱 <b>Phone:</b> <code>+{html.escape(formatted)}</code>\n"
            f"🆔 <b>Transaction ID:</b> <code>{html.escape(transid)}</code>\n\n"
            "An SMS verification code has been dispatched by Indosat.\n"
            "👉 <b>Please enter the 6-digit OTP code below:</b>",
            chat_id=chat_id,
            message_id=wait_msg.message_id,
            reply_markup=get_cancel_keyboard()
        )
    else:
        user_states.pop(chat_id, None)
        err_code = html.escape(str(res.get("code", "Unknown")))
        err_msg = html.escape(str(res.get("message", "Request failed")))
        bot.edit_message_text(
            f"❌ <b>myIM3 Login Error!</b>\n\n"
            f"<b>Status Code:</b> <code>{err_code}</code>\n"
            f"<b>Server Message:</b> {err_msg}\n\n"
            "💡 <i>Diagnostics: Number may not be active on Indosat cellular network, or you can use Session Login directly.</i>",
            chat_id=chat_id,
            message_id=wait_msg.message_id,
            reply_markup=get_main_keyboard()
        )

def handle_manual_otp(message):
    chat_id = message.chat.id
    otp_code = message.text.strip().replace(" ", "")
    session = user_states.get(chat_id, {})
    transid = session.get("transid", "")
    phone = session.get("phone", "")

    if not otp_code.isdigit() or len(otp_code) < 4:
        bot.send_message(chat_id, "⚠️ <b>Invalid OTP!</b> Please send the numeric verification code.")
        return

    wait_msg = bot.send_message(chat_id, "🔐 <i>Verifying OTP with myIM3...</i>")
    res = im3_client.validate_otp(transid, otp_code)

    if res["success"]:
        token_id = res["token_id"]
        config.save_last_session(phone, token_id)
        bot.edit_message_text(
            "✅ <b>Login Successful!</b>\n🎁 Generating Google Gemini 6M Offer Link...",
            chat_id=chat_id,
            message_id=wait_msg.message_id
        )
        offer_data = im3_client.get_gemini_details(token_id, phone=phone)
        if offer_data.get("is_verified") and offer_data.get("gemini_link"):
            send_single_gemini_success(chat_id, offer_data)
            record_claim_to_master_log(offer_data)
            batch_manager.create_batch([offer_data["gemini_link"]], phones=[phone], label="Manual Claim")
            config.increment_stat("total_claims", 1)
            config.save_successful_session(
                phone=phone or offer_data.get("phone", ""),
                token_id=token_id,
                gemini_link=offer_data.get("gemini_link", ""),
                direct_google_url=offer_data.get("direct_google_url", ""),
                source="Manual OTP Login"
            )
            user_states.pop(chat_id, None)
        else:
            user_states.pop(chat_id, None)
            bot.edit_message_text(
                "❌ <b>Google Gemini Offer Not Available for this Number!</b>\n\n"
                f"📱 <b>Phone:</b> <code>+{html.escape(phone)}</code>\n"
                "IOH Gateway response: <code>URL not available</code>\n\n"
                "💡 <i>This SIM card has not been provisioned with the Google Gemini AI Plus 6M benefit by Indosat. Please use an IM3 number that has the Gemini promo starter pack.</i>",
                chat_id=chat_id,
                message_id=wait_msg.message_id,
                reply_markup=get_main_keyboard()
            )
    else:
        err_code = html.escape(str(res.get("code", "Unknown")))
        err_msg = html.escape(str(res.get("message", "Verification failed")))
        bot.edit_message_text(
            f"❌ <b>Verification Failed!</b>\n\n"
            f"<b>Code:</b> <code>{err_code}</code>\n"
            f"<b>Message:</b> {err_msg}\n\n"
            "Please check the OTP code and enter again, or send /cancel.",
            chat_id=chat_id,
            message_id=wait_msg.message_id,
            reply_markup=get_cancel_keyboard()
        )

# --- SESSION TOKEN LOGIN LOGIC ---

def start_session_login(chat_id):
    user_states[chat_id] = {"action": "WAITING_SESSION_TOKEN"}
    text = (
        "🔑 <b>Session Token Login Mode</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Paste a <b>single session token</b> OR <b>multiple session tokens</b> at once (one per line, or <code>phone:token</code>).\n"
        "You can also upload a <code>.txt</code> file containing your tokens!\n\n"
        "💡 <b>Supported Formats:</b>\n"
        "• Single token: <code>eyJhbGciOi...</code>\n"
        "• Multiple tokens: 1 per line\n"
        "• Phone + Token: <code>6285712345678:eyJhbGciOi...</code>\n"
        "• Full format: <code>+628... | eyJ... : https://...</code>\n\n"
        "<i>Paste your token(s) or upload a .txt file below (or send /cancel to return to menu):</i>"
    )
    bot.send_message(chat_id, text, reply_markup=get_cancel_keyboard())

def handle_session_token(message):
    chat_id = message.chat.id
    raw_text = message.text.strip()

    tokens = parse_bulk_tokens(raw_text)
    if not tokens:
        cleaned = raw_text.replace('"', '').replace("'", "").strip()
        if len(cleaned) >= 20:
            tokens = [{"phone": "", "token": cleaned}]

    if not tokens:
        bot.send_message(
            chat_id,
            "⚠️ <b>Invalid Session Token!</b>\n\n"
            "Please paste a valid session token (starts with <code>eyJ...</code>) or send /cancel.",
            reply_markup=get_cancel_keyboard()
        )
        return

    # If multiple tokens were supplied at once:
    if len(tokens) > 1:
        user_states.pop(chat_id, None)
        process_bulk_session_input(chat_id, raw_text)
        return

    # Single token processing:
    token_item = tokens[0]
    token_input = token_item["token"]
    input_phone = token_item.get("phone", "")

    wait_msg = bot.send_message(chat_id, "🔐 <i>Validating myIM3 session token...</i>")
    prof = im3_client.get_profile(token_input)
    status = str(prof.get("status", ""))

    if status == "0":
        p_data = prof.get("data", {})
        phone_msisdn = input_phone or p_data.get("msisdn", "") or p_data.get("phone", "") or p_data.get("mob", "")
        config.save_last_session(phone_msisdn, token_input)
        try:
            bot.edit_message_text(
                "✅ <b>Session Token Valid!</b>\n🎁 Generating Google Gemini 6M Offer Link...",
                chat_id=chat_id,
                message_id=wait_msg.message_id
            )
        except Exception:
            pass

        offer_data = im3_client.get_gemini_details(token_input, phone=phone_msisdn)
        if offer_data.get("is_verified") and offer_data.get("gemini_link"):
            offer_data["token_id"] = token_input
            if phone_msisdn:
                offer_data["phone"] = phone_msisdn
            send_single_gemini_success(chat_id, offer_data)
            record_claim_to_master_log(offer_data)
            batch_manager.create_batch([offer_data["gemini_link"]], phones=[phone_msisdn], label="Session Claim")
            config.increment_stat("total_claims", 1)
            config.save_successful_session(
                phone=phone_msisdn or offer_data.get("phone", ""),
                token_id=token_input,
                gemini_link=offer_data.get("gemini_link", ""),
                direct_google_url=offer_data.get("direct_google_url", ""),
                source="Session Login"
            )
            user_states.pop(chat_id, None)
        else:
            user_states.pop(chat_id, None)
            try:
                bot.edit_message_text(
                    "❌ <b>Google Gemini Offer Not Available for this Session!</b>\n\n"
                    f"📱 <b>Phone:</b> <code>+{html.escape(phone_msisdn or 'N/A')}</code>\n"
                    "IOH Gateway response: <code>URL not available</code>\n\n"
                    "💡 <i>This account does not currently have an active Google Gemini promo benefit on Indosat.</i>",
                    chat_id=chat_id,
                    message_id=wait_msg.message_id,
                    reply_markup=get_main_keyboard()
                )
            except Exception:
                bot.send_message(
                    chat_id,
                    "❌ <b>Google Gemini Offer Not Available for this Session!</b>",
                    reply_markup=get_main_keyboard()
                )
    else:
        err_msg = prof.get("message") or "Session token is invalid or expired."
        err_code = prof.get("code") or status
        try:
            bot.edit_message_text(
                f"❌ <b>Session Verification Failed!</b>\n\n"
                f"<b>Code:</b> <code>{html.escape(str(err_code))}</code>\n"
                f"<b>Message:</b> {html.escape(str(err_msg))}\n\n"
                "The token may have expired. Please copy a fresh token from myIM3 and try again, or send /cancel.",
                chat_id=chat_id,
                message_id=wait_msg.message_id,
                reply_markup=get_cancel_keyboard()
            )
        except Exception:
            bot.send_message(
                chat_id,
                f"❌ <b>Session Verification Failed:</b> {html.escape(str(err_msg))}",
                reply_markup=get_cancel_keyboard()
            )

# --- BULK SESSION LOGIN & MULTI-ACCOUNT MANAGEMENT ---

def parse_bulk_tokens(text: str) -> list:
    """
    Extracts JWT tokens and optional phone numbers from raw text or uploaded files.
    Supports 1-per-line tokens, phone:token, phone:token:link, comma/pipe-separated lines.
    """
    results = []
    seen_tokens = set()
    lines = text.strip().splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        jwt_match = re.search(r'(eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+)', line)
        if not jwt_match:
            jwt_match = re.search(r'(eyJ[a-zA-Z0-9_\-\.]{25,})', line)
        if jwt_match:
            token = jwt_match.group(1).strip()
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            phone_match = re.search(r'(\+?62\d{8,13}|08\d{8,12}|\b\d{10,14}\b)', line.replace(token, ''))
            phone = phone_match.group(1).replace('+', '') if phone_match else ''
            results.append({'phone': phone, 'token': token})
        else:
            cleaned = line.replace('"', '').replace("'", '').strip()
            if len(cleaned) > 30 and '.' in cleaned:
                if cleaned not in seen_tokens:
                    seen_tokens.add(cleaned)
                    results.append({'phone': '', 'token': cleaned})
    return results

def render_bulk_dashboard(state: dict, current_status: str) -> str:
    total = state.get("total", 0)
    curr = state.get("current", 0)
    claimed = state.get("claimed", 0)
    ineligible = state.get("ineligible", 0)
    invalid = state.get("invalid", 0)

    pct = int((curr / total) * 100) if total > 0 else 0
    filled = int((curr / total) * 10) if total > 0 else 0
    bar = "🟩" * filled + "⬜" * (10 - filled)

    text = (
        "⚡ <b>Bulk Session Claimer Pipeline</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Progress:</b> [{bar}] {pct}% ({curr}/{total})\n"
        f"✅ <b>Successfully Claimed:</b> <code>{claimed}</code>\n"
        f"⚠️ <b>Ineligible (No Promo):</b> <code>{ineligible}</code>\n"
        f"🔴 <b>Expired / Invalid:</b> <code>{invalid}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 <b>Status:</b> <i>{html.escape(current_status)}</i>"
    )
    return text

def start_bulk_session_login(chat_id: int):
    if chat_id in active_bulk_logins and not active_bulk_logins[chat_id].get("stop"):
        bot.send_message(
            chat_id,
            "⚠️ <b>A bulk session process is already active!</b>\nPlease wait for it to finish or click <b>Stop</b>.",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("🛑 Stop Bulk Claimer", callback_data="btn_stop_bulk", style="danger")
            )
        )
        return

    user_states[chat_id] = {"action": "WAITING_BULK_SESSION_TOKENS"}
    text = (
        "⚡ <b>Bulk Session Login Mode</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Process multiple myIM3 accounts simultaneously and generate Google Gemini 6M promo links for each!\n\n"
        "📥 <b>Send tokens now in one of two ways:</b>\n"
        "1. <b>Paste tokens directly</b> into this chat (one token per line or <code>phone:token</code>).\n"
        "2. <b>Upload a <code>.txt</code> file</b> containing your list of session tokens.\n\n"
        "<b>Supported Formats:</b>\n"
        "• <code>eyJhbGciOi...</code>\n"
        "• <code>6285712345678:eyJhbGciOi...</code>\n"
        "• <code>+6285712345678 | eyJhbGciOi... : https://...</code>\n\n"
        "<i>Send your text or upload your .txt file now (or send /cancel to abort):</i>"
    )
    bot.send_message(chat_id, text, reply_markup=get_cancel_keyboard())

def process_bulk_session_input(chat_id: int, raw_text: str):
    tokens = parse_bulk_tokens(raw_text)
    if not tokens:
        bot.send_message(
            chat_id,
            "⚠️ <b>No valid session tokens detected!</b>\n\n"
            "Please ensure each token starts with <code>eyJ...</code> and paste again, or send /cancel.",
            reply_markup=get_cancel_keyboard()
        )
        return

    if chat_id in active_bulk_logins and not active_bulk_logins[chat_id].get("stop"):
        bot.send_message(chat_id, "⚠️ A bulk session job is already running for your chat!")
        return

    if chat_id in active_claimers and not active_claimers[chat_id].get("stop"):
        bot.send_message(chat_id, "⚠️ Auto-claimer SMS is currently running. Please stop it before starting bulk session login.")
        return

    user_states.pop(chat_id, None)
    th = threading.Thread(target=bulk_session_worker_thread, args=(chat_id, tokens), daemon=True)
    th.start()

def bulk_session_worker_thread(chat_id: int, token_items: list):
    state = {
        "stop": False,
        "total": len(token_items),
        "current": 0,
        "claimed": 0,
        "ineligible": 0,
        "invalid": 0,
        "successful_claims": []
    }
    active_bulk_logins[chat_id] = state

    stop_markup = types.InlineKeyboardMarkup()
    stop_markup.add(types.InlineKeyboardButton("🛑 Stop Bulk Processing", callback_data="btn_stop_bulk", style="danger"))

    dash_msg = bot.send_message(
        chat_id,
        render_bulk_dashboard(state, "🚀 Starting bulk session verification..."),
        reply_markup=stop_markup
    )
    msg_id = dash_msg.message_id
    last_ui_update = time.time()

    try:
        for idx, item in enumerate(token_items, 1):
            if state.get("stop", False):
                break

            state["current"] = idx
            token = item["token"]
            phone = item.get("phone", "")

            # 1. Profile / Validity check
            prof = im3_client.get_profile(token)
            if str(prof.get("status", "")) != "0":
                state["invalid"] += 1
                status_desc = f"Account {idx}: Invalid/Expired Token"
            else:
                if not phone:
                    p_data = prof.get("data", {})
                    phone = p_data.get("msisdn") or p_data.get("phone") or p_data.get("mob") or ""

                # 2. Get Gemini Promo details
                offer_data = im3_client.get_gemini_details(token, phone=phone)
                if offer_data.get("is_verified") and offer_data.get("gemini_link"):
                    offer_data["token_id"] = token
                    if phone:
                        offer_data["phone"] = phone
                    state["claimed"] += 1
                    state["successful_claims"].append(offer_data)
                    config.increment_stat("total_claims", 1)
                    record_claim_to_master_log(offer_data)
                    config.save_successful_session(
                        phone=phone or offer_data.get("phone", ""),
                        token_id=token,
                        gemini_link=offer_data.get("gemini_link", ""),
                        direct_google_url=offer_data.get("direct_google_url", ""),
                        source="Bulk Session Login"
                    )
                    status_desc = f"Account {idx}: 🎉 Gemini Claimed!"
                    send_single_gemini_success(chat_id, offer_data, claim_number=state["claimed"])
                else:
                    state["ineligible"] += 1
                    status_desc = f"Account {idx}: No active Gemini promo"

            # Periodic dashboard update (at most every 1.5s or on finish)
            now = time.time()
            if now - last_ui_update >= 1.5 or idx == len(token_items):
                try:
                    bot.edit_message_text(
                        render_bulk_dashboard(state, status_desc),
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=stop_markup
                    )
                    last_ui_update = now
                except Exception:
                    pass

            time.sleep(0.3)

    except Exception as e:
        print(f"Error in bulk session worker: {e}")
    finally:
        active_bulk_logins.pop(chat_id, None)

    # Deliver final report and files
    claimed_list = state["successful_claims"]
    total_claimed = len(claimed_list)
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    final_text = (
        "🏁 <b>Bulk Session Login Finished!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>Summary Statistics:</b>\n"
        f"• <b>Total Accounts Processed:</b> {state['current']} / {state['total']}\n"
        f"• <b>✅ Successfully Claimed:</b> {total_claimed}\n"
        f"• <b>⚠️ Ineligible (No Gemini Promo):</b> {state['ineligible']}\n"
        f"• <b>🔴 Expired / Invalid Tokens:</b> {state['invalid']}\n"
        f"• <b>Completion Time:</b> {timestamp_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    if total_claimed > 0:
        valid_links = [it.get("gemini_link", "") for it in claimed_list if it.get("gemini_link")]
        phones = [it.get("phone", "") for it in claimed_list if it.get("phone")]
        batch_obj = batch_manager.create_batch(valid_links, phones=phones, label="Bulk Session Claim")

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("📁 Saved Batches & Links", callback_data="btn_download_txt", style="primary"),
            types.InlineKeyboardButton("💾 All Saved Sessions", callback_data="btn_download_sessions", style="success"),
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="success")
        )
        final_text += (
            f"📦 <b>Session Batch Created:</b> <code>{html.escape(batch_obj['batch_name'])}</code>\n"
            f"📄 <i>Both the links file and full account sessions file are attached below:</i>"
        )
        bot.send_message(chat_id, final_text, reply_markup=markup)

        # 1. Links file
        links_content = "\n".join(valid_links) + "\n"
        links_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"gemini_bulk_{batch_obj['batch_id']}.txt")
        try:
            with open(links_path, "w", encoding="utf-8") as f:
                f.write(links_content)
            with open(links_path, "rb") as doc:
                bot.send_document(
                    chat_id,
                    doc,
                    caption=f"📄 <b>Gemini Bulk Links ({total_claimed} Links)</b>\nBatch: {html.escape(batch_obj['batch_name'])}\n1 link per line.",
                    visible_file_name=f"gemini_bulk_{batch_obj['batch_id']}.txt"
                )
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Error sending links file: {html.escape(str(e))}")

        # 2. Sessions file (Phone + Token + Link)
        sess_content = config.format_sessions_export(claimed_list, f"Bulk Claim Batch - {batch_obj['batch_name']}")
        sess_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"gemini_bulk_sessions_{batch_obj['batch_id']}.txt")
        try:
            with open(sess_path, "w", encoding="utf-8") as f:
                f.write(sess_content)
            with open(sess_path, "rb") as doc:
                bot.send_document(
                    chat_id,
                    doc,
                    caption=(
                        f"🔑 <b>Gemini Bulk Claimed Sessions ({total_claimed} Accounts)</b>\n"
                        f"Batch: {html.escape(batch_obj['batch_name'])}\n"
                        "Includes Phone Numbers, JWT Tokens, and Gemini Links."
                    ),
                    visible_file_name=f"gemini_bulk_sessions_{batch_obj['batch_id']}.txt"
                )
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Error sending sessions file: {html.escape(str(e))}")
    else:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("⚡ Bulk Login Again", callback_data="btn_bulk_session_login", style="primary"),
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger")
        )
        final_text += "ℹ️ <i>None of the processed sessions had an active Google Gemini promo benefit on Indosat.</i>"
        bot.send_message(chat_id, final_text, reply_markup=markup)

def send_all_saved_sessions_file(chat_id: int):
    sessions = config.get_all_saved_sessions()
    if not sessions:
        bot.send_message(
            chat_id,
            "ℹ️ <b>No saved sessions yet!</b>\n\n"
            "Sessions are automatically saved whenever a Gemini link is claimed via:\n"
            "• ⚡ <b>Auto-Claimer</b>\n"
            "• ⚡ <b>Bulk Session Login</b>\n"
            "• 🔑 <b>Session Login</b>\n"
            "• 📱 <b>Manual OTP Login</b>",
            reply_markup=get_main_keyboard()
        )
        return

    content = config.format_sessions_export(sessions, "All Claimed Sessions Database")
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_all_claimed_sessions.txt")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        with open(file_path, "rb") as doc:
            bot.send_document(
                chat_id,
                doc,
                caption=(
                    f"💾 <b>All Saved Gemini Sessions ({len(sessions)} Accounts)</b>\n"
                    "Official MyIM3 Google Gemini Database.\n"
                    "Includes Phone Numbers, JWT Session Tokens, and Gemini Links."
                ),
                visible_file_name="gemini_all_claimed_sessions.txt"
            )
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Error sending sessions file: {html.escape(str(e))}")

# --- AUTO CLAIMER: OPERATOR & TARGET SELECTION ---

def start_auto_login_flow(chat_id, edit_message_id=None):
    settings = config.load_settings()
    prov_key = settings.get("sms_provider", "simxpro")
    providers = settings.get("providers", {})
    p_info = providers.get(prov_key, config.DEFAULT_PROVIDERS.get(prov_key, {}))
    prov_name = p_info.get("name", prov_key)
    prov_domain = p_info.get("domain", "")
    charges_info = p_info.get("charges_info", "N/A")

    sms = get_sms_service(prov_key)
    bal_res = sms.get_balance()

    if not bal_res["success"]:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(f"🔑 Add / Fix {prov_name} API Key", callback_data=f"setkey_prov_{prov_key}", style="primary"),
            types.InlineKeyboardButton("🔄 Switch SMS Provider", callback_data="btn_providers", style="primary"),
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger")
        )
        text = (
            f"⚠️ <b>SMS Provider Connection Error ({html.escape(prov_name)})!</b>\n\n"
            f"<b>Message:</b> <code>{html.escape(bal_res['message'])}</code>\n\n"
            f"Please configure your API Key for {html.escape(prov_name)} or switch to another provider."
        )
        if edit_message_id:
            try:
                bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
                return
            except Exception:
                pass
        bot.send_message(chat_id, text, reply_markup=markup)
        return

    current_balance = bal_res["balance"]

    if prov_key == "simxpro":
        operators = settings.get("operators", {})
        op31_info = operators.get("31", {"price": 7.00, "name": "Operator 31"})
        op7_info = operators.get("7", {"price": 12.42, "name": "Operator 7"})

        status_31 = "🟢" if current_balance >= op31_info["price"] else "🔴"
        status_7 = "🟢" if current_balance >= op7_info["price"] else "🔴"

        text = (
            "⚡ <b>Auto-Claimer Automation (myIM3 Indosat)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 <b>Active Provider:</b> <code>Simxpro (simxpro.org)</code>\n"
            f"💰 <b>Live Balance:</b> <code>₹{current_balance:.2f}</code>\n"
            f"🏷 <b>myIM3 Charges:</b> <code>{html.escape(charges_info)}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Available Operator Pools on Simxpro:</b>\n"
            f"• {status_31} <b>Operator 31:</b> <code>₹{op31_info['price']:.2f}</code> (<i>Budget Pool — may be offline</i>)\n"
            f"• {status_7} <b>Operator 7:</b> <code>₹{op7_info['price']:.2f}</code> (<i>Primary Pool — Active in Stock</i>)\n\n"
            "<i>Select your operator pool to begin:</i>"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(f"⭐ Operator 7 (Active In Stock) — ₹{op7_info['price']:.2f}", callback_data="select_op_7", style="success"),
            types.InlineKeyboardButton(f"Operator 31 — ₹{op31_info['price']:.2f}", callback_data="select_op_31", style="primary"),
            types.InlineKeyboardButton("🔄 Switch SMS Provider", callback_data="btn_providers", style="primary"),
            types.InlineKeyboardButton("📁 Download Links (.txt)", callback_data="btn_download_txt", style="primary"),
            types.InlineKeyboardButton("🔙 Back to Main Menu", callback_data="btn_back_main", style="danger")
        )
    elif prov_key == "uotp":
        operators = p_info.get("operators", {})
        op_955 = operators.get("uotp_955", {"name": "Operator (₹9.55)", "price": 9.55})
        op_8 = operators.get("8", {"name": "Operator 8 (₹13.91)", "price": 13.91})

        status_955 = "🟢" if current_balance >= op_955["price"] else "🔴"
        status_8 = "🟢" if current_balance >= op_8["price"] else "🔴"

        text = (
            "⚡ <b>Auto-Claimer Automation (myIM3 Indosat)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 <b>Active Provider:</b> <code>uOTP.store (uotp.store)</code>\n"
            f"💰 <b>Live Balance:</b> <code>₹{current_balance:.2f}</code>\n"
            f"🏷 <b>myIM3 Charges:</b> <code>Operator: ₹9.55 | Operator 8: ₹13.91</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Available Operator Pools on uOTP.store:</b>\n"
            f"• {status_8} <b>Operator 8 (Primary Pool):</b> <code>₹{op_8['price']:.2f}</code> (<i>🟢 Live & Active in Stock! Instant Delivery</i>)\n"
            f"• {status_955} <b>Operator (Budget Pool):</b> <code>₹{op_955['price']:.2f}</code> (<i>⚠️ Currently Upstream NO_CONNECTION / Out of Stock</i>)\n\n"
            "👉 <b>Recommended:</b> Use <b>Operator 8</b> for guaranteed instant numbers:"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(f"⭐ Operator 8 (Live In Stock) — ₹{op_8['price']:.2f}", callback_data="select_op_8", style="success"),
            types.InlineKeyboardButton(f"Operator (Budget Pool) — ₹{op_955['price']:.2f}", callback_data="select_op_uotp_955", style="primary"),
            types.InlineKeyboardButton("🔄 Switch SMS Provider", callback_data="btn_providers", style="primary"),
            types.InlineKeyboardButton("📁 Download Links (.txt)", callback_data="btn_download_txt", style="primary"),
            types.InlineKeyboardButton("🔙 Back to Main Menu", callback_data="btn_back_main", style="danger")
        )
    else:
        # NexNum.in, Grizzly SMS
        text = (
            f"⚡ <b>Auto-Claimer Automation (myIM3 Indosat)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 <b>Active Provider:</b> <code>{html.escape(prov_name)} ({html.escape(prov_domain)})</code>\n"
            f"💰 <b>Live Balance:</b> <code>₹{current_balance:.2f}</code>\n"
            f"🏷 <b>myIM3 Charges:</b> <code>{html.escape(charges_info)}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Continuous Auto-Repeat Workflow:</b>\n"
            f"• Automatically buys Indosat virtual numbers via {html.escape(prov_name)}.\n"
            "• Non-Indosat or rejected numbers are <b>instantly cancelled & refunded</b>.\n"
            "• Dispatches OTP, captures incoming SMS, and activates <b>Google AI Plus 6-Month Link</b>!\n\n"
            "<i>Select your target quantity to start:</i>"
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        b1 = types.InlineKeyboardButton("🎯 1 Link (Repeat until Claimed)", callback_data="claim_qty_direct_1", style="success")
        b2 = types.InlineKeyboardButton("🎯 3 Valid Links", callback_data="claim_qty_direct_3", style="primary")
        b3 = types.InlineKeyboardButton("🎯 5 Valid Links", callback_data="claim_qty_direct_5", style="primary")
        b4 = types.InlineKeyboardButton("🔢 Custom Qty", callback_data="claim_qty_direct_custom", style="primary")
        b5 = types.InlineKeyboardButton("🔄 Switch Provider", callback_data="btn_providers", style="primary")
        b6 = types.InlineKeyboardButton("📁 Get Claimed (.txt)", callback_data="btn_download_txt", style="success")
        b7 = types.InlineKeyboardButton("🔙 Main Menu", callback_data="btn_back_main", style="danger")
        markup.add(b1)
        markup.add(b2, b3)
        markup.add(b4)
        markup.add(b5, b6)
        markup.add(b7)

    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup)

def get_op_details(op_id, prov_key=None):
    if not prov_key:
        prov_key = config.get_setting("sms_provider", "simxpro")
    
    if prov_key == "uotp":
        if op_id == "uotp_955":
            return {"name": "Operator (₹9.55)", "price": 9.55, "id": "1"}
        elif op_id == "8":
            return {"name": "Operator 8 (₹13.91)", "price": 13.91, "id": "8"}
        return {"name": f"Operator {op_id}", "price": 9.55, "id": op_id}
    else:
        if op_id == "31":
            return {"name": "Operator 31", "price": 7.00, "id": "31"}
        elif op_id == "7":
            return {"name": "Operator 7", "price": 12.42, "id": "7"}
        return {"name": f"Operator {op_id}", "price": 7.00, "id": op_id}

def show_quantity_selector(chat_id, op_id, edit_message_id=None):
    settings = config.load_settings()
    prov_key = settings.get("sms_provider", "simxpro")
    op_info = get_op_details(op_id, prov_key)
    op_name = op_info["name"]
    required_price = op_info["price"]

    sms = get_sms_service(prov_key)
    bal_res = sms.get_balance()
    current_balance = bal_res["balance"] if bal_res["success"] else 0.0

    markup = types.InlineKeyboardMarkup(row_width=2)
    b1 = types.InlineKeyboardButton("🎯 1 Link (Repeat until Claimed)", callback_data=f"claim_qty_{op_id}_1", style="success")
    b2 = types.InlineKeyboardButton("🎯 3 Valid Links", callback_data=f"claim_qty_{op_id}_3", style="primary")
    b3 = types.InlineKeyboardButton("🎯 5 Valid Links", callback_data=f"claim_qty_{op_id}_5", style="primary")
    b4 = types.InlineKeyboardButton("🔢 Custom Qty", callback_data=f"claim_qty_{op_id}_custom", style="primary")
    b5 = types.InlineKeyboardButton("📁 Get Claimed (.txt)", callback_data="btn_download_txt", style="primary")
    b6 = types.InlineKeyboardButton("🔙 Back", callback_data="btn_auto_login", style="danger")
    markup.add(b1)
    markup.add(b2, b3)
    markup.add(b4)
    markup.add(b5, b6)

    info_text = (
        f"✅ <b>Target Setup for {html.escape(op_name)}</b>\n\n"
        f"💰 <b>Current Balance:</b> <code>₹{current_balance:.2f}</code>\n"
        f"🏷 <b>Operator Price:</b> <code>₹{required_price:.2f}</code>\n\n"
        "<b>Continuous Auto-Repeat Workflow:</b>\n"
        "• Bot purchases number using selected operator.\n"
        "• Non-Indosat or rejected numbers are <b>instantly cancelled & refunded</b>.\n"
        "• Automatically repeats until a valid <b>Google AI Plus 6-Month Link</b> is secured!\n\n"
        "Select your target to start:"
    )

    if edit_message_id:
        try:
            bot.edit_message_text(info_text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, info_text, reply_markup=markup)

def validate_balance_and_select_op(chat_id, op_id, edit_message_id=None):
    settings = config.load_settings()
    prov_key = settings.get("sms_provider", "simxpro")
    sms = get_sms_service(prov_key)
    bal_res = sms.get_balance()
    
    op_info = get_op_details(op_id, prov_key)
    required_price = op_info["price"]
    op_name = op_info["name"]

    if not bal_res["success"]:
        bot.send_message(chat_id, f"❌ Failed to fetch live balance: {html.escape(bal_res['message'])}")
        return

    current_balance = bal_res["balance"]

    if current_balance < required_price:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton(f"⚡ Force Start Anyway ({op_name})", callback_data=f"force_claim_{op_id}", style="primary"),
            types.InlineKeyboardButton("🔄 Refresh Live Balance", callback_data=f"select_op_{op_id}", style="primary"),
            types.InlineKeyboardButton("📁 Download Links (.txt)", callback_data="btn_download_txt", style="primary"),
            types.InlineKeyboardButton("🔙 Choose Another Operator", callback_data="btn_auto_login", style="danger")
        )
        p_name = "uOTP.store" if prov_key == "uotp" else "Simxpro"
        error_text = (
            f"⚠️ <b>Low Balance Warning for {html.escape(op_name)}!</b>\n\n"
            f"💰 <b>Your Current Balance:</b> <code>₹{current_balance:.2f}</code>\n"
            f"🏷 <b>Required for this Operator:</b> <code>₹{required_price:.2f}</code>\n\n"
            f"{p_name} requires sufficient funds in your account to rent numbers.\n"
            f"• Please top up balance on {p_name} and tap <b>Refresh Live Balance</b>.\n"
            f"• Or tap <b>Force Start Anyway</b> to attempt number purchase with current balance."
        )
        if edit_message_id:
            try:
                bot.edit_message_text(error_text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
                return
            except Exception:
                pass
        bot.send_message(chat_id, error_text, reply_markup=markup)
        return

    show_quantity_selector(chat_id, op_id, edit_message_id=edit_message_id)

# --- AUTO CLAIMER ENGINE WITH CONTINUOUS PIPELINE ---

def start_auto_claimer_execution(chat_id, operator=None, target_claims=1):
    if chat_id in active_claimers:
        if not active_claimers[chat_id].get("stopped", True):
            bot.send_message(chat_id, "⚠️ An automation job is already running for your account! Send /cancel or tap Stop to halt it.")
            return
        else:
            active_claimers.pop(chat_id, None)

    settings = config.load_settings()
    prov_key = settings.get("sms_provider", "simxpro")
    prov_info = settings.get("providers", {}).get(prov_key, {})
    prov_name = prov_info.get("name", prov_key)

    msg = bot.send_message(
        chat_id,
        f"🚀 <b>Starting Auto-Claimer Automation Engine ({html.escape(prov_name)})...</b>\nConnecting to SMS Gateway and myIM3 backend..."
    )

    claimer_state = {
        "stop": False,
        "stopped": False,
        "target_claims": target_claims,
        "successful_claims": [],
        "attempts": 0,
        "skipped_non_indosat": 0,
        "rejected_myim3": 0,
        "timeout_sms": 0,
        "operator": operator,
        "provider": prov_key,
        "message_id": msg.message_id
    }
    active_claimers[chat_id] = claimer_state

    t = threading.Thread(target=auto_claimer_worker_thread, args=(chat_id, claimer_state))
    t.daemon = True
    t.start()

def auto_claimer_worker_thread(chat_id, state):
    prov_key = state.get("provider", "simxpro")
    sms = get_sms_service(prov_key)
    settings = config.load_settings()
    p_info = settings.get("providers", {}).get(prov_key, {})
    prov_name = p_info.get("name", prov_key)
    country_id = p_info.get("default_country", "6")
    service_code = p_info.get("default_service", "is")
    operator = state["operator"]
    target_claims = state["target_claims"]
    msg_id = state["message_id"]

    if prov_key == "uotp":
        if operator == "uotp_955":
            op_price = 9.55
            op_name = "Operator (₹9.55)"
            op_param = "1"
        elif operator == "8":
            op_price = 13.91
            op_name = "Operator 8 (₹13.91)"
            op_param = "8"
        else:
            op_price = 13.91
            op_name = "Operator 8 (₹13.91)"
            op_param = "8"
    elif prov_key == "simxpro" and operator:
        operators = settings.get("operators", {})
        op_price = operators.get(operator, {}).get("price", 7.00 if operator == "31" else 12.42)
        op_name = f"Operator {operator}"
        op_param = operator
    else:
        op_price = p_info.get("price", 7.50)
        op_name = prov_name
        op_param = operator

    stop_markup = types.InlineKeyboardMarkup(row_width=2)
    stop_markup.add(
        types.InlineKeyboardButton("🛑 Stop Auto-Claimer", callback_data="btn_stop_claimer", style="danger"),
        types.InlineKeyboardButton("📁 Download Links (.txt)", callback_data="btn_download_txt", style="primary")
    )

    # Active pool of rented numbers
    # Each entry: {
    #   'order_id': str,
    #   'phone': str,
    #   'transid': str or None,
    #   'otp_sent': bool,
    #   'purchase_time': float,
    #   'last_sms_check': float
    # }
    active_orders = []

    def render_dashboard(status_msg, current_bal=None):
        bal_str = f"₹{current_bal:.2f}" if current_bal is not None else "Checking..."
        active_count = len(active_orders)
        active_otp_count = len([o for o in active_orders if o.get("otp_sent")])
        buffer_line = f"• <b>📱 Active Numbers in Pool:</b> {active_count} ({active_otp_count} waiting SMS)\n" if active_count > 0 else ""
        return (
            f"🔄 <b>Auto-Claimer in Progress ({html.escape(prov_name)})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <b>Live Progress Dashboard:</b>\n"
            f"• <b>Target:</b> {len(state['successful_claims'])} / {target_claims} Claimed\n"
            f"• <b>Total Attempts:</b> #{state['attempts']}\n"
            f"• <b>Provider:</b> {html.escape(prov_name)} ({html.escape(op_name)})\n"
            f"• <b>✅ Claimed:</b> {len(state['successful_claims'])}\n"
            f"• <b>🚫 Non-Indosat Skipped:</b> {state['skipped_non_indosat']}\n"
            f"• <b>♻️ myIM3 Rejected:</b> {state['rejected_myim3']}\n"
            f"• <b>⏱️ SMS Timeouts:</b> {state['timeout_sms']}\n"
            f"{buffer_line}"
            f"• <b>💰 Live Balance:</b> <code>{bal_str}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ <b>Current Status:</b>\n{status_msg}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Auto-drains balance across active numbers; oldest auto-refunds to loop continuously!</i>"
        )

    def cleanup_all():
        for ord_item in list(active_orders):
            try:
                sms.canceller.add(ord_item["order_id"], ord_item["purchase_time"])
            except Exception:
                pass
        active_orders.clear()

    try:
        while len(state["successful_claims"]) < target_claims:
            if state.get("stop", False):
                break

            # 1. Fetch live balance
            bal_res = sms.get_balance()
            live_balance = bal_res["balance"] if bal_res["success"] else 0.0

            # 2. CONTINUOUS BUYING BURST:
            # Drain balance by buying numbers while live_balance >= op_price!
            has_funds = (live_balance >= op_price) or (len(active_orders) == 0 and state["attempts"] == 0)
            while (has_funds and 
                   len(active_orders) < 10 and 
                   len(state["successful_claims"]) < target_claims and 
                   not state.get("stop", False)):

                state["attempts"] += 1
                config.increment_stat("total_attempts", 1)

                try:
                    bot.edit_message_text(
                        render_dashboard(f"🚀 <b>Purchasing number #{state['attempts']} via {prov_name}...</b>", live_balance),
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=stop_markup
                    )
                except Exception:
                    pass

                buy_res = sms.buy_number(country=country_id, service=service_code, operator=op_param)

                if not buy_res["success"]:
                    raw_err = buy_res.get("raw", "")
                    err_msg = html.escape(buy_res.get("message", "Purchase error"))

                    if "NO_CONNECTION" in raw_err or "NO_NUMBERS" in raw_err:
                        if prov_key == "uotp" and op_param == "1":
                            status_alert = (
                                f"⏳ <b>Stock Alert ({prov_name}):</b> Operator ₹9.55 reported <code>{raw_err}</code>.\n"
                                "👉 <b>Operator 8 (₹13.91) is LIVE & in stock!</b> Retrying..."
                            )
                        else:
                            status_alert = f"⏳ <b>Stock Alert ({prov_name}):</b> Reported <code>{raw_err}</code>. Retrying in 4s..."
                    elif "NO_BALANCE" in raw_err:
                        if not active_orders:
                            status_alert = f"⚠️ <b>{prov_name} Balance Insufficient!</b> The gateway reported <code>NO_BALANCE</code> for this operator pool. Please check funds or switch operator."
                            try:
                                bot.edit_message_text(
                                    render_dashboard(status_alert, live_balance),
                                    chat_id=chat_id,
                                    message_id=msg_id,
                                    reply_markup=get_main_keyboard()
                                )
                            except Exception:
                                pass
                            state["stop"] = True
                            break
                        else:
                            status_alert = f"⚠️ <b>{prov_name} Balance Drained!</b> Awaiting oldest number refund..."
                    else:
                        status_alert = f"⚠️ Gateway: <code>{err_msg}</code>. Retrying in 4s..."

                    try:
                        bot.edit_message_text(
                            render_dashboard(status_alert, live_balance),
                            chat_id=chat_id,
                            message_id=msg_id,
                            reply_markup=stop_markup
                        )
                    except Exception:
                        pass
                    break

                order_id = buy_res["order_id"]
                phone = format_msisdn(buy_res["phone"])
                purchase_time = buy_res.get("purchase_time", time.time())

                # Check Indosat prefix
                if not is_indosat_prefix(phone):
                    state["skipped_non_indosat"] += 1
                    config.increment_stat("total_refunded", 1)
                    imm_canc = False
                    try:
                        imm_canc = sms.set_status(order_id, 8)
                    except Exception:
                        pass
                    if not imm_canc:
                        active_orders.append({
                            "order_id": order_id,
                            "phone": phone,
                            "transid": None,
                            "otp_sent": False,
                            "purchase_time": purchase_time,
                            "last_sms_check": 0
                        })
                else:
                    # Valid Indosat Prefix -> Request OTP from myIM3 backend
                    otp_req = im3_client.send_otp(phone)
                    if not otp_req["success"]:
                        state["rejected_myim3"] += 1
                        config.increment_stat("total_refunded", 1)
                        imm_canc = False
                        try:
                            imm_canc = sms.set_status(order_id, 8)
                        except Exception:
                            pass
                        if not imm_canc:
                            active_orders.append({
                                "order_id": order_id,
                                "phone": phone,
                                "transid": None,
                                "otp_sent": False,
                                "purchase_time": purchase_time,
                                "last_sms_check": 0
                            })
                    else:
                        # OTP dispatched successfully!
                        active_orders.append({
                            "order_id": order_id,
                            "phone": phone,
                            "transid": otp_req["transid"],
                            "otp_sent": True,
                            "purchase_time": purchase_time,
                            "last_sms_check": 0
                        })

                # Refresh balance after buy
                bal_after = sms.get_balance()
                live_balance = bal_after["balance"] if bal_after["success"] else (live_balance - op_price)

            # 3. SMS POLLING FOR ALL ACTIVE NUMBERS:
            now_t = time.time()
            for ord_item in list(active_orders):
                if state.get("stop", False):
                    break
                if not ord_item.get("otp_sent") or not ord_item.get("transid"):
                    continue

                if now_t - ord_item.get("last_sms_check", 0) >= 2.5:
                    ord_item["last_sms_check"] = now_t
                    sms_status = sms.check_status(ord_item["order_id"])

                    if sms_status["status"] == "OK" and sms_status["code"]:
                        otp_code = sms_status["code"]
                        sms.finish_number(ord_item["order_id"])
                        active_orders.remove(ord_item)

                        try:
                            bot.edit_message_text(
                                render_dashboard(
                                    f"📩 <b>OTP Received on +{ord_item['phone']}!</b>\n"
                                    f"🔑 Code: <code>{otp_code}</code>\n"
                                    "🔐 Logging into myIM3 & verifying Google Gemini 6M offer...",
                                    live_balance
                                ),
                                chat_id=chat_id,
                                message_id=msg_id,
                                reply_markup=stop_markup
                            )
                        except Exception:
                            pass

                        val_res = im3_client.validate_otp(ord_item["transid"], otp_code)
                        if val_res["success"]:
                            token_id = val_res["token_id"]
                            # Save login session automatically!
                            config.save_last_session(ord_item["phone"], token_id)

                            offer_data = im3_client.get_gemini_details(token_id, phone=ord_item["phone"])

                            if offer_data.get("is_verified") and offer_data.get("gemini_link"):
                                offer_data["token_id"] = token_id
                                offer_data["phone"] = ord_item["phone"]
                                state["successful_claims"].append(offer_data)
                                config.increment_stat("total_claims", 1)
                                record_claim_to_master_log(offer_data)
                                config.save_successful_session(
                                    phone=ord_item["phone"],
                                    token_id=token_id,
                                    gemini_link=offer_data.get("gemini_link", ""),
                                    direct_google_url=offer_data.get("direct_google_url", ""),
                                    source="Auto-Claimer"
                                )
                                send_single_gemini_success(chat_id, offer_data, claim_number=len(state["successful_claims"]))
                                if len(state["successful_claims"]) >= target_claims:
                                    break
                            else:
                                state["not_eligible"] = state.get("not_eligible", 0) + 1
                                try:
                                    bot.edit_message_text(
                                        render_dashboard(
                                            f"⚠️ Number <code>+{ord_item['phone']}</code> has no active Gemini benefit.\n"
                                            "IOH Gateway reported: <code>URL not available</code>.\n"
                                            "🔄 Continuing pipeline...",
                                            live_balance
                                        ),
                                        chat_id=chat_id,
                                        message_id=msg_id,
                                        reply_markup=stop_markup
                                    )
                                except Exception:
                                    pass
                        break

            if len(state["successful_claims"]) >= target_claims or state.get("stop", False):
                break

            # 4. PROCESS MATURED ORDERS (age >= 122s):
            now_t = time.time()
            for ord_item in list(active_orders):
                age = now_t - ord_item["purchase_time"]
                if age >= 122:
                    try:
                        sms.set_status(ord_item["order_id"], 8)
                    except Exception:
                        pass
                    active_orders.remove(ord_item)
                    config.increment_stat("total_refunded", 1)
                    if ord_item.get("otp_sent"):
                        state["timeout_sms"] += 1

            # 5. BALANCE DRAINED MANAGEMENT:
            bal_check = sms.get_balance()
            live_balance = bal_check["balance"] if bal_check["success"] else live_balance

            if live_balance < op_price:
                if not active_orders:
                    try:
                        bot.edit_message_text(
                            f"⚠️ <b>Auto-Claimer Paused: Insufficient Balance!</b>\n\n"
                            f"💰 Current Balance on {html.escape(prov_name)}: <code>₹{live_balance:.2f}</code> (Required: <code>₹{op_price:.2f}</code>)\n"
                            f"Please top up funds on {html.escape(p_info.get('domain', prov_name))} to continue.",
                            chat_id=chat_id,
                            message_id=msg_id,
                            reply_markup=get_main_keyboard()
                        )
                    except Exception:
                        pass
                    break
                else:
                    oldest = min(active_orders, key=lambda x: x["purchase_time"])
                    elapsed = int(time.time() - oldest["purchase_time"])
                    remaining = max(0, 122 - elapsed)

                    if remaining > 0:
                        active_waiting_sms = len([o for o in active_orders if o.get("otp_sent")])
                        try:
                            bot.edit_message_text(
                                render_dashboard(
                                    f"💰 <b>Balance drained across {len(active_orders)} active numbers!</b>\n"
                                    f"⏳ <b>Awaiting oldest refund:</b> <code>{remaining}s remaining</code>\n"
                                    f"📱 <i>Oldest: +{oldest['phone']} ({elapsed}s/122s)\n"
                                    f"⚡ Actively polling {active_waiting_sms} numbers for incoming SMS...</i>",
                                    live_balance
                                ),
                                chat_id=chat_id,
                                message_id=msg_id,
                                reply_markup=stop_markup
                            )
                        except Exception:
                            pass
                        for _ in range(int(min(3, remaining) * 5)):
                            if state.get("stop", False):
                                break
                            time.sleep(0.2)
                        continue
                    else:
                        # Oldest reached 122s! Cancel now to trigger refund!
                        try:
                            bot.edit_message_text(
                                render_dashboard(
                                    f"♻️ <b>Cancelling oldest number (+{oldest['phone']}) to replenish balance...</b>\n"
                                    "🔄 <i>Purchasing next number immediately after refund...</i>",
                                    live_balance
                                ),
                                chat_id=chat_id,
                                message_id=msg_id,
                                reply_markup=stop_markup
                            )
                        except Exception:
                            pass

                        try:
                            sms.set_status(oldest["order_id"], 8)
                        except Exception:
                            pass
                        active_orders.remove(oldest)
                        config.increment_stat("total_refunded", 1)
                        if oldest.get("otp_sent"):
                            state["timeout_sms"] += 1
                        for _ in range(10):
                            if state.get("stop", False):
                                break
                            time.sleep(0.2)
                        continue
            else:
                for _ in range(5):
                    if state.get("stop", False):
                        break
                    time.sleep(0.2)

    finally:
        try:
            cleanup_all()
        except Exception as e:
            print(f"[Cleanup Error] {e}", flush=True)

    state["stopped"] = True
    active_claimers.pop(chat_id, None)

    try:
        send_final_report_and_file(chat_id, state["successful_claims"], state)
    except Exception as e:
        print(f"[Final Report Error] {e}", flush=True)

def record_claim_to_master_log(item):
    gemini_link = item.get("gemini_link", "").strip()
    if not gemini_link:
        return
    # Strictly double-check that link resolves with HTTP 302 to Google One!
    verif = verify_gemini_link(gemini_link)
    if not verif.get("valid"):
        print(f"[MasterLog] Rejected unverified link: {gemini_link}")
        return
    try:
        with open(MASTER_CLAIMS_FILE, "a", encoding="utf-8") as f:
            f.write(gemini_link + "\n")
    except Exception as e:
        print(f"Error logging to master: {e}")

def send_single_gemini_success(chat_id, offer_data, claim_number=None):
    phone = html.escape(str(offer_data.get("phone", "N/A")))
    gemini_link = html.escape(str(offer_data.get("gemini_link", "")))
    token_id = str(offer_data.get("token_id", ""))
    is_verified = offer_data.get("is_verified", False)
    
    header = f"🎉 <b>NEW GOOGLE AI PLUS (6M) CLAIMED! (#{claim_number})</b>" if claim_number else "🎉 <b>GEMINI OFFER READY!</b>"
    status_badge = "🟢 <b>Status:</b> Live & Verified Active (Official IOH Activation Link)\n" if is_verified else ""

    markup = types.InlineKeyboardMarkup(row_width=1)
    if gemini_link:
        markup.add(types.InlineKeyboardButton("🎁 Open IOH Gemini Link", url=gemini_link))
    markup.add(
        types.InlineKeyboardButton("📋 View / Copy Saved Session", callback_data="btn_view_session", style="primary"),
        types.InlineKeyboardButton("📁 Download Claimed Links (.txt)", callback_data="btn_download_txt", style="success"),
        types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="primary")
    )

    token_section = ""
    if token_id:
        token_section = (
            "🔑 <b>Session Token (Tap to Copy):</b>\n"
            f"<code>{html.escape(token_id)}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
        )

    msg = (
        f"{header}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>Number:</b> <code>+{phone}</code>\n"
        "🎁 <b>Offer:</b> Google Gemini AI Plus 6 Months Free (400GB)\n"
        f"{status_badge}"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>Official IOH Short Link:</b>\n{gemini_link}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{token_section}"
        "💡 <i>Click the button above or copy the link to activate 6 months of Google Gemini AI Plus!</i>"
    )

    try:
        bot.send_message(chat_id, msg, reply_markup=markup, disable_web_page_preview=False)
    except Exception as e:
        print(f"Error sending success message: {e}")

def send_final_report_and_file(chat_id, claimed_results, state):
    # Clear / update original progress message so Stop button does not linger
    orig_msg_id = state.get("message_id")
    if orig_msg_id:
        try:
            bot.edit_message_text(
                "🛑 <b>Auto-Claimer Automation Ended.</b>\nSummary report generated below:",
                chat_id=chat_id,
                message_id=orig_msg_id,
                reply_markup=None
            )
        except Exception:
            pass

    valid_links = [
        item.get("gemini_link", "").strip() 
        for item in claimed_results 
        if item.get("gemini_link", "").strip() and item.get("is_verified") is True
    ]
    phones = [item.get("phone", "") for item in claimed_results if item.get("phone")]
    total_claimed = len(valid_links)
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    prov_key = state.get("provider", "simxpro")
    sms = get_sms_service(prov_key)
    bal_res = sms.get_balance()
    live_bal = f"₹{bal_res['balance']:.2f}" if bal_res["success"] else "N/A"

    is_stopped = state.get("stop", False)
    header_title = "🛑 <b>Auto-Claimer Stopped by User!</b>" if is_stopped else "🏁 <b>Auto-Claimer Automation Finished!</b>"

    summary_text = (
        f"{header_title}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>Session Statistics:</b>\n"
        f"• <b>Total Attempts:</b> {state['attempts']}\n"
        f"• <b>✅ Successfully Claimed in this Session:</b> {total_claimed}\n"
        f"• <b>🚫 Non-Indosat Skipped:</b> {state['skipped_non_indosat']}\n"
        f"• <b>♻️ myIM3 Rejected & Refunded:</b> {state['rejected_myim3']}\n"
        f"• <b>⏱️ SMS Timeouts:</b> {state['timeout_sms']}\n"
        f"• <b>💰 Final Live Balance:</b> <code>{live_bal}</code>\n"
        f"• <b>Completion Time:</b> {timestamp_str}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    if total_claimed > 0:
        # Create unique batch for THIS session with 12h expiry!
        batch_obj = batch_manager.create_batch(valid_links, phones=phones, label="Auto-Claimer")
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("📁 Saved Batches & Links", callback_data="btn_download_txt", style="primary"),
            types.InlineKeyboardButton("💾 All Saved Sessions (.txt)", callback_data="btn_download_sessions", style="success"),
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="success")
        )
        summary_text += (
            f"📦 <b>Session Batch Created:</b> <code>{html.escape(batch_obj['batch_name'])}</code>\n"
            f"⏳ <b>Auto-Vanish:</b> This session batch will automatically expire in 12 hours.\n\n"
            f"📄 <i>All {total_claimed} verified links and account sessions from this run are attached below:</i>"
        )
        bot.send_message(chat_id, summary_text, reply_markup=markup)

        # 1. Send Links File (.txt)
        file_content = "\n".join(valid_links) + "\n"
        file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"gemini_batch_{batch_obj['batch_id']}.txt")
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(file_content)

            with open(file_path, "rb") as doc:
                bot.send_document(
                    chat_id,
                    doc,
                    caption=f"📄 <b>Gemini Session Links ({total_claimed} Links)</b>\nBatch: {html.escape(batch_obj['batch_name'])}\nOfficial verified IOH links (1 per line).",
                    visible_file_name=f"gemini_{batch_obj['batch_id']}.txt"
                )
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Error sending links file: {html.escape(str(e))}")

        # 2. Send Full Account Sessions File (.txt) (Phone + JWT Token + Gemini Link)
        sess_content = config.format_sessions_export(claimed_results, f"Auto-Claimer Batch - {batch_obj['batch_name']}")
        sess_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"gemini_sessions_{batch_obj['batch_id']}.txt")
        try:
            with open(sess_path, "w", encoding="utf-8") as f:
                f.write(sess_content)

            with open(sess_path, "rb") as doc:
                bot.send_document(
                    chat_id,
                    doc,
                    caption=(
                        f"🔑 <b>Gemini Claimed Sessions ({total_claimed} Accounts)</b>\n"
                        f"Batch: {html.escape(batch_obj['batch_name'])}\n"
                        "Includes full credentials (Phone + JWT Session Token + Gemini Links)."
                    ),
                    visible_file_name=f"gemini_sessions_{batch_obj['batch_id']}.txt"
                )
        except Exception as e:
            bot.send_message(chat_id, f"⚠️ Error sending sessions file: {html.escape(str(e))}")
    else:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger"))
        summary_text += "ℹ️ <i>No valid Google Gemini links were generated during this run. No .txt file was created.</i>"
        bot.send_message(chat_id, summary_text, reply_markup=markup)


# --- DOCUMENT / FILE HANDLER FOR BULK SESSIONS ---

@bot.message_handler(content_types=['document'])
def handle_document_messages(message):
    chat_id = message.chat.id
    doc = message.document
    if not doc:
        return

    fn = (doc.file_name or "").lower()
    if fn.endswith(".txt") or fn.endswith(".csv") or fn.endswith(".log"):
        wait_msg = bot.send_message(chat_id, "📥 <i>Downloading and parsing uploaded tokens file...</i>")
        try:
            file_info = bot.get_file(doc.file_id)
            downloaded = bot.download_file(file_info.file_path)
            content = downloaded.decode("utf-8", errors="ignore")
            try:
                bot.delete_message(chat_id, wait_msg.message_id)
            except Exception:
                pass
            tokens = parse_bulk_tokens(content)
            if not tokens:
                bot.send_message(chat_id, "⚠️ No valid session tokens found in the uploaded file!", reply_markup=get_cancel_keyboard())
                return
            if len(tokens) == 1:
                message.text = tokens[0]["token"]
                handle_session_token(message)
            else:
                process_bulk_session_input(chat_id, content)
        except Exception as e:
            bot.edit_message_text(f"⚠️ Failed to read uploaded file: {html.escape(str(e))}", chat_id=chat_id, message_id=wait_msg.message_id)
    else:
        bot.send_message(
            chat_id,
            "⚠️ Please upload a <code>.txt</code> file containing your session tokens.",
            reply_markup=get_cancel_keyboard()
        )

# --- TEXT MESSAGE HANDLER ---

@bot.message_handler(func=lambda m: True)
def handle_text_messages(message):
    chat_id = message.chat.id
    text = message.text.strip()
    session = user_states.get(chat_id)

    if not session:
        bot.send_message(message.chat.id, "🤖 Please select an option from the menu:", reply_markup=get_main_keyboard())
        return

    action = session.get("action")

    if action == "WAITING_MANUAL_PHONE":
        handle_manual_phone(message)

    elif action == "WAITING_MANUAL_OTP":
        handle_manual_otp(message)

    elif action == "WAITING_SESSION_TOKEN":
        handle_session_token(message)

    elif action == "WAITING_BULK_SESSION_TOKENS":
        process_bulk_session_input(chat_id, text)

    elif action == "WAITING_CUSTOM_QTY":
        op = session.get("operator")
        if not text.isdigit() or int(text) < 1:
            bot.send_message(chat_id, "⚠️ Please enter a valid positive number.")
            return
        qty = int(text)
        user_states.pop(chat_id, None)
        start_auto_claimer_execution(chat_id, operator=op, target_claims=qty)

    elif action == "WAITING_PROVIDER_API_KEY":
        prov_key = session.get("provider_key", "simxpro")
        settings = config.load_settings()
        p_info = settings.get("providers", {}).get(prov_key, config.DEFAULT_PROVIDERS.get(prov_key, {}))
        name = p_info.get("name", prov_key)
        domain = p_info.get("domain", "")

        api_key_input = text.strip()
        user_states.pop(chat_id, None)

        bot.send_chat_action(chat_id, "typing")

        # Test balance on the provider with the new key!
        test_sms = SMSService(provider_url=p_info.get("url"), api_key=api_key_input)
        bal_res = test_sms.get_balance()

        if bal_res["success"]:
            config.set_provider_key(prov_key, api_key_input)
            bal_val = bal_res["balance"]
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton(f"⚡ Set {name} as Active Provider Now", callback_data=f"activate_prov_{prov_key}", style="success"),
                types.InlineKeyboardButton("📡 View All Providers", callback_data="btn_providers", style="primary"),
                types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger")
            )
            bot.send_message(
                chat_id,
                f"✅ <b>{html.escape(name)} API Key Connected Successfully!</b>\n\n"
                f"💰 <b>Live Balance:</b> <code>₹{bal_val:.2f}</code>\n"
                f"🏷 <b>myIM3 Rate:</b> <code>{html.escape(p_info.get('charges_info', 'N/A'))}</code>\n\n"
                "API Key is saved! Tap below to set this provider as active for Auto-Claimer:",
                reply_markup=markup
            )
        else:
            config.set_provider_key(prov_key, api_key_input)
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("🔄 Re-enter API Key", callback_data=f"setkey_prov_{prov_key}", style="primary"),
                types.InlineKeyboardButton("📡 View Providers", callback_data="btn_providers", style="primary"),
                types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger")
            )
            bot.send_message(
                chat_id,
                f"⚠️ <b>API Key Saved, but verification returned:</b>\n\n"
                f"<code>{html.escape(bal_res['message'])}</code>\n\n"
                f"Please verify that your key is correct on <code>https://{html.escape(domain)}</code>.",
                reply_markup=markup
            )

# --- ADMIN PANEL LOGIC (Accessible only via /admin command) ---

def show_admin_panel(chat_id, edit_message_id=None):
    settings = config.load_settings()
    stats = settings.get("stats", {})
    text = (
        "👑 <b>Admin Control Panel</b>\n\n"
        "Master controls:\n\n"
        f"📊 <b>Total Valid Claims:</b> <code>{stats.get('total_claims', 0)}</code>\n"
        f"🔄 <b>Total Attempts:</b> <code>{stats.get('total_attempts', 0)}</code>\n"
        f"♻️ <b>Total Cancelled & Refunded:</b> <code>{stats.get('total_refunded', 0)}</code>\n"
        f"📡 <b>Active Provider:</b> <code>{settings.get('sms_provider')}</code>\n\n"
        "Select an action below:"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Bot Statistics", callback_data="adm_stats", style="primary"),
        types.InlineKeyboardButton("📁 Download Master Log", callback_data="adm_download_log", style="success"),
        types.InlineKeyboardButton("🔙 Back to Main Menu", callback_data="btn_back_main", style="danger")
    )
    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup)

def show_admin_stats(chat_id, edit_message_id=None):
    settings = config.load_settings()
    stats = settings.get("stats", {})
    active_count = len(active_claimers)

    text = (
        "📊 <b>Detailed Bot Statistics</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Total Successful Claims:</b> <code>{stats.get('total_claims', 0)}</code>\n"
        f"• <b>Total Number Purchases:</b> <code>{stats.get('total_attempts', 0)}</code>\n"
        f"• <b>Auto-Refunded (Non-Indosat/Rejected):</b> <code>{stats.get('total_refunded', 0)}</code>\n"
        f"• <b>Active Auto-Claim Jobs:</b> <code>{active_count}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back to Main Menu", callback_data="btn_back_main", style="danger"))
    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup)

def show_batch_selection_menu(chat_id, edit_message_id=None):
    active_batches = batch_manager.get_active_batches()
    markup = types.InlineKeyboardMarkup(row_width=1)

    if not active_batches:
        text = (
            "📁 <b>Saved Gemini Claim Batches</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ <i>No active saved batches found from the past 12 hours.</i>\n\n"
            "💡 <b>12-Hour Expiration System:</b>\n"
            "• Every claim session produces a unique named batch.\n"
            "• Batches automatically expire and vanish after 12 hours.\n"
            "• Run the <b>⚡ Auto-Claimer</b> to generate fresh promotional links!"
        )
        markup.add(
            types.InlineKeyboardButton("⚡ Auto-Claimer (SMS API)", callback_data="btn_auto_login", style="primary"),
            types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger")
        )
    else:
        total_links_all = sum(b.get("count", 0) for b in active_batches)
        text = (
            "📁 <b>Select Batch / Session to Download</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Please select which session batch you want to inspect & download, "
            "or download all active links combined:\n\n"
            f"📊 <b>Active Batches (Past 12h):</b> <code>{len(active_batches)}</code>\n"
            f"🔗 <b>Total Active Links:</b> <code>{total_links_all}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⏳ <i>Note: All saved batches automatically expire and vanish after 12 hours.</i>"
        )

        for b in active_batches[:8]:
            now = time.time()
            rem_sec = max(0, int(b.get("expires_at", 0) - now))
            rem_h = rem_sec // 3600
            rem_m = (rem_sec % 3600) // 60
            rem_str = f"{rem_h}h {rem_m}m" if rem_h > 0 else f"{rem_m}m"

            btn_label = f"📦 {b['batch_name']} ({rem_str} left)"
            markup.add(types.InlineKeyboardButton(btn_label, callback_data=f"view_batch_{b['batch_id']}", style="primary"))

        if len(active_batches) > 1:
            markup.add(types.InlineKeyboardButton(f"🌐 All Active Batches Combined ({total_links_all} Links)", callback_data="dl_batch_all", style="success"))
        elif len(active_batches) == 1:
            markup.add(types.InlineKeyboardButton(f"🌐 Download All Active Links ({total_links_all} Links)", callback_data="dl_batch_all", style="success"))

        markup.add(types.InlineKeyboardButton("🔙 Back to Main Menu", callback_data="btn_back_main", style="danger"))

    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id, reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup)

def handle_batch_download_single(chat_id, batch_id):
    batch = batch_manager.get_batch_by_id(batch_id)
    if not batch:
        bot.send_message(chat_id, "⚠️ <b>Batch Expired!</b>\nThis session was created more than 12 hours ago and has vanished.", reply_markup=get_main_keyboard())
        return

    now = time.time()
    rem_sec = max(0, int(batch.get("expires_at", 0) - now))
    rem_h = rem_sec // 3600
    rem_m = (rem_sec % 3600) // 60
    rem_str = f"{rem_h}h {rem_m}m" if rem_h > 0 else f"{rem_m}m"

    links = batch.get("links", [])
    count = len(links)
    
    preview_lines = []
    for idx, lk in enumerate(links[:5], 1):
        preview_lines.append(f"<b>{idx}.</b> <code>{html.escape(lk)}</code>")
    if count > 5:
        preview_lines.append(f"<i>...and {count - 5} more links in the .txt file below</i>")
    
    links_text = "\n".join(preview_lines) if preview_lines else "No links"

    detail_msg = (
        f"📦 <b>Session Batch Details</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <b>Batch Name:</b> <code>{html.escape(batch['batch_name'])}</code>\n"
        f"🆔 <b>Batch ID:</b> <code>{html.escape(batch['batch_id'])}</code>\n"
        f"🕒 <b>Created At:</b> <code>{html.escape(batch.get('created_time_str', 'N/A'))}</code>\n"
        f"⏳ <b>Auto-Expires In:</b> <code>{rem_str}</code> (12h TTL)\n"
        f"📊 <b>Total Links:</b> <code>{count}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>Verified IOH Links:</b>\n{links_text}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📄 <i>The clean .txt file for this specific session is attached below:</i>"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📁 Choose Another Batch", callback_data="btn_download_txt", style="primary"),
        types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger")
    )

    bot.send_message(chat_id, detail_msg, reply_markup=markup)

    file_content = "\n".join(links) + "\n"
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"gemini_batch_{batch['batch_id']}.txt")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(file_content)

        with open(file_path, "rb") as doc:
            bot.send_document(
                chat_id,
                doc,
                caption=f"📄 <b>Gemini Links — {html.escape(batch['batch_name'])}</b>\n{count} verified IOH activation links for this session.",
                visible_file_name=f"gemini_{batch['batch_id']}.txt"
            )
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Error sending file: {html.escape(str(e))}")

def handle_batch_download_all(chat_id):
    active_batches = batch_manager.get_active_batches()
    if not active_batches:
        bot.send_message(chat_id, "📁 <b>No active batches found from the past 12 hours!</b>", reply_markup=get_main_keyboard())
        return

    all_links = batch_manager.get_all_active_links()
    total_count = len(all_links)

    if total_count == 0:
        bot.send_message(chat_id, "📁 <b>No verified links found in active batches.</b>", reply_markup=get_main_keyboard())
        return

    summary_text = (
        "🌐 <b>All Active Batches Combined (Past 12 Hours)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Active Batches:</b> <code>{len(active_batches)}</code>\n"
        f"🔗 <b>Total Verified Links:</b> <code>{total_count}</code>\n"
        "⏳ <b>Auto-Vanish Window:</b> Links older than 12h are automatically purged.\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📄 <i>All active verified links combined into one .txt file attached below:</i>"
    )

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📁 Choose Specific Batch", callback_data="btn_download_txt", style="primary"),
        types.InlineKeyboardButton("🏠 Main Menu", callback_data="btn_back_main", style="danger")
    )

    bot.send_message(chat_id, summary_text, reply_markup=markup)

    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_content = "\n".join(all_links) + "\n"
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"gemini_all_active_{timestamp_str}.txt")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(file_content)

        with open(file_path, "rb") as doc:
            bot.send_document(
                chat_id,
                doc,
                caption=f"📄 <b>All Active Gemini Claimed Links ({total_count} Links)</b>\nCombined from all sessions in the last 12 hours.",
                visible_file_name=f"gemini_active_12h_{timestamp_str}.txt"
            )
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Error sending file: {html.escape(str(e))}")

def send_master_log_file(chat_id):
    if not os.path.exists(MASTER_CLAIMS_FILE):
        bot.send_message(chat_id, "📁 <b>No verified Google Gemini links have been generated yet!</b>\n\nNo .txt file is available.", reply_markup=get_main_keyboard())
        return
    with open(MASTER_CLAIMS_FILE, "r", encoding="utf-8") as f:
        valid_lines = [l.strip() for l in f if l.strip() and l.startswith("https://apigw.ioh.co.id/shrt")]
    if not valid_lines:
        bot.send_message(chat_id, "📁 <b>No verified Google Gemini links have been generated yet!</b>\n\nNo .txt file is available.", reply_markup=get_main_keyboard())
        return
    try:
        with open(MASTER_CLAIMS_FILE, "rb") as doc:
            bot.send_document(
                chat_id,
                doc,
                caption=f"📄 <b>Master Gemini Claimed Links File (.txt)</b>\nTotal {len(valid_lines)} verified working official IOH short links.",
                visible_file_name="gemini_all_claimed_links.txt"
            )
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Error sending file: {html.escape(str(e))}")

def start_render_health_server():
    """
    Render Free Web Service requires the app to listen on 0.0.0.0:$PORT.
    If PORT is present in os.environ, start a minimal daemon HTTP health check server.
    """
    port_str = os.environ.get("PORT")
    if not port_str:
        return
    try:
        port = int(port_str)
        class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"MyIM3 Gemini Bot is online and healthy!")

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.end_headers()

            def log_message(self, format, *args):
                pass  # Suppress health check access log noise

        def run_server():
            socketserver.TCPServer.allow_reuse_address = True
            with socketserver.TCPServer(("0.0.0.0", port), HealthCheckHandler) as httpd:
                print(f"🌐 Health-check HTTP server listening on 0.0.0.0:{port} for Render deployment.", flush=True)
                httpd.serve_forever()

        t = threading.Thread(target=run_server, daemon=True)
        t.start()
    except Exception as e:
        print(f"⚠️ Health server start failed: {e}", flush=True)

if __name__ == "__main__":
    start_render_health_server()
    print("=" * 50, flush=True)
    print("🤖 MyIM3 Google Gemini Telegram Bot is Running...", flush=True)
    if BOT_TOKEN:
        print(f"📱 Bot Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-5:]}", flush=True)
    else:
        print("⚠️ WARNING: BOT_TOKEN is empty! Please set it in settings.json or Render Environment Variables (BOT_TOKEN).", flush=True)
    print("=" * 50, flush=True)

    # 1. Clear any stuck webhooks or pending lockouts
    try:
        bot.delete_webhook(drop_pending_updates=True)
        print("🧹 Webhook cleared and pending updates flushed.", flush=True)
    except Exception as e:
        print(f"Notice deleting webhook: {e}", flush=True)

    # 2. Resilient polling with 409 conflict detection & backoff
    backoff = 3
    while True:
        try:
            print("🚀 Starting Telegram bot listener (polling)...", flush=True)
            bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True)
            break
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                print(f"⚠️ Telegram 409 Conflict: Another instance is releasing connection. Retrying in {backoff}s...", flush=True)
                time.sleep(backoff)
                backoff = min(backoff + 3, 15)
            else:
                print(f"⚠️ Telegram API Exception: {e}. Retrying in 5s...", flush=True)
                time.sleep(5)
        except Exception as e:
            print(f"⚠️ Polling Exception: {e}. Retrying in 5s...", flush=True)
            time.sleep(5)
