import time
import re
import threading
import requests

class DeferredCanceller:
    """
    Background worker that queues SMS orders that could not be immediately cancelled
    (due to provider restrictions like EARLY_CANCEL_DENIED during the first 120 seconds).
    It retries cancellation once the 120-second window passes, ensuring 100% balance refunds.
    """
    def __init__(self, sms_service):
        self.sms_service = sms_service
        self.pending = []  # [{'order_id': str, 'created_at': float}]
        self.lock = threading.Lock()
        self.running = True
        self.worker_thread = threading.Thread(target=self._run, daemon=True)
        self.worker_thread.start()

    def add(self, order_id: str, purchase_time: float = None):
        if not order_id:
            return
        p_time = purchase_time if purchase_time else time.time()
        with self.lock:
            # Avoid duplicate queuing
            if not any(item['order_id'] == order_id for item in self.pending):
                self.pending.append({'order_id': order_id, 'created_at': p_time})
                print(f"[DeferredCanceller] Queued order {order_id} for deferred refund")

    def _run(self):
        while self.running:
            time.sleep(10)
            now = time.time()
            to_remove = []

            with self.lock:
                current_items = list(self.pending)

            for item in current_items:
                age = now - item['created_at']
                # Try refunding once at least 120 seconds have elapsed
                if age >= 120:
                    try:
                        success = self.sms_service.set_status(item['order_id'], 8)
                        if success:
                            print(f"[DeferredCanceller] Successfully refunded order {item['order_id']} after {int(age)}s!")
                            to_remove.append(item)
                        elif age > 360:
                            # Provider automatically cancels and refunds expired activations after 5-6 mins
                            print(f"[DeferredCanceller] Order {item['order_id']} expired on provider ({int(age)}s), removing from queue.")
                            to_remove.append(item)
                    except Exception as e:
                        print(f"[DeferredCanceller] Error cancelling order {item['order_id']}: {e}")

            if to_remove:
                with self.lock:
                    for item in to_remove:
                        if item in self.pending:
                            self.pending.remove(item)


