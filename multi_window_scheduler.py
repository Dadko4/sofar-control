"""Multi-window charging scheduler.

Manages multiple charging windows per day with automatic sequencing:
- Stores queue of windows to charge
- Starts first window when time comes
- Transitions to self-use after each window
- Retry logic (5x with 2min intervals) on mode change failures
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOGGER = None  # Will be set by run.py

class MultiWindowScheduler:
    """Queue-based multi-window charger for 4T tariff optimization."""
    
    def __init__(self, client, state_path, mode_lock, discharge_active_fn, timezone_str, logger):
        self.client = client
        self.state_path = Path(state_path)
        self.mode_lock = mode_lock
        self.discharge_active_fn = discharge_active_fn
        self.timezone_str = timezone_str
        global LOGGER
        LOGGER = logger
        self.history_store = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        self._state = {
            "status": "idle",
            "windows_queue": [],  # List of {charge_start, charge_end, target_soc, ...}
            "current_window_index": 0,
            "plan_date": None,
            "error": None,
        }
        self._load_state()
    
    def _load_state(self):
        """Load saved state from disk."""
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and "windows_queue" in state:
                self._state = state
        except FileNotFoundError:
            pass
        except Exception as e:
            LOGGER.warning(f"Could not load multi-window state: {e}")
    
    def _save_state(self):
        """Save state to disk atomically."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            temp_path = self.state_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f)
            temp_path.replace(self.state_path)
        except Exception as e:
            LOGGER.error(f"Could not save multi-window state: {e}")
    
    def schedule_windows(self, windows, plan_date):
        """Schedule multiple charging windows for a day.
        
        Args:
            windows: List of window dicts with charge_start, charge_end, target_soc, grid_charge_kwh
            plan_date: Date string (YYYY-MM-DD)
        """
        with self._lock:
            self._state = {
                "status": "pending",
                "windows_queue": windows,
                "current_window_index": 0,
                "plan_date": plan_date,
                "error": None,
            }
            self._save_state()
            LOGGER.info(f"Scheduled {len(windows)} charging windows for {plan_date}")
            return self.get_state()
    
    def get_state(self):
        """Get current scheduler state."""
        with self._lock:
            return dict(self._state)
    
    def run_due(self, now=None):
        """Check if a window should be active now and execute it."""
        if now is None:
            now = datetime.now(ZoneInfo(self.timezone_str))
        
        # Ensure now is timezone-aware
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo(self.timezone_str))
        
        with self._lock:
            if self._state["status"] not in ("pending", "charging", "transitioning"):
                return
            
            windows = self._state.get("windows_queue", [])
            idx = self._state.get("current_window_index", 0)
            
            if idx >= len(windows):
                self._state["status"] = "completed"
                self._save_state()
                return
            
            window = windows[idx]
            start_dt = datetime.fromisoformat(window["charge_start"])
            end_dt = datetime.fromisoformat(window["charge_end"])
            
            # Ensure datetimes are timezone-aware (they come from window which was already isoformat())
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=ZoneInfo(self.timezone_str))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=ZoneInfo(self.timezone_str))
            
            # Check if we're in the window
            if now < start_dt:
                return  # Not time yet
            
            if now >= end_dt:
                # Window ended, move to next
                self._state["current_window_index"] += 1
                self._state["status"] = "transitioning"
                self._save_state()
                self._return_to_self_use_then_next()
                return
            
            # We're in the window - start charging if not already
            if self._state["status"] == "pending":
                self._state["status"] = "charging"
                self._save_state()
                self._start_window_charging(window)
    
    def _start_window_charging(self, window):
        """Start charging for a window with retry logic."""
        target_soc = window.get("recommended_target_soc", 80)
        max_retries = 5
        retry_interval = 120  # 2 min
        
        for attempt in range(1, max_retries + 1):
            try:
                if self.discharge_active_fn():
                    LOGGER.warning("Cannot start charging: discharge active")
                    return
                
                with self.mode_lock:
                    self.client.set_energy_storage_mode(
                        "grid_charge",
                        2000,
                        confirmed=True,
                        target_soc=target_soc,
                    )
                LOGGER.info(f"Window charging started: target SOC {target_soc}%")
                return
            except Exception as e:
                if attempt < max_retries:
                    LOGGER.warning(f"Charging start failed (attempt {attempt}/{max_retries}): {e}")
                    time.sleep(retry_interval)
                else:
                    LOGGER.error(f"Charging start failed after {max_retries} attempts")
                    with self._lock:
                        self._state["status"] = "failed"
                        self._state["error"] = str(e)
                        self._save_state()
    
    def _return_to_self_use_then_next(self):
        """Return to self-use after window ends, then start next window if any."""
        max_retries = 5
        retry_interval = 120
        
        for attempt in range(1, max_retries + 1):
            try:
                with self.mode_lock:
                    self.client.set_energy_storage_mode("self_use", confirmed=True)
                LOGGER.info("Returned to self-use after window")
                
                # Check if there's a next window
                with self._lock:
                    windows = self._state.get("windows_queue", [])
                    idx = self._state.get("current_window_index", 0)
                    if idx < len(windows):
                        self._state["status"] = "pending"
                    else:
                        self._state["status"] = "completed"
                    self._save_state()
                return
            except Exception as e:
                if attempt < max_retries:
                    LOGGER.warning(f"Self-use return failed (attempt {attempt}/{max_retries}): {e}")
                    time.sleep(retry_interval)
                else:
                    LOGGER.error(f"Self-use return failed after {max_retries} attempts")
                    with self._lock:
                        self._state["status"] = "failed"
                        self._state["error"] = str(e)
                        self._save_state()
    
    def cancel(self):
        """Cancel scheduled windows."""
        with self._lock:
            self._state["status"] = "cancelled"
            self._save_state()
    
    def start(self):
        """Start background monitoring thread."""
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
    
    def stop(self):
        """Stop background monitoring thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
    
    def _run(self):
        """Background loop: check due windows every second."""
        while not self._stop_event.is_set():
            try:
                self.run_due()
            except Exception as e:
                LOGGER.exception(f"Multi-window scheduler error: {e}")
            self._stop_event.wait(1)
