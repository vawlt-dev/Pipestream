# =============================================================================
# agent_worker.py — Polls VPS for Tasks, Runs Workflows, Posts Results
# =============================================================================
# This runs inside Docker on your home PC.
# It continuously polls your VPS for pending tasks, processes them,
# and sends results back.
#
# FLOW:
#   1. Poll VPS: "Any tasks for me?"
#   2. If task found: Mark as "running", start workflow
#   3. Workflow researches, drafts email, asks for confirmation
#   4. Wait for user to confirm/cancel on website
#   5. If confirmed: Send email, mark completed
# =============================================================================

import os
import time
import requests
from datetime import datetime
from router import route_workflow
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# CONFIG
# =============================================================================

VPS_BASE_URL = os.getenv("VPS_BASE_URL", "https://blakecollins.dev")
VPS_API_KEY = os.getenv("VPS_API_KEY", "")
POLL_INTERVAL = 10 # int(os.getenv("POLL_INTERVAL", "3"))  # seconds

if not VPS_API_KEY:
    print("❌ ERROR: VPS_API_KEY not set!")
    print("   Set it in .env or as environment variable")
    exit(1)

# =============================================================================
# VPS API CLIENT
# =============================================================================

class VPSClient:
    """Handles all communication with the VPS."""
    
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    
    def get_pending_tasks(self) -> list:
        """Fetch tasks waiting to be processed."""
        try:
            r = requests.get(
                f"{self.base_url}/agent/api/pending",
                headers=self.headers,
                timeout=10
            )
            r.raise_for_status()
            return r.json().get("tasks", [])
        except Exception as e:
            print(f"⚠️  Failed to fetch tasks: {e}")
            return []
    
    def get_task(self, task_id: str) -> dict | None:
        """Get task details."""
        try:
            r = requests.get(
                f"{self.base_url}/agent/api/task/{task_id}",
                headers=self.headers,
                timeout=10
            )
            r.raise_for_status()
            return r.json().get("task")
        except Exception as e:
            print(f"⚠️  Failed to get task {task_id}: {e}")
            return None
    
    def update_status(self, task_id: str, status: str, **kwargs):
        """Update task status and optional fields."""
        try:
            data = {"status": status, **kwargs}
            r = requests.post(
                f"{self.base_url}/agent/api/task/{task_id}/status",
                headers=self.headers,
                json=data,
                timeout=10
            )
            r.raise_for_status()
        except Exception as e:
            print(f"⚠️  Failed to update status: {e}")
    
    def log(self, task_id: str, message: str, log_type: str = "info"):
        """Add a log entry visible in the web UI."""
        try:
            r = requests.post(
                f"{self.base_url}/agent/api/task/{task_id}/log",
                headers=self.headers,
                json={"message": message, "log_type": log_type},
                timeout=10
            )
            r.raise_for_status()
        except Exception as e:
            # Don't spam errors for log failures
            pass

# =============================================================================
# WORKER LOOP
# =============================================================================

def run_worker():
    """Main worker loop — poll, process, repeat."""
    
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║              AGENT WORKER STARTING                           ║
╠══════════════════════════════════════════════════════════════╣
║  VPS: {VPS_BASE_URL:<52}   ║
║  Polling every {POLL_INTERVAL} seconds                                    ║
║  Press Ctrl+C to stop                                        ║
╚══════════════════════════════════════════════════════════════╝
""")
    
    client = VPSClient(VPS_BASE_URL, VPS_API_KEY)
    
    # Test connection
    print("🔌 Testing VPS connection...")
    tasks = client.get_pending_tasks()
    if tasks is not None:
        print(f"✅ Connected! {len(tasks)} pending task(s)")
    else:
        print("⚠️  Connection test returned None — check API key and URL")
    
    while True:
        try:
            # Poll for pending tasks
            tasks = client.get_pending_tasks()
            
            if tasks:
                task = tasks[0]  # Process one at a time
                task_id = task["id"]
                input_text = task["input_text"]
                
                print(f"\n{'='*60}", flush=True)
                print(f"📋 NEW TASK: {task_id}", flush=True)
                print(f"   Input:   {input_text}", flush=True)
                print(f"   Status:  {task.get('status')}", flush=True)
                print(f"   Trusted: {task.get('trusted', False)}", flush=True)
                print(f"{'='*60}", flush=True)

                # Mark as running
                client.update_status(task_id, "running")
                client.log(task_id, "Agent picked up task", "info")

                t_start = datetime.now()
                try:
                    route_workflow(task_id, input_text, client)
                    elapsed = (datetime.now() - t_start).total_seconds()
                    print(f"\n✅ Task {task_id} finished in {elapsed:.1f}s", flush=True)

                except Exception as e:
                    import traceback
                    error_msg = str(e)
                    elapsed   = (datetime.now() - t_start).total_seconds()
                    print(f"\n❌ Workflow error after {elapsed:.1f}s: {error_msg}", flush=True)
                    print(traceback.format_exc(), flush=True)
                    client.log(task_id, f"Error: {error_msg}", "error")
                    client.update_status(task_id, "failed", error_message=error_msg)
            
            else:
                # No tasks, just show a dot occasionally
                pass
            
            time.sleep(POLL_INTERVAL)
            
        except KeyboardInterrupt:
            print("\n\n👋 Worker stopped by user")
            break
        except Exception as e:
            print(f"⚠️  Worker error: {e}")
            time.sleep(POLL_INTERVAL)

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    run_worker()
