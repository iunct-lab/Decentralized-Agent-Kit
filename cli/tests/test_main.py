import unittest
from unittest.mock import patch, MagicMock
from typer.testing import CliRunner

from src.client import ApprovalError
from src.main import app


class TestCLICommands(unittest.TestCase):
    def setUp(self):
        """Set up test environment."""
        self.runner = CliRunner()

    @patch('src.main.config_manager')
    def test_login_command(self, mock_config_manager):
        """Test login command."""
        result = self.runner.invoke(app, [
            "login",
            "--username", "test_user",
            "--agent-url", "http://test:8000"
        ])
        
        self.assertEqual(result.exit_code, 0)
        mock_config_manager.set_user.assert_called_once_with("test_user")
        mock_config_manager.set_agent_url.assert_called_once_with("http://test:8000")
        self.assertIn("Successfully logged in", result.stdout)

    @patch('src.main.config_manager')
    def test_config_command(self, mock_config_manager):
        """Test config command displays current settings."""
        mock_config_manager.get_user.return_value = "test_user"
        mock_config_manager.get_agent_url.return_value = "http://test:8000"
        
        result = self.runner.invoke(app, ["config"])
        
        self.assertEqual(result.exit_code, 0)
        self.assertIn("test_user", result.stdout)
        self.assertIn("http://test:8000", result.stdout)

    @patch('src.main.AgentClient')
    @patch('src.main.config_manager')
    def test_run_command_success(self, mock_config_manager, mock_client_class):
        """Test run command with successful response."""
        mock_config_manager.get_user.return_value = "test_user"
        
        # Mock client
        mock_client = MagicMock()
        mock_client.run_task.return_value = [{
            "content": {
                "role": "model",
                "parts": [{"text": "Test response"}]
            }
        }]
        mock_client_class.return_value = mock_client
        
        result = self.runner.invoke(app, ["run", "test prompt"])
        
        self.assertEqual(result.exit_code, 0)
        mock_client.run_task.assert_called_once()
        self.assertIn("Test response", result.stdout)

    @patch('src.main.AgentClient')
    @patch('src.main.config_manager')
    def test_run_command_error(self, mock_config_manager, mock_client_class):
        """Test run command handles errors gracefully."""
        mock_config_manager.get_user.return_value = "test_user"
        
        # Mock client to raise error
        mock_client = MagicMock()
        mock_client.run_task.side_effect = Exception("Connection error")
        mock_client_class.return_value = mock_client
        
        result = self.runner.invoke(app, ["run", "test prompt"])
        
        self.assertEqual(result.exit_code, 0)  # Typer commands succeed even with handled exceptions
        self.assertIn("Error", result.stdout)

    @patch('src.main.AgentClient')
    def test_run_answers_an_approval_through_approvals(self, mock_client_class):
        """The approval prompt in `run` answers through /approvals/{id}/reply."""
        mock_client = MagicMock(session_id="s1")
        mock_client.run_task.return_value = {"status": "needs_approval", "tool_call": {
            "tool_name": "planner", "tool_args": {}, "tool_call_id": "fc_1"}}
        mock_client.reply_approval.return_value = [{"content": {"role": "model", "parts": [{"text": "Planned."}]}}]
        mock_client_class.return_value = mock_client

        result = self.runner.invoke(app, ["run", "plan it"], input="n\n")

        self.assertEqual(result.exit_code, 0, result.stdout)
        mock_client.reply_approval.assert_called_once_with("fc_1", "s1", "reject")
        self.assertIn("Planned.", result.stdout)

    @patch('src.main.AgentClient')
    def test_approvals_lists_another_clients_session(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.list_approvals.return_value = [
            {"id": "fc_1", "kind": "approval", "tool_name": "planner", "tool_args": {}, "status": "pending"}]
        mock_client_class.return_value = mock_client

        result = self.runner.invoke(app, ["approvals", "--session", "bff_s", "--user", "bff_u"])

        self.assertEqual(result.exit_code, 0, result.stdout)
        mock_client.list_approvals.assert_called_once_with("bff_s", user_id="bff_u")
        self.assertIn("fc_1", result.stdout)
        self.assertIn("planner", result.stdout)

    @patch('src.main.AgentClient')
    def test_approve_sends_the_mode(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.reply_approval.return_value = [{"content": {"role": "model", "parts": [{"text": "Stopped."}]}}]
        mock_client_class.return_value = mock_client

        for args, mode, reason in ((["fc_1", "-s", "s1"], "once", ""),
                                   (["fc_1", "-s", "s1", "--always"], "always", ""),
                                   (["fc_1", "-s", "s1", "--reject", "--reason", "not now"], "reject", "not now")):
            mock_client.reply_approval.reset_mock()
            result = self.runner.invoke(app, ["approve", *args])
            self.assertEqual(result.exit_code, 0, result.stdout)
            mock_client.reply_approval.assert_called_once_with("fc_1", "s1", mode, reason=reason, user_id=None)
            self.assertIn("Stopped.", result.stdout)

    @patch('src.main.AgentClient')
    def test_approve_fails_when_not_pending(self, mock_client_class):
        """A second answer to the same id is refused by the agent, and the CLI says so with a non-zero exit."""
        mock_client = MagicMock()
        mock_client.reply_approval.side_effect = ApprovalError(404, "fc_1 is not pending")
        mock_client_class.return_value = mock_client

        result = self.runner.invoke(app, ["approve", "fc_1", "-s", "s1"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("not pending", result.stdout)

    def test_approve_refuses_reject_and_always_together(self):
        result = self.runner.invoke(app, ["approve", "fc_1", "-s", "s1", "--reject", "--always"])
        self.assertEqual(result.exit_code, 2)


if __name__ == '__main__':
    unittest.main()