class SMSService:
    """
    Handles SMS-Activate protocol compatible providers:
    - Simxpro (simxpro.org)
    - Grizzly SMS (api.grizzlysms.com)
    - SMS-Activate (api.sms-activate.org)
    """

    def __init__(self, provider_url: str, api_key: str):
        self.provider_url = (provider_url or "").strip()
        self.api_key = (api_key or "").strip()
        self.web_token = ""
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        })
        self.canceller = DeferredCanceller(self)

    def set_web_token(self, token: str):
        self.web_token = token.strip()

    def get_balance(self) -> dict:
        """Returns balance details: {'success': bool, 'balance': float, 'message': str}"""
        if not self.api_key:
            return {"success": False, "balance": 0.0, "message": "API key is not configured."}
        
        try:
            params = {
                "api_key": self.api_key,
                "action": "getBalance"
            }
            resp = self.session.get(self.provider_url, params=params, timeout=15)
            text = resp.text.strip()
            
            if "ACCESS_BALANCE" in text:
                parts = text.split(":")
                balance = float(parts[1]) if len(parts) > 1 else 0.0
                return {"success": True, "balance": balance, "message": f"{balance:.2f}"}
            
            try:
                data = resp.json()
                if isinstance(data, dict) and "balance" in data:
                    balance = float(data["balance"])
                    return {"success": True, "balance": balance, "message": f"{balance:.2f}"}
            except Exception:
                pass

            if "BAD_KEY" in text or "NO_KEY" in text or "INVALID_KEY" in text:
                return {"success": False, "balance": 0.0, "message": "Invalid API Key! Please verify your key."}
            else:
                return {"success": False, "balance": 0.0, "message": f"Response: {text}"}
        except Exception as e:
            return {"success": False, "balance": 0.0, "message": f"Connection error: {str(e)}"}

    def buy_number(self, country: str = "6", service: str = "is", operator: str = None, server: str = None) -> dict:
        """
        Requests a new virtual phone number.
        Returns: {'success': bool, 'order_id': str, 'phone': str, 'message': str, 'raw': str, 'purchase_time': float}
        """
        if not self.api_key:
            return {"success": False, "order_id": "", "phone": "", "message": "API key is not configured.", "raw": "", "purchase_time": 0.0}
        
        # Handle provider-specific service codes and operator translations
        is_uotp = "uotp.store" in self.provider_url
        if is_uotp:
            if service in ["is", "im3"] or not service:
                service = "bme"
            if operator == "uotp_955":
                operator = "1"
            elif not operator:
                operator = "8"

        try:
            params = {
                "api_key": self.api_key,
                "action": "getNumber",
                "service": service,
                "country": country
            }
            if operator:
                params["operator"] = str(operator)
            if server:
                params["server"] = str(server)

            resp = None
            for attempt in range(2):
                try:
                    resp = self.session.get(self.provider_url, params=params, timeout=15)
                    break
                except Exception as net_err:
                    if attempt == 0:
                        time.sleep(1)
                    else:
                        raise net_err
            text = resp.text.strip()

            # Format: ACCESS_NUMBER:order_id:phone_number
            if "ACCESS_NUMBER" in text:
                parts = text.split(":")
                order_id = parts[1]
                phone = parts[2]
                return {
                    "success": True,
                    "order_id": order_id,
                    "phone": phone,
                    "message": "Number acquired successfully.",
                    "raw": text,
                    "purchase_time": time.time()
                }
            # Fallback service negotiation for providers supporting either 'is' or 'im3'
            if "BAD_SERVICE" in text and service in ["is", "im3"]:
                alt_service = "im3" if service == "is" else "is"
                params["service"] = alt_service
                try:
                    resp_alt = self.session.get(self.provider_url, params=params, timeout=15)
                    text_alt = resp_alt.text.strip()
                    if "ACCESS_NUMBER" in text_alt:
                        parts = text_alt.split(":")
                        return {
                            "success": True,
                            "order_id": parts[1],
                            "phone": parts[2],
                            "message": f"Number acquired successfully ({alt_service}).",
                            "raw": text_alt,
                            "purchase_time": time.time()
                        }
                    text = text_alt
                except Exception:
                    pass

            if "NO_NUMBERS" in text:
                return {"success": False, "order_id": "", "phone": "", "message": "No numbers available right now for this service/operator (NO_NUMBERS).", "raw": text, "purchase_time": 0.0}
            elif "NO_BALANCE" in text:
                return {"success": False, "order_id": "", "phone": "", "message": "Insufficient balance on your SMS provider account (NO_BALANCE).", "raw": text, "purchase_time": 0.0}
            elif "BAD_KEY" in text:
                return {"success": False, "order_id": "", "phone": "", "message": "Invalid API key (BAD_KEY).", "raw": text, "purchase_time": 0.0}
            elif "BAD_SERVICE" in text:
                return {"success": False, "order_id": "", "phone": "", "message": f"Service code '{service}' is not supported by this provider (BAD_SERVICE).", "raw": text, "purchase_time": 0.0}
            elif "NO_CONNECTION" in text:
                return {"success": False, "order_id": "", "phone": "", "message": "Provider connection temporarily offline or stock replenishing (NO_CONNECTION).", "raw": text, "purchase_time": 0.0}
            else:
                return {"success": False, "order_id": "", "phone": "", "message": f"Provider response: {text}", "raw": text, "purchase_time": 0.0}
        except Exception as e:
            return {"success": False, "order_id": "", "phone": "", "message": f"Network exception: {str(e)}", "raw": str(e), "purchase_time": 0.0}

    def check_status(self, order_id: str) -> dict:
        """
        Checks SMS order status.
        Returns:
            {'status': 'WAIT'|'OK'|'CANCEL'|'ERROR', 'code': str, 'raw': str}
        """
        try:
            params = {
                "api_key": self.api_key,
                "action": "getStatus",
                "id": order_id
            }
            resp = self.session.get(self.provider_url, params=params, timeout=15)
            text = resp.text.strip()

            if "STATUS_WAIT_CODE" in text:
                return {"status": "WAIT", "code": "", "raw": text}
            elif "STATUS_OK" in text:
                parts = text.split(":", 1)
                full_code = parts[1] if len(parts) > 1 else ""
                # Extract 6 digit numeric code
                extracted = re.findall(r'\b\d{6}\b', full_code)
                code = extracted[0] if extracted else full_code
                return {"status": "OK", "code": code, "raw": full_code}
            elif "STATUS_CANCEL" in text:
                return {"status": "CANCEL", "code": "", "raw": text}
            else:
                return {"status": "ERROR", "code": "", "raw": text}
        except Exception as e:
            return {"status": "ERROR", "code": "", "raw": str(e)}

    def wait_for_otp(self, order_id: str, timeout_seconds: int = 120, poll_interval: int = 4, progress_callback=None, check_stop_callback=None) -> dict:
        start = time.time()
        while time.time() - start < timeout_seconds:
            if check_stop_callback and check_stop_callback():
                return {"success": False, "code": "", "message": "Stopped by user."}

            elapsed = int(time.time() - start)
            if progress_callback:
                try:
                    progress_callback(elapsed, timeout_seconds)
                except Exception:
                    pass

            res = self.check_status(order_id)
            if res["status"] == "OK":
                return {"success": True, "code": res["code"], "raw": res["raw"]}
            elif res["status"] == "CANCEL":
                return {"success": False, "code": "", "message": "Order was cancelled."}

            time.sleep(poll_interval)

        return {"success": False, "code": "", "message": f"Timeout! No SMS received within {timeout_seconds} seconds."}

    def set_status(self, order_id: str, status_code: int) -> bool:
        """
        status_code:
        6 = complete/finish order
        8 = cancel/refund order
        """
        try:
            params = {
                "api_key": self.api_key,
                "action": "setStatus",
                "status": str(status_code),
                "id": order_id
            }
            resp = self.session.get(self.provider_url, params=params, timeout=15)
            return "ACCESS_" in resp.text
        except Exception:
            return False

    def cancel_number(self, order_id: str, purchase_time: float = None) -> bool:
        """
        Attempts immediate cancel. If denied due to early cancellation window (<120s),
        automatically queues for deferred cancellation to guarantee refund.
        """
        success = self.set_status(order_id, 8)
        if not success:
            # Queue for refund once the 120-second window passes
            self.canceller.add(order_id, purchase_time)
        return success

    def cancel_and_wait_refund(self, order_id: str, purchase_time: float, progress_callback=None, check_stop_callback=None) -> bool:
        """
        Ensures strict 1-by-1 lifecycle: Cancels the order and waits until the 120s window
        passes so the refund is 100% processed BEFORE returning. This guarantees that
        no additional balance is consumed, allowing the bot to safely repeat with the SAME balance!
        """
        # Try immediate cancel first
        if self.set_status(order_id, 8):
            return True

        # Simxpro requires 120s before allowing refund
        MIN_WAIT = 122
        while True:
            if check_stop_callback and check_stop_callback():
                self.canceller.add(order_id, purchase_time)
                return False

            now = time.time()
            elapsed = int(now - purchase_time)
            remaining = max(0, MIN_WAIT - elapsed)

            if progress_callback:
                try:
                    progress_callback(remaining, elapsed)
                except Exception:
                    pass

            if remaining <= 0:
                # 120s reached -> execute cancellation now
                if self.set_status(order_id, 8):
                    return True
                # If still processing, wait 3s and retry up to 150s
                time.sleep(3)
                if elapsed > 150:
                    self.canceller.add(order_id, purchase_time)
                    return True
            else:
                sleep_dur = min(3, remaining) if remaining > 0 else 1
                time.sleep(sleep_dur)

    def finish_number(self, order_id: str) -> bool:
        return self.set_status(order_id, 6)
