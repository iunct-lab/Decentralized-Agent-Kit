import requests
from typing import Dict, Any, List, Optional
import time
import uuid
from .config import ConfigManager


class ApprovalError(Exception):
    """The agent refused an answer: 404 (not pending: answered, dropped, unknown),
    409 (expired: the agent was resumed with timed_out), 422 (bad body)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code


def _needs_approval(response_data: Any) -> Any:
    """ADK's events, or a needs_approval dict when the turn stopped on a confirmation."""
    if isinstance(response_data, list):
        for event in response_data:
            for part in event.get("content", {}).get("parts", []):
                fc = part.get("functionCall") or {}
                if fc.get("name") == "adk_request_confirmation":
                    original_fc = fc.get("args", {}).get("originalFunctionCall", {})
                    return {
                        "status": "needs_approval",
                        "tool_call": {
                            "tool_name": original_fc.get("name"),
                            "tool_args": original_fc.get("args"),
                            "tool_call_id": fc.get("id"),  # ID of the confirmation request
                        },
                        "response": response_data,
                    }
    return response_data


class AgentClient:
    def __init__(self, session_id: Optional[str] = None):
        self.config = ConfigManager()
        self.base_url = self.config.get_agent_url()
        self.username = self.config.get_user()
        
        if session_id:
            self.session_id = session_id
        elif self.username:
            # Generate a unique session ID if not provided
            # Format: session_{username}_{uuid}
            self.session_id = f"session_{self.username}_{uuid.uuid4()}"
        else:
            self.session_id = str(uuid.uuid4())

    def reset_session(self):
        """Regenerate a new session ID."""
        if self.username:
            self.session_id = f"session_{self.username}_{uuid.uuid4()}"
        else:
            self.session_id = str(uuid.uuid4())

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.username:
            headers["X-User-ID"] = self.username
            if self.session_id:
                headers["X-Session-ID"] = self.session_id
        return headers

    def _ensure_session(self):
        """Ensure session exists, create if needed."""
        if not self.username:
            raise ValueError("Not logged in. Please run 'dak-cli login' first.")
        
        # Try to get session info to check if it exists
        try:
            response = requests.get(
                f"{self.base_url}/apps/dak_agent/users/{self.username}/sessions/{self.session_id}",
                headers=self._get_headers(),
                timeout=5
            )
            if response.status_code == 200:
                return  # Session exists
        except:
            pass
        
        # Session doesn't exist, create it
        try:
            response = requests.post(
                f"{self.base_url}/apps/dak_agent/users/{self.username}/sessions",
                json={},
                headers=self._get_headers(),
                timeout=10
            )
            response.raise_for_status()
            session_data = response.json()
            self.session_id = session_data.get("id", self.session_id)
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to create session: {e}")


    def run_task(self, prompt: str, permissions: Dict[str, str] = None) -> Dict[str, Any]:
        if not self.username:
            raise ValueError("Not logged in. Please run 'dak-cli login' first.")

        # Ensure session exists
        self._ensure_session()

        # ADK standard API schema
        payload = {
            "app_name": "dak_agent",
            "user_id": self.username,
            "session_id": self.session_id,
            "new_message": {"parts": [{"text": prompt}]},
        }

        try:
            response = requests.post(
                f"{self.base_url}/run",
                json=payload,
                headers=self._get_headers(),
                timeout=300
            )
            response.raise_for_status()
            return _needs_approval(response.json())
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to communicate with agent: {e}")

    def list_approvals(self, session_id: str, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Pending approvals and questions of a session, from any client (GET /approvals)."""
        try:
            response = requests.get(
                f"{self.base_url}/approvals",
                params={"app_name": "dak_agent", "user_id": user_id or self.username, "session_id": session_id},
                headers=self._get_headers(),
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to list approvals: {e}")

    def reply_approval(self, approval_id: str, session_id: str, mode: str, reason: str = "",
                       user_id: Optional[str] = None) -> Any:
        """Answer a pending approval (once / always / reject) through POST
        /approvals/{id}/reply, which refuses an id that is no longer pending."""
        try:
            response = requests.post(
                f"{self.base_url}/approvals/{approval_id}/reply",
                json={"app_name": "dak_agent", "user_id": user_id or self.username, "session_id": session_id,
                      "mode": mode, "reason": reason},
                headers=self._get_headers(),
                timeout=300
            )
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to reply to approval: {e}")
        if response.status_code != 200:
            raise ApprovalError(response.status_code, response.text)
        return _needs_approval(response.json())

    def list_sessions(self) -> Dict[str, Any]:
        if not self.username:
            raise ValueError("Not logged in. Please run 'dak-cli login' first.")
        
        try:
            # ADK standard: GET /apps/{app}/users/{user}/sessions
            response = requests.get(
                f"{self.base_url}/apps/dak_agent/users/{self.username}/sessions",
                headers=self._get_headers(),
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to list sessions: {e}")

    def get_session_history(self, session_id: str) -> Dict[str, Any]:
        if not self.username:
            raise ValueError("Not logged in. Please run 'dak-cli login' first.")
        
        try:
            # ADK standard: GET /apps/{app}/users/{user}/sessions/{session}
            response = requests.get(
                f"{self.base_url}/apps/dak_agent/users/{self.username}/sessions/{session_id}",
                headers=self._get_headers(),
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to get session history: {e}")

    def delete_session(self, session_id: str) -> Dict[str, Any]:
        if not self.username:
            raise ValueError("Not logged in. Please run 'dak-cli login' first.")
        
        try:
            # ADK standard: DELETE /apps/{app}/users/{user}/sessions/{session}
            response = requests.delete(
                f"{self.base_url}/apps/dak_agent/users/{self.username}/sessions/{session_id}",
                headers=self._get_headers(),
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to delete session: {e}")
