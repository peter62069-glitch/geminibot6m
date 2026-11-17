# 🤖 MyIM3 Google Gemini 6M Automation Telegram Bot

An automated Telegram Bot integrating with **Indosat Ooredoo (myIM3)** and virtual SMS gateway APIs (**Simxpro**, **Grizzly SMS**, **SMS-Activate**) to claim **Google Gemini / AI Plus (6 Months Free Promo)** access links.

---

## 🌟 Key Updates & Features

1. **⚡ Live Operator Options (Simxpro Pricing):**
   - **🟢 Operator 31 (Standard)** — `₹7.00`
   - **🔵 Operator 7 (Fast / Premium)** — `₹12.42`
   - Direct `operator=31` and `operator=7` parameter mapping to fix `NO_CONNECTION` issues.

2. **🔄 Continuous Zero-Loss Auto-Claimer Loop:**
   - **Live Balance Pre-Check:** Only starts if your live balance satisfies the selected operator's price.
   - **Stock Waiting & Auto-Retry:** If a pool is temporarily empty (`NO_CONNECTION`), the bot updates the live dashboard and retries automatically every 3 seconds instead of failing.
   - **Instant Non-Indosat Detection:** Validates official Indosat prefixes (`0814`, `0815`, `0816`, `0855`, `0856`, `0857`, `0858`). If a non-Indosat number is assigned (e.g. Telkomsel, XL, Tri), the bot **instantly cancels & refunds** the order in `< 0.5s` and grabs the next number.
   - **myIM3 Verification:** If myIM3 rejects the SIM (Error 10011 / inactive cellular status), the bot **instantly cancels & refunds** on the SMS API and buys the next number.
   - **Continuous Auto-Repeat:** Repeats automatically using the refunded balance until a valid Google Gemini AI Plus 6-month link is claimed!
   - **Live Balance Refresh:** Displays refreshed balance in real-time on the progress dashboard.
   - **Live 1-by-1 Delivery:** Sends each generated link into chat immediately, and compiles all accounts into an attached `.txt` report file.

3. **📱 Clean User Interface:**
   - Standard menu options:
     - 📱 **Manual Login** (Phone + OTP verification)
     - ⚡ **Auto-Claimer (SMS API)** (Automated purchase, OTP polling & claim)
     - 🔑 **Session Login** (Instant login & claim via pasted session/JWT token)
     - 💰 **Check Live Balance**
     - ⚙️ **Settings**
   - Native Telegram persistent Menu button registered.

---

## 📂 Project Structure

```text
myim3_gemini_bot/
├── bot.py             # Main Telegram Bot (TeleBot HTML handlers, operator 31/7 worker)
├── im3_api.py         # MyIM3 REST API Client (RC4 crypto, SHA-512 signing, prefix filter)
├── sms_service.py     # SMS Provider API client (Simxpro with operator param)
├── config.py          # Dynamic settings manager (JSON-backed)
├── settings.json      # Dynamic configuration & operator prices file
├── requirements.txt   # Dependencies
├── start.bat          # 1-Click Windows Launcher
└── README.md          # Documentation
```

---

## 🚀 How to Run

Double click [start.bat](file:///C:/Users/PARAS/.gemini/antigravity/scratch/myim3_gemini_bot/start.bat) or run in terminal:
```bash
python bot.py
```
